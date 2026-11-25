import time
from fastapi.testclient import TestClient
from app.main import Config, create_app, db_conn, diagnose, Explanation
import pytest

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('OPS_MODE','demo'); monkeypatch.setenv('OPS_DB',str(tmp_path/'test.db')); monkeypatch.delenv('LLM_API_KEY',raising=False)
    return TestClient(create_app())

def new(c,scenario='unhealthy'):
    r=c.post('/api/incidents',json={'scenario':scenario}); assert r.status_code==201; return r.json()

def step(c,i,action,role='operator'):
    return c.post(f'/api/incidents/{i["id"]}/{action}',json={'reason':'Reviewed'},headers={'X-Demo-Role':role})

def test_approved_workflow(client):
    i=new(client); assert i['status']=='pending' and i['evidence']['source']=='simulated'
    assert step(client,i,'execute').status_code==409
    assert step(client,i,'approve').status_code==403
    assert step(client,i,'approve','approver').json()['status']=='approved'
    assert step(client,i,'execute','approver').status_code==403
    assert step(client,i,'execute').json()['status']=='executed'
    assert step(client,i,'execute').status_code==409
    audit=client.get(f'/api/incidents/{i["id"]}/audit').json(); assert [a['event'] for a in audit]==['analyzed','approve','execute','executed']; assert 'Simulated' in audit[-1]['detail']

def test_reject(client):
    i=new(client); assert step(client,i,'reject','approver').json()['status']=='rejected'; assert step(client,i,'execute').status_code==409

@pytest.mark.parametrize('scenario,severity',[('high_cpu','warning'),('healthy','healthy')])
def test_no_action(client,scenario,severity):
    i=new(client,scenario); assert i['status']=='observed' and i['decision']['severity']==severity; assert step(client,i,'approve','approver').status_code==409

def test_input_validation(client):
    assert client.post('/api/incidents',json={'deployment':'bad;command'}).status_code==422
    assert client.post('/api/incidents',json={},headers={'X-Demo-Role':'approver'}).status_code==403
    assert client.get('/api/incidents/not-found/audit').status_code==404

def test_metrics_and_static(client):
    new(client); assert 'infraops_workflow_events_total' in client.get('/metrics').text; assert 'InfraOps Assistant' in client.get('/').text; assert client.get('/static/app.js').status_code==200

def test_persistence_and_expiry(client):
    i=new(client); step(client,i,'approve','approver'); cfg=Config()
    with db_conn(cfg) as conn: conn.execute('UPDATE incidents SET updated=? WHERE id=?',(time.time()-1000,i['id']))
    second=TestClient(create_app(cfg)); assert second.get('/api/incidents').json()[0]['status']=='approved'; assert step(second,i,'execute').status_code==409

def test_missing_evidence():
    d=diagnose({'ready_replicas':None,'desired_replicas':2,'cpu_percent':99}); assert d['severity']=='unknown' and d['action']=='none'

def test_llm_bad_response_fallback(client,monkeypatch):
    from app import main
    monkeypatch.setenv('LLM_API_KEY','test'); cfg=Config()
    def bad(*a,**kw): raise RuntimeError('timeout')
    monkeypatch.setattr(main.httpx,'post',bad)
    other=TestClient(create_app(cfg)); assert new(other)['explanation']['provider']=='rules_fallback'

def test_live_role_and_allowlist(tmp_path,monkeypatch):
    for k,v in {'OPS_MODE':'live','OPS_DB':str(tmp_path/'live.db'),'KUBE_CONTEXT':'test','KUBE_ALLOWED_TARGETS':'demo/payments-api','OPERATOR_TOKEN':'a'*32,'APPROVER_TOKEN':'b'*32}.items(): monkeypatch.setenv(k,v)
    c=TestClient(create_app()); assert c.get('/api/incidents').status_code==401
    assert c.post('/api/incidents',json={'deployment':'not-allowed'},headers={'Authorization':'Bearer '+'a'*32}).status_code==403
    assert c.post('/api/incidents',json={},headers={'Authorization':'Bearer '+'b'*32}).status_code==403

def test_live_mocked_restart(tmp_path,monkeypatch):
    from app import main
    for k,v in {'OPS_MODE':'live','OPS_DB':str(tmp_path/'live.db'),'KUBE_CONTEXT':'test','KUBE_ALLOWED_TARGETS':'demo/payments-api','OPERATOR_TOKEN':'a'*32,'APPROVER_TOKEN':'b'*32,'ENABLE_LIVE_RESTART':'true'}.items(): monkeypatch.setenv(k,v)
    monkeypatch.delenv('LLM_API_KEY',raising=False)
    commands=[]
    def kube(cfg,args):
        commands.append(args)
        return '{"spec":{"replicas":2},"status":{"readyReplicas":1}}' if 'get' in args else 'restarted'
    monkeypatch.setattr(main,'run_kube',kube)
    c=TestClient(create_app()); op={'Authorization':'Bearer '+'a'*32}; ap={'Authorization':'Bearer '+'b'*32}
    i=c.post('/api/incidents',json={},headers=op).json(); url='/api/incidents/'+i['id']; note={'reason':'reviewed'}
    assert c.post(url+'/execute',json=note,headers=op).status_code==409
    assert not any('restart' in cmd for cmd in commands)
    c.post(url+'/approve',json=note,headers=ap)
    assert c.post(url+'/execute',json=note,headers=op).json()['status']=='executed'
    assert sum('restart' in cmd for cmd in commands)==1
