# InfraOps Assistant

An infrastructure operations prototype with a React dashboard, Python/FastAPI REST API, optional LangGraph orchestration and LLM explanations, SQLite incident history, separate operator and approver roles, and Prometheus application metrics.

## Quick start (macOS / Linux)

Use Python 3.11 or 3.12. No AWS account, Kubernetes cluster, Node build, or API key is required for the offline demo after installing dependencies.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
uvicorn app.server:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Alternatively use `python3 -m venv .venv` and `pip install -r requirements.txt`.

### Demo walkthrough

1. Keep Operator selected; choose Unhealthy replicas and click Analyze.
2. Inspect simulated CPU and replica readiness, policy decision, and suggested investigation.
3. Switch Role to Approver; enter a reason and click Approve (or Reject).
4. Switch back to Operator; click Execute approved restart. The demo records a simulated restart without modifying anything.
5. Read the audit trail, then try High CPU and Healthy scenarios, which do not propose restarts. Refresh to verify persistence.

### Optional LLM and LangGraph

```bash
uv pip install -r requirements-agent.txt
export LLM_API_KEY='YOUR_PROVIDER_KEY'
export LLM_BASE_URL='https://api.groq.com/openai/v1'
export LLM_MODEL='openai/gpt-oss-20b'
uvicorn app.server:app --host 127.0.0.1 --port 8000
```

Use an available model supporting JSON-object responses on your OpenAI-compatible provider. The key is server-side; never commit it. Without a key the assistant uses deterministic explanation rules. A failed or malformed model response falls back to rules. If LangGraph is installed, a graph runs collection → diagnosis → explanation; otherwise the same three stages run in Python. LLM output is validated with Pydantic and cannot choose or execute operations. The dashboard labels the actual workflow engine and explanation provider. Only metric evidence and policy decision are sent to the provider; get authorization before sending live telemetry to a third party.

### Optional live connections (test environment only)

Requires kubectl, an explicitly selected non-production context, a namespace/deployment allowlist and separate role tokens. Use a Kubernetes identity with read-only access initially, add narrowly scoped patch rights only for the allowlisted deployment if testing restarts. Configure an authenticated AWS profile separately with `ec2:DescribeInstances` if needed. No AWS writes are implemented. Prometheus must expose the standard cAdvisor and kube-state-metrics series; CPU query estimates usage relative to configured limits, not host CPU utilization. Missing values remain unknown.

```bash
export OPS_MODE=live
export KUBE_CONTEXT='your-test-context'
export KUBE_ALLOWED_TARGETS='demo/payments-api'
export OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export APPROVER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
# Optional, after configuring access:
export PROMETHEUS_URL=http://127.0.0.1:9090
# export AWS_REGION=ap-south-1
export ENABLE_LIVE_RESTART=false
uvicorn app.server:app --host 127.0.0.1 --port 8000
```

In the dashboard enter the appropriate token for each role (retrieve your tokens in your own terminal without sharing them). Enable live restart only after reviewing permissions, target and test cluster: `export ENABLE_LIVE_RESTART=true`, then restart the server. Approval expires after 15 minutes; execution rechecks readiness before issuing a fixed `kubectl rollout restart deployment` command. The response confirms a request, not completed recovery. If execution crashes or times out, verify cluster state manually rather than automatically retrying. Do not deploy publicly; this is not a hardened multi-user production service. Shared role tokens are not individual identity/SSO; use TLS, proper identity, authorization, tamper-resistant audit logs and secrets management for a real deployment.

### API and observability

| Route | Purpose |
|---|---|
| GET /health | Liveness |
| GET /api/config | Mode and feature flags |
| GET /api/incidents | Recent incidents |
| POST /api/incidents | Analyze namespace, deployment and scenario |
| POST /api/incidents/{id}/approve | Approver decision with reason |
| POST /api/incidents/{id}/reject | Approver rejection |
| POST /api/incidents/{id}/execute | Operator action after approval |
| GET /api/incidents/{id}/audit | Audit history |
| GET /metrics | Prometheus request counts, latency, workflow transitions |
| GET /openapi.json | API specification |

Live endpoints require the appropriate Bearer token; demo uses X-Demo-Role (not real authentication). Example: `curl -X POST http://127.0.0.1:8000/api/incidents -H 'Content-Type: application/json' -d '{"scenario":"unhealthy"}'`.

### Tests and build

```bash
uv pip install -r requirements-dev.txt
python -m pytest -q
node --check static/app.js # optional, if Node is installed
```

Tests cover approval/rejection, role enforcement, duplicate execution, persistence, expiry, unknown evidence, invalid input, metrics, and LLM fallback. Live Kubernetes restart is mocked in tests. The CI workflow is supplied but has not run on GitHub. The optional Dockerfile builds the demo only; it has not been built in the authoring environment.

### Architecture and limits

```text
React dashboard → FastAPI → collect evidence → fixed policy → optional LLM explanation
                      ↓                         ↓
                 SQLite audit          pending approval
                                              ↓
                                   approver → operator
                                              ↓
                           simulated restart / allowlisted kubectl
```

React 16 UMD assets are bundled for offline operation; upgrade to a maintained React version before external deployment. Accessibility-oriented labels, keyboard focus, status announcements and responsive layout are included; WCAG compliance has not been audited. The live connectors and optional provider/LangGraph path need environment-specific integration testing. No Grafana dashboard, RAG, production deployment or real incident remediation is claimed. Demo data is synthetic. The rule-based recommendation is not a root-cause diagnosis. AI-generated starter code should be reviewed and understood before representing it as your own work.

### Learning order

1. Read `app/main.py`: models, REST endpoints and SQLite.
2. Understand `collect`, `diagnose`, `explain`, and `workflow`.
3. Trace role authorization and approval state transitions.
4. Read `static/app.js`: React state, form, API calls and audit UI.
5. Run tests and add your own scenarios, then configure the optional LLM and test cluster.
