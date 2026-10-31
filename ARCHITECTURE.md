# AI Cloud Server — Architecture

A self-hosted, single-node AI platform running a local LLM on an **NVIDIA RTX Quadro 8000 (48 GB)**,
with a **LangGraph** agent orchestrator, an app-managed **Vault**, a **storage access-control** module,
and a full observability stack.

---

## 1. Hardware & model layer

| Item | Choice | Notes |
|------|--------|-------|
| GPU | RTX Quadro 8000, 48 GB | Turing, no FP8 tensor cores — prefer AWQ/GPTQ **INT4** or FP16 |
| Model (default) | **26B / A4B MoE** (~4B active) | INT4 weights ≈ 13–15 GB; only active experts compute per token → fast |
| Model (option) | NVIDIA Nemotron | swap via `LLM_MODEL` in `.env`; no code change |
| Server | **vLLM** | OpenAI-compatible API at `:8001`, high concurrent throughput |

### VRAM budget (26B-A4B, INT4-AWQ, 48 GB)
- Weights: ~13–15 GB (all experts must be resident even though only ~4B activate per token)
- KV cache: large remainder → long context / many concurrent requests
- Launch: `--quantization awq --max-model-len 8192 --gpu-memory-utilization 0.90`

> Turing lacks FP8; do **not** use `--quantization fp8`. Use AWQ/GPTQ INT4 or plain FP16.
> A MoE keeps all expert weights in VRAM (so plan for the full ~26B footprint) but activates only a
> subset per token, cutting compute/latency versus a dense model of the same size.

---

## 2. Component map

```
                           ┌────────────────────────────────────────────┐
   client ── HTTPS ──▶     │  FastAPI Gateway (:8000)                     │
                           │   • auth (JWT)  • rate limit  • OTel traces  │
                           └───────┬───────────────────────┬─────────────┘
                                   │                        │
                     ┌─────────────▼─────────┐   ┌──────────▼───────────┐
                     │  Orchestrator         │   │  Admin / ACL API      │
                     │  (LangGraph)          │   │  users, policies,     │
                     │   • state graph       │   │  secrets (vault)      │
                     │   • agent nodes       │   └──────────┬───────────┘
                     │   • tool calls        │              │
                     │   • PG checkpointer   │              │
                     └───┬───────┬───────┬───┘              │
                         │       │       │                  │
          ┌──────────────▼┐  ┌───▼────┐ ┌▼────────────┐ ┌───▼──────────┐
          │ vLLM (:8001)  │  │ Vault  │ │ ACL engine  │ │ Storage      │
          │ local LLM     │  │ (AES-  │ │ (PDP)       │ │ Postgres +   │
          │               │  │  GCM)  │ │             │ │ MinIO        │
          └───────────────┘  └────────┘ └─────────────┘ └──────────────┘

   Cross-cutting: Redis (queue/cache) · OpenTelemetry → Prometheus (metrics)
                  · Loki (logs) · Grafana (dashboards)
```

---

## 3. Services (docker-compose)

| Service | Port | Role |
|---------|------|------|
| `gateway` | 8000 | FastAPI app: API, auth, orchestrator, ACL, vault |
| `vllm` | 8001 | Local LLM inference (GPU) |
| `postgres` | 5432 | Relational: users, ACL, vault blobs, LangGraph checkpoints |
| `redis` | 6379 | Task queue, response cache, distributed locks, rate limiting |
| `minio` | 9000/9001 | S3-compatible object store for documents/artifacts |
| `otel-collector` | 4317/4318 | Receives OTLP traces/metrics, fans out |
| `prometheus` | 9090 | Scrapes metrics (gateway + collector + node) |
| `loki` | 3100 | Log aggregation |
| `grafana` | 3000 | Dashboards over Prometheus + Loki + traces |

---

## 4. Agent orchestrator (LangGraph)

- **State** (`orchestrator/state.py`): typed `TypedDict` — messages, user/subject, scratchpad, tool results, ACL decisions.
- **Graph** (`orchestrator/graph.py`): `router → agent → tools → agent → …` with conditional edges; compiled with a **Postgres checkpointer** so every run is resumable and auditable.
- **Nodes** (`orchestrator/nodes.py`): the LLM-backed agent(s); call vLLM via the OpenAI-compatible client.
- **Tools** (`orchestrator/tools.py`): each tool call is **ACL-gated** — the tool asks the ACL engine "can subject X do action Y on resource Z?" before touching storage/vault.
- Checkpoints + thread IDs give per-conversation memory and replay.

