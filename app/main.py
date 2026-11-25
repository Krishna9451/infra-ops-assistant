
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('infra-ops')

class Target(BaseModel):
    namespace: str = Field(default='demo', pattern=r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
    deployment: str = Field(default='payments-api', pattern=r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
    scenario: Literal['high_cpu','unhealthy','healthy'] = 'unhealthy'

class Note(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

class Explanation(BaseModel):
    summary: str = Field(min_length=1, max_length=1500)
    investigation: list[str] = Field(max_length=5)

class Config:
    def __init__(self):
        self.mode = os.getenv('OPS_MODE', 'demo')
        if self.mode not in ('demo','live'): raise ValueError('OPS_MODE must be demo or live')
        self.db = os.getenv('OPS_DB', str(ROOT / ('ops-live.db' if self.mode == 'live' else 'ops.db')))
        self.prom = os.getenv('PROMETHEUS_URL','').rstrip('/')
        self.context = os.getenv('KUBE_CONTEXT','')
        self.allow = set(filter(None, os.getenv('KUBE_ALLOWED_TARGETS','').split(',')))
        self.live_write = os.getenv('ENABLE_LIVE_RESTART','false').lower() == 'true'
        self.operator = os.getenv('OPERATOR_TOKEN','')
        self.approver = os.getenv('APPROVER_TOKEN','')
        self.llm_key = os.getenv('LLM_API_KEY','')
        self.llm_url = os.getenv('LLM_BASE_URL','https://api.groq.com/openai/v1').rstrip('/')
        self.llm_model = os.getenv('LLM_MODEL','openai/gpt-oss-20b')
        self.aws_region = os.getenv('AWS_REGION','')
        if self.mode == 'live' and (not self.context or not self.allow or min(len(self.operator),len(self.approver)) < 24 or self.operator == self.approver):
            raise ValueError('Live mode requires explicit Kubernetes context, target allowlist and distinct >=24-character role tokens')

@contextmanager
def db_conn(cfg):
    conn = sqlite3.connect(cfg.db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn: yield conn
    finally: conn.close()

def init_db(cfg):
    with db_conn(cfg) as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL, at REAL NOT NULL);''')

def audit(c, iid, actor, event, detail=''):
    c.execute('INSERT INTO audit(incident_id,actor,event,detail,at) VALUES(?,?,?,?,?)',(iid,actor,event,detail,time.time()))
    log.info('incident=%s actor=%s event=%s',iid,actor,event)

def get_incident(c,iid):
    row = c.execute('SELECT * FROM incidents WHERE id=?',(iid,)).fetchone()
    if not row: raise HTTPException(404,'Incident not found')
    data = json.loads(row['payload'])
    data.update(id=iid,status=row['status'],created=row['created'],updated=row['updated'])
    return data

def run_kube(cfg, args):
    return subprocess.run(['kubectl','--context',cfg.context,'--request-timeout=8s',*args], capture_output=True,text=True,timeout=12,check=True).stdout

def check_target(cfg,t):
    if cfg.mode == 'live' and f'{t.namespace}/{t.deployment}' not in cfg.allow:
        raise HTTPException(403,'Target not allowlisted')

def collect(cfg,t):
    if cfg.mode == 'demo':
        values={'unhealthy':(42,1,2),'high_cpu':(94,2,2),'healthy':(23,2,2)}
        cpu,ready,desired=values[t.scenario]
        return {'source':'simulated','cpu_percent':cpu,'ready_replicas':ready,'desired_replicas':desired,'aws_instances':None,'warnings':[]}
    warnings=[]; cpu=None; ready=None; desired=None; aws=None
    try:
        obj=json.loads(run_kube(cfg,['-n',t.namespace,'get','deployment',t.deployment,'-o','json']))
        desired=int(obj['spec'].get('replicas',1)); ready=int(obj['status'].get('readyReplicas',0))
    except Exception as e:
        log.warning('kubernetes collection error: %s',type(e).__name__); warnings.append('Kubernetes deployment data unavailable')
    if cfg.prom:
        try:
            # Standard kube-state-metrics / cAdvisor series. Deployment CPU usage as percentage of CPU limits.
            ns=t.namespace; dep=t.deployment
            selector=f'namespace="{ns}",pod=~"{dep}-[a-z0-9]+-[a-z0-9]+",container!="",container!="POD"'
            query=f'100 * sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m])) / sum(kube_pod_container_resource_limits{{{selector},resource="cpu",unit="core"}})'
            res=httpx.get(cfg.prom+'/api/v1/query',params={'query':query},timeout=8).raise_for_status().json()
            val=float(res['data']['result'][0]['value'][1]); cpu=round(val,1) if 0<=val<100000 else None
            if cpu is None: warnings.append('CPU metric invalid or missing limits')
        except Exception as e:
            log.warning('prometheus collection error: %s',type(e).__name__); warnings.append('Prometheus CPU metric unavailable')
    else: warnings.append('Prometheus not configured')
    if cfg.aws_region:
        try:
            import boto3
            from botocore.config import Config as AWSConfig
            ec2=boto3.client('ec2',region_name=cfg.aws_region,config=AWSConfig(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1}))
            response=ec2.describe_instances(MaxResults=100,Filters=[{'Name':'instance-state-name','Values':['running']}])
            aws=sum(len(r['Instances']) for r in response['Reservations'])
            if response.get('NextToken'): warnings.append('AWS running-instance count is first page only')
        except Exception as e:
            log.warning('aws collection error: %s',type(e).__name__); warnings.append('AWS instance inventory unavailable')
    return {'source':'live','cpu_percent':cpu,'ready_replicas':ready,'desired_replicas':desired,'aws_instances':aws,'warnings':warnings}

def diagnose(evidence):
    ready=evidence['ready_replicas']; desired=evidence['desired_replicas']; cpu=evidence['cpu_percent']
    if ready is None or desired is None:
        return {'severity':'unknown','action':'none','summary':'Deployment state unavailable; investigate the connection first.'}
    if ready<desired:
        return {'severity':'critical','action':'restart_deployment','summary':'Deployment has fewer ready replicas than desired. Review events and logs before approving a rolling restart.'}
    if cpu is not None and cpu>=85:
        return {'severity':'warning','action':'none','summary':'CPU usage is elevated. Review resource limits, traffic and autoscaling; no automatic restart proposed.'}
    if cpu is None:
        return {'severity':'unknown','action':'none','summary':'Replicas are ready, but CPU evidence is unavailable.'}
    return {'severity':'healthy','action':'none','summary':'Replica readiness and CPU checks are within configured policy thresholds.'}

def explain(cfg,evidence,decision):
    fallback={'summary':decision['summary'],'investigation':['Inspect deployment events and recent logs','Review resource limits and recent changes','Verify readiness after remediation'],'provider':'rules'}
    if not cfg.llm_key: return fallback
    try:
        response=httpx.post(cfg.llm_url+'/chat/completions',headers={'Authorization':'Bearer '+cfg.llm_key},json={'model':cfg.llm_model,'messages':[{'role':'system','content':'You are an SRE assistant. Return only a JSON object with summary (string) and investigation (list of up to 5 strings). Explain the supplied metrics and fixed policy decision. Never suggest executing commands or change the policy action. Missing data is unknown.'},{'role':'user','content':json.dumps({'evidence':evidence,'policy':decision})}],'response_format':{'type':'json_object'},'temperature':0.2},timeout=15).raise_for_status().json()
        result=Explanation.model_validate_json(response['choices'][0]['message']['content']).model_dump()
        return {**result,'provider':'llm'}
    except Exception as e:
        log.warning('llm fallback: %s',type(e).__name__)
        return {**fallback,'provider':'rules_fallback'}

def workflow(cfg,t):
    def gather(state): return {'evidence':collect(cfg,t)}
    def classify(state): return {'decision':diagnose(state['evidence'])}
    def narrate(state): return {'explanation':explain(cfg,state['evidence'],state['decision'])}
    try:
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict
    except ImportError:
        state={}; state.update(gather(state)); state.update(classify(state)); state.update(narrate(state)); return {**state,'engine':'python'}
    class State(TypedDict,total=False):
        evidence:dict
        decision:dict
        explanation:dict
    graph=StateGraph(State)
    graph.add_node('collect',gather); graph.add_node('diagnose',classify); graph.add_node('explain',narrate)
    graph.add_edge(START,'collect'); graph.add_edge('collect','diagnose'); graph.add_edge('diagnose','explain'); graph.add_edge('explain',END)
    return {**graph.compile().invoke({}),'engine':'langgraph'}

def create_app(cfg=None):
    cfg=cfg or Config(); init_db(cfg)
    api=FastAPI(title='InfraOps Assistant',version='1.0.0')
    registry=CollectorRegistry(); requests=Counter('infraops_http_requests_total','API requests',['method','status'],registry=registry); latency=Histogram('infraops_http_request_seconds','API latency',registry=registry); actions=Counter('infraops_workflow_events_total','Workflow transitions',['event'],registry=registry)
    @api.middleware('http')
    async def observe(request:Request,call_next):
        start=time.monotonic(); response=await call_next(request); requests.labels(request.method,str(response.status_code)).inc(); latency.observe(time.monotonic()-start)
        response.headers['X-Content-Type-Options']='nosniff'; response.headers['Referrer-Policy']='no-referrer'; response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response
    def role(authorization:str=Header(default=''),x_demo_role:str=Header(default='operator')):
        if cfg.mode=='demo':
            if x_demo_role not in ('operator','approver'): raise HTTPException(400,'Invalid demo role')
            return x_demo_role
        token=authorization.removeprefix('Bearer ')
        if authorization.startswith('Bearer ') and hmac.compare_digest(token,cfg.operator): return 'operator'
        if authorization.startswith('Bearer ') and hmac.compare_digest(token,cfg.approver): return 'approver'
        raise HTTPException(401,'Valid role token required')
    def require(actor,expected):
        if actor!=expected: raise HTTPException(403,f'{expected} role required')
    @api.get('/api/config')
    def config(): return {'mode':cfg.mode,'llm_configured':bool(cfg.llm_key),'live_restart_enabled':cfg.live_write,'allowed_targets':sorted(cfg.allow)}
    @api.get('/health')
    def health(): return {'status':'ok','mode':cfg.mode}
    @api.get('/metrics')
    def metrics(actor=Depends(role)): return Response(generate_latest(registry),media_type=CONTENT_TYPE_LATEST)
    @api.get('/api/incidents')
    def incidents(actor=Depends(role)):
        with db_conn(cfg) as c: return [get_incident(c,r['id']) for r in c.execute('SELECT id FROM incidents ORDER BY created DESC LIMIT 100')]
    @api.post('/api/incidents',status_code=201)
    def analyze(t:Target,actor=Depends(role)):
        require(actor,'operator'); check_target(cfg,t); result=workflow(cfg,t); iid=str(uuid4()); now=time.time()
        status='pending' if result['decision']['action']!='none' else 'observed'
        payload={'target':t.model_dump(),'mode':cfg.mode,**result}
        with db_conn(cfg) as c:
            c.execute('INSERT INTO incidents VALUES(?,?,?,?,?)',(iid,json.dumps(payload),status,now,now)); audit(c,iid,actor,'analyzed',result['decision']['action']); data=get_incident(c,iid)
        actions.labels('analyzed').inc(); return data
    @api.get('/api/incidents/{iid}/audit')
    def history(iid:str,actor=Depends(role)):
        with db_conn(cfg) as c:
            get_incident(c,iid); return [dict(r) for r in c.execute('SELECT actor,event,detail,at FROM audit WHERE incident_id=? ORDER BY id',(iid,))]
    @api.post('/api/incidents/{iid}/{choice}')
    def transition(iid:str,choice:Literal['approve','reject','execute'],note:Note,actor=Depends(role)):
        require(actor,'operator' if choice=='execute' else 'approver')
        expected='approved' if choice=='execute' else 'pending'; new={'approve':'approved','reject':'rejected','execute':'executing'}[choice]
        with db_conn(cfg) as c:
            c.execute('BEGIN IMMEDIATE'); item=get_incident(c,iid)
            if item['mode']!=cfg.mode: raise HTTPException(409,'Incident mode mismatch')
            if item['status']!=expected: raise HTTPException(409,f'Expected {expected}, found {item["status"]}')
            if choice=='execute':
                if item['decision']['action']!='restart_deployment': raise HTTPException(409,'No allowed action')
                check_target(cfg,Target(**item['target']))
                if cfg.mode=='live' and not cfg.live_write: raise HTTPException(403,'Live restart disabled')
                if time.time()-item['updated']>900: raise HTTPException(409,'Approval expired; create a fresh incident')
            c.execute('UPDATE incidents SET status=?,updated=? WHERE id=?',(new,time.time(),iid)); audit(c,iid,actor,choice,note.reason)
        actions.labels(choice).inc()
        if choice=='execute':
            t=Target(**item['target']); outcome='executed'; detail='Simulated rolling restart requested; no real infrastructure changed.'
            if cfg.mode=='live':
                try:
                    # Recheck readiness before changing a real deployment.
                    fresh=collect(cfg,t)
                    if diagnose(fresh)['action']!='restart_deployment': outcome='skipped'; detail='Fresh evidence no longer supports restart.'
                    else:
                        run_kube(cfg,['-n',t.namespace,'rollout','restart','deployment',t.deployment]); detail='Rolling restart request accepted; verify rollout status separately.'
                except Exception as e:
                    outcome='failed'; detail='Restart request failed or timed out; verify cluster state before retrying.'; log.warning('execution failure %s',type(e).__name__)
            with db_conn(cfg) as c:
                c.execute('UPDATE incidents SET status=?,updated=? WHERE id=?',(outcome,time.time(),iid)); audit(c,iid,actor,outcome,detail)
            actions.labels(outcome).inc()
        with db_conn(cfg) as c: return get_incident(c,iid)
    @api.get('/')
    def index(): return FileResponse(ROOT/'static'/'index.html')
    api.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    return api
