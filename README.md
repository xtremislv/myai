# AI Platform

Self-hosted AI cloud server: a local LLM on a **rented NVIDIA cloud GPU** (hosted on
[aiccloud.in](https://aiccloud.in/)), served by **vLLM**, with a **LangGraph** agent orchestrator, an
app-managed **vault**, a **storage access-control** module, and a full **observability** stack.

**Docs:**
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — full design
- [`docs/CLOUD_SETUP_GUIDE.md`](./docs/CLOUD_SETUP_GUIDE.md) — provision & deploy on aiccloud.in
- [`docs/USER_GUIDE.md`](./docs/USER_GUIDE.md) — how users authenticate & send requests
- [`docs/PROGRESS_AND_ROADMAP.md`](./docs/PROGRESS_AND_ROADMAP.md) — status, departments, Request Manager, plan

## Stack

| Layer | Tech |
|-------|------|
| Inference | vLLM (OpenAI-compatible, GPU) |
| Orchestration | LangGraph (+ Postgres checkpoints) |
| API | FastAPI |
| Vault | AES-256-GCM envelope encryption (app-managed) |
| Access control | Policy engine (deny-by-default) over Postgres |
| Data | Postgres + Redis + MinIO |
| Observability | OpenTelemetry → Prometheus / Loki / Grafana |

## Quickstart

```bash
cd ai-platform
cp .env.example .env

# 1. generate a vault master key and paste into .env (VAULT_MASTER_KEY)
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"

# 2. bring up the stack
docker compose up -d --build

# 3. bootstrap DB + first admin + baseline policies
docker compose exec gateway python -m scripts.init_db --admin-pass 'change-me'

# 4. log in
curl -s localhost:8000/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"change-me"}'
```

## Endpoints

| URL | What |
|-----|------|
| `http://localhost:8000/docs` | FastAPI OpenAPI UI |
| `http://localhost:8000/healthz` | health |
| `http://localhost:8000/metrics` | Prometheus metrics |
| `http://localhost:8001/v1` | vLLM OpenAI API |
| `http://localhost:9001` | MinIO console |
| `http://localhost:9090` | Prometheus |
| `http://localhost:3000` | Grafana — dashboards: *LLM & ACL*, *Diagnostics & Logs* |
| `http://localhost:3100` | Loki |

## Using it

```bash
TOKEN=...   # from /v1/auth/login

# grant a role access to some objects
curl -s localhost:8000/v1/admin/policies -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"subject":"analyst","effect":"allow","action":"read","resource":"object:reports/*"}'

# store a secret in the vault
curl -s -X PUT localhost:8000/v1/admin/secrets -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name":"openai_key","value":"sk-..."}'

# talk to the orchestrated agent
curl -s localhost:8000/v1/chat -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"thread_id":"demo-1","message":"List the documents under reports/ and summarize q3."}'
```

## Model notes (rented cloud GPU)

- **Default: a 26B / A4B MoE** (~4B active params per token). At INT4 the weights are ~13–15 GB — a
  comfortable fit on a 48 GB L40S (and roomy on A100/H100 80 GB) — and only the active experts compute
  per token, so throughput is well above a dense 27B.
- **Switch to Nemotron** by setting `LLM_MODEL` (and usually `HUGGING_FACE_HUB_TOKEN`) in `.env`. No
  code change needed — the client talks to whatever vLLM serves.
- **FP8 depends on the card:** available on Ada/Hopper (L40S, RTX 4090, H100); **not** on Ampere
  (A100, RTX 3090) — use `awq`/`gptq` (INT4) there. The compose default `awq` works everywhere.
- Set the exact Hugging Face repo id for your chosen weights in `LLM_MODEL`.
- GPU selection + full deploy steps: [`docs/CLOUD_SETUP_GUIDE.md`](./docs/CLOUD_SETUP_GUIDE.md).

## Local dev without Docker

```bash
pip install -e ".[dev]"
# point .env at localhost services, then:
uvicorn app.main:app --reload
pytest
```