---

## 5. Vault (app-managed, envelope encryption)

SQLCipher/age-style confidentiality without a separate daemon, cross-platform:

- A **master key** (`VAULT_MASTER_KEY`, 32 bytes, base64) lives only in env / an OS keyring — never in the DB.
- Each secret is encrypted with **AES-256-GCM** under a per-secret random data key; the data key is wrapped by the master key (envelope encryption).
- Ciphertext + nonce + wrapped key stored in Postgres `vault_secrets`.
- Rotation: re-wrap data keys under a new master key without re-encrypting payloads.
- All reads/writes are ACL-gated and audit-logged.

> Trade-off vs HashiCorp Vault: no dynamic secrets / leasing. Interface (`vault/interface.py`) is swappable if you later move to Vault/Infisical.

---

## 6. Storage access-control module

- **Model** (`acl/models.py`): `Subject` (user/agent/service), `Resource` (`type:id`, e.g. `object:reports/q3.pdf`), `Action` (`read|write|delete|list|admin`), `Policy` (allow/deny rules, role bindings).
- **Engine** (`acl/engine.py`): a Policy Decision Point — `check(subject, action, resource) -> Decision`. Deny-by-default, explicit deny overrides allow, role + direct grants, resource prefix matching.
- **Enforcement (PEP)**: every storage/vault/tool access routes through `engine.check(...)`; denials raise `403` and are audit-logged.
- Backed by Postgres (`acl_policies`, `acl_role_bindings`); decisions emit OTel spans + metrics.

---

## 7. Observability

- **Traces:** OpenTelemetry SDK in the gateway auto-instruments FastAPI + HTTP + SQLAlchemy; spans for each graph node, tool call, LLM call, and ACL decision. Exported OTLP → collector → (Tempo/Jaeger optional) and Grafana.
- **Metrics:** Prometheus scrapes `/metrics` (request latency, token counts, GPU proxy metrics, ACL allow/deny, queue depth).
- **Logs:** structured JSON logs → Loki (via collector or Promtail) → Grafana.
- **Dashboards:** Grafana provisioned with Prometheus + Loki datasources.

---

## 8. Request lifecycle (chat)

1. Client → `POST /v1/chat` with JWT.
2. Gateway authenticates, opens an OTel trace, resolves the `Subject`.
3. Orchestrator loads/creates a thread checkpoint from Postgres.
4. Graph runs: agent node calls vLLM; tool nodes call ACL-gated storage/vault.
5. State is checkpointed after each step (resumable).
6. Response streamed back; metrics/logs/traces emitted throughout.

---

## 9. Security posture

- Deny-by-default ACL; secrets never logged; master key out of DB.
- JWT auth on all routes; admin routes require `admin` action grant.
- MinIO + Postgres credentials themselves stored in the vault / env, not source.
- Audit log table for every vault access and ACL decision.

---

## 10. Layout

```
ai-platform/
├── ARCHITECTURE.md          # this file
├── README.md                # quickstart
├── docker-compose.yml       # full stack
├── .env.example
├── pyproject.toml
├── app/
│   ├── config.py            # settings (pydantic)
│   ├── main.py              # FastAPI app + lifespan + OTel
│   ├── observability.py     # OTel/metrics setup
│   ├── api/                 # routes + auth deps
│   ├── llm/                 # vLLM client
│   ├── orchestrator/        # LangGraph graph/state/nodes/tools/checkpoint
│   ├── vault/               # interface + AES-GCM store
│   ├── acl/                 # models + engine + store
│   ├── storage/             # MinIO objects + repos
│   ├── auth/                # users + jwt
│   └── db/                  # SQLAlchemy session + models
├── deploy/
│   ├── prometheus.yml
│   ├── otel-collector.yaml
│   ├── loki-config.yaml
│   └── grafana/             # provisioning
└── scripts/
    └── init_db.py
```
