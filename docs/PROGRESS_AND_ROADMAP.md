# AI Platform — Progress & Roadmap

**Date:** 2026-10-31
**Target host:** single node, NVIDIA RTX Quadro 8000 (48 GB), Turing
**Default model:** 26B / A4B MoE (`LLM_MODEL=gemma-3-26b-a4b-it`) · Nemotron switchable via `.env`

---

## 1. Executive summary

The foundation is built and tested: a FastAPI gateway, a LangGraph orchestrator (single-agent **and**
multi-agent team), an app-managed encrypted vault, a deny-by-default access-control engine, object/data
storage, and a full observability stack — all wired into `docker-compose` with a provisioned Grafana
dashboard. **7/7 unit tests pass.**

What remains is: (1) hardening the scaffold into production code, (2) standing up the model on the GPU,
(3) building **department agents** (finance, logistics, research, misc, admin), (4) building the
**Request Manager** that classifies, authorizes, segregates, and tracks every request department-wise,
and (5) a staged deployment. This document is the step-by-step plan for all of it.

---

## 2. Current progress (done)

| Area | Status | Where |
|------|--------|-------|
| Project scaffold + docs | ✅ | `ARCHITECTURE.md`, `README.md` |
| FastAPI gateway (auth/chat/admin, `/metrics`, `/healthz`) | ✅ | `app/main.py`, `app/api/` |
| JWT auth + bcrypt | ✅ | `app/auth/` |
| Single-agent LangGraph loop (Postgres checkpoints) | ✅ | `app/orchestrator/graph.py` |
| Multi-agent team (planner→researcher→writer) | ✅ | `app/orchestrator/multi_graph.py`, `agents.py` |
| ACL-gated tools | ✅ | `app/orchestrator/tools.py` |
| Vault (AES-256-GCM envelope + rotation) | ✅ | `app/vault/aesgcm_vault.py` |
| ACL engine (deny-by-default, globs, deny-override, audit) | ✅ | `app/acl/engine.py` |
| Object storage (MinIO, ACL-enforced) | ✅ | `app/storage/objects.py` |
| vLLM service + GPU reservation in compose | ✅ | `docker-compose.yml` |
| Observability: OTel → Prometheus/Loki/Grafana | ✅ | `app/observability.py`, `deploy/` |
| Grafana dashboard (LLM/ACL/logs) provisioned | ✅ | `deploy/grafana/` |
| Tests (vault, ACL, team graph) — 7 passing | ✅ | `tests/` |

**Not yet built:** DB migrations, streaming, rate limiting, department agents, the Request Manager,
human-in-the-loop approvals, TLS/reverse proxy, backups, CI, load/security testing, real model weights.

---

## 3. What needs to be done — step by step

Phases are ordered by dependency. Each task has an acceptance check (AC).

### Phase 1 — Harden the core
1. **DB migrations (Alembic).** Replace `create_all` with versioned migrations. *AC:* `alembic upgrade head` builds all tables from empty DB.
2. **Config & secrets hygiene.** Require `JWT_SECRET` and `VAULT_MASTER_KEY` at boot; fail fast if default/missing. Load DB/MinIO creds from the vault or Docker secrets, not plain `.env` in prod. *AC:* boot aborts with a clear error when a required secret is unset.
3. **Readiness vs liveness.** Split `/healthz` (liveness) from `/readyz` (checks Postgres, Redis, MinIO, vLLM reachable). *AC:* `/readyz` returns 503 until all deps are up.
4. **Structured error handling.** Global exception handler → JSON error + trace id; map `PermissionError` → 403. *AC:* ACL denial returns 403 with the reason and a correlation id.
5. **Streaming responses (SSE).** Stream tokens from vLLM through the gateway for `/v1/chat`. *AC:* client receives incremental tokens.
6. **Redis rate limiting.** Per-subject token bucket middleware. *AC:* over-limit requests get 429.

### Phase 2 — Model serving on the Quadro 8000
7. **Acquire weights & set exact repo id.** Put the real HF repo id in `LLM_MODEL` (MoE 26B/A4B); set `HUGGING_FACE_HUB_TOKEN` if gated. *AC:* `docker compose up vllm` loads the model.
8. **Quantize (INT4 AWQ/GPTQ).** Ensure weights fit ~13–15 GB; **no FP8** on Turing. *AC:* model resident with headroom for KV cache at `--gpu-memory-utilization 0.90`.
9. **Benchmark.** Measure tokens/s and concurrency; tune `--max-model-len` and `--max-num-seqs`. *AC:* documented throughput at the target concurrency.
10. **Nemotron switch test.** Confirm swapping `LLM_MODEL` to Nemotron works with no code change. *AC:* both models serve the same API.

### Phase 3 — Department agents (see §5)
11. **Tool registry per department.** Scope each department's agents to only its tools + data prefixes. *AC:* a finance agent cannot call a logistics-only tool.
12. **Department team graphs.** Reuse the team graph with department-specific prompts/tools. *AC:* each department has its own runnable graph.
13. **Human-in-the-loop approval node.** For critical actions (finance transactions), pause the graph for approval before executing. *AC:* a transaction is not executed until approved; LangGraph checkpoint resumes on approval.

### Phase 4 — Request Manager (see §6)
14. **Request classification + routing.** Resolve department from JWT/role (and optional classifier), route to the right team. *AC:* a finance request runs the finance team only.
15. **Department policy packs.** Seed ACL policies per department (resource prefixes, actions). *AC:* cross-department data access is denied by default.
16. **Per-department quotas & concurrency.** Redis-backed limits per department. *AC:* a department over quota is throttled without affecting others.
17. **Critical-data guard.** Explicit allow-list of critical resources (`secret:payment/*`, etc.) with extra checks/approval. *AC:* critical reads require elevated grant + audit entry.

### Phase 5 — Observability depth
18. **Per-department dashboard.** Requests, denials, latency, quota usage, approvals pending — grouped by department. *AC:* Grafana shows a row per department.
19. **Alerts.** Prometheus alert rules: spike in denials, quota exhaustion, vLLM down, queue backlog. *AC:* alert fires in Grafana/Alertmanager on condition.
20. **Trace backend (optional).** Add Tempo/Jaeger for full trace view. *AC:* a request trace shows gateway→RM→graph→tool spans.

### Phase 6 — Deployment (see §7)
21. Reverse proxy + TLS, secrets, backups, GPU driver/toolkit, restart policy, log retention. *AC:* clean bring-up on the host from a documented runbook.

### Phase 7 — Quality gates
22. **CI** (ruff + pytest) on every push. *AC:* green pipeline required to merge.
23. **Load test** (locust/k6) at expected concurrency. *AC:* p95 latency within target under load.
24. **Security review.** Secret handling, authz coverage, dependency scan, audit-log completeness. *AC:* checklist signed off.

---

## 4. Setting up agents for multiple tasks

The orchestrator already supports two execution shapes; departments will use the **team** shape with
scoped tools:

- **Single-agent loop** — fast path for simple Q&A/tool calls.
- **Team** — `planner → researcher → (tools)* → writer` for multi-step tasks.

**Per-department wiring (Phase 3):**
- Each department gets a **tool registry** (a subset of `TOOLS`) and a **system prompt** describing its
  remit and limits.
- Each department runs as a **role** in the ACL: policies bind the role to its data prefixes.
- A **department agent** = a compiled team graph parameterized by `(tool_registry, prompt, acl_role)`.
- Critical actions route through a **human-in-the-loop approval** node before any side effect.

---

## 5. Department model

| Department | Responsibilities | Primary tools (to build) | Data scope (ACL) | Sensitivity |
|-----------|------------------|--------------------------|------------------|-------------|
| **Finance** | Run/initiate transactions, manage invoices, reconcile | `create_invoice`, `get_invoice`, `initiate_payment` (approval-gated), `ledger_query` | `object:finance/*`, `secret:payment/*` | **Critical** |
| **Logistics** | Shipment tracking, notifications, reminders, inventory | `track_shipment`, `send_notification`, `set_reminder`, `inventory_lookup`, `inventory_adjust` | `object:inventory/*`, `object:shipments/*` | Medium |
| **Research** | Information gathering, synthesis, reports | `read_document`, `list_documents`, `web_search` (if enabled), `summarize` | `object:research/*`, `object:reports/*` | Low–Medium |
| **Misc** | General/uncategorized tasks | general read-only tools | `object:shared/*` | Low |
| **Admin** | Manage users, policies, secrets, departments | admin API + `*` | `*` | **Critical** |

**Rules**
- Deny-by-default across departments; a department sees only its own prefixes unless explicitly granted.
- Finance **transactions** and all **critical-data** reads require an approval step + elevated grant.
- Admin is the only role with cross-department and policy-management rights.

---

## 6. Request Manager — design & build plan

The Request Manager (RM) is the control plane that sits between the gateway and the orchestrator. It
**classifies** each request to a department, **authorizes** it, **segregates** it (data + agents +
quotas), **routes** it to the right department team, gates **critical** actions, and **emits** per-
department telemetry for the dashboard.

### 6.1 Pipeline

```
client ─▶ Gateway (JWT auth) ─▶ REQUEST MANAGER ───────────────────────────▶ Department team (LangGraph)
                                  │                                              │
                                  ├─ 1. resolve department (JWT role / classifier)
                                  ├─ 2. authorize: dept allowed for this action class?  (ACL)
                                  ├─ 3. quota/concurrency check            (Redis)
                                  ├─ 4. bind scoped tool registry + ACL role
                                  ├─ 5. critical action? → approval gate   (human-in-the-loop)
                                  └─ 6. emit span + metrics tagged by dept (OTel/Prom)
                                                                               │
                                        audit log  ◀───────────────────────────┘
```

### 6.2 Responsibilities (maps to the user's requirements)

| Requirement | How the RM delivers it |
|-------------|------------------------|
| *"distinguish and limit access to critical data"* | Department resolution + ACL critical-data allow-list; critical reads need elevated grant + approval; secrets stay in the vault |
| *"segregate the request department-wise"* | Each request is tagged with a department; routing + ACL role + scoped tool registry isolate departments |
| *"track all this pipeline and flow in the dashboard"* | Every stage emits an OTel span and Prometheus counters/histograms labeled `department`; Grafana shows per-department rows |
| *"finance team … transactions / invoices"* | Finance team + approval-gated `initiate_payment`, invoice tools, `secret:payment/*` |
| *"logistics … tracing, notifying, reminders, inventory"* | Logistics team + tracking/notification/reminder/inventory tools |
| *"research team … misc … admin access"* | Research/Misc/Admin teams with scoped policies; Admin manages the rest |

### 6.3 New components to build

1. `app/request_manager/classifier.py` — resolve department from JWT role claim first; optional LLM/keyword classifier fallback for unlabeled requests; default to `misc`.
2. `app/request_manager/policies.py` — department → `{allowed_actions, data_prefixes, tools, quota, requires_approval}` policy packs (seeded into ACL + a `departments` table).
3. `app/request_manager/quota.py` — Redis token-bucket per department (rate + max concurrency).
4. `app/request_manager/router.py` — select and run the department's team graph with the scoped registry + ACL subject (department role).
5. `app/request_manager/approval.py` — LangGraph interrupt/approval gate; pending approvals persisted; `/v1/approvals` endpoints for admins to approve/deny; graph resumes from checkpoint.
6. `app/api/routes_request.py` — single entry `POST /v1/request` that drives the RM end-to-end; plus `/v1/approvals`.
7. **Metrics:** `requests_total{department,outcome}`, `request_latency_seconds{department}`, `quota_rejections_total{department}`, `approvals_pending{department}`, `critical_access_total{department,resource}`.

### 6.4 Build order
RM depends on Phase 1 (hardening) + Phase 3 (department agents). Build in the order:
policies → classifier → router → quota → approval → endpoints → metrics → dashboard rows (§5 Phase 18).

---

## 7. Deployment plan

**Strategy:** single-node first (the Quadro box is the whole platform), then optionally split stateful
services later. All services already defined in `docker-compose.yml`.

### 7.1 Host prerequisites
1. OS + NVIDIA driver for Turing (data-center/studio driver supporting CUDA ≥ 12.x).
2. Docker + **NVIDIA Container Toolkit** (so the `vllm` container sees the GPU).
3. Verify: `docker run --rm --gpus all nvidia/cuda:12.4.0-base nvidia-smi` lists the Quadro 8000.

### 7.2 Secrets & config
4. `cp .env.example .env`; generate `VAULT_MASTER_KEY` (32 bytes base64) and a long `JWT_SECRET`.
5. Set the real `LLM_MODEL` repo id (+ `HUGGING_FACE_HUB_TOKEN` if gated).
6. Move DB/MinIO credentials to Docker secrets or the vault for production (not committed).

### 7.3 Bring-up
7. `docker compose up -d --build`.
8. `docker compose exec gateway python -m scripts.init_db --admin-pass '<strong>'` (first admin + baseline policies).
9. Seed department policy packs (Phase 15).
10. Smoke test: `/readyz`, login, a chat in `team` mode, a Grafana panel updating.

### 7.4 Edge, TLS, persistence
11. Put a reverse proxy in front (Caddy or Traefik) terminating **TLS**; expose only 443 publicly. Keep 8001/9000/9090/3100 internal.
12. Confirm named volumes for `pg-data`, `minio-data`, `prom-data`, `loki-data`, `grafana-data`, `hf-cache`.
13. **Backups:** nightly `pg_dump` + MinIO bucket snapshot to off-box storage; test restore.
14. **Restart policy:** already `unless-stopped`; add a `docker compose` systemd unit for boot start.
15. **Log retention:** set Loki + Prometheus retention to fit disk.

### 7.5 Operate
16. Grafana alerts (Phase 19) → notify (email/Slack/webhook).
17. Runbook: how to roll the model, rotate the vault master key (`AESGCMVault.rotate_master_key`), rotate JWT secret, restore from backup.
18. Capacity: watch GPU memory/util and per-department quota usage; adjust `--max-num-seqs` and quotas.

### 7.6 Later (scale-out, optional)
- Move Postgres/Redis/MinIO to dedicated hosts or managed equivalents.
- Add a second GPU node and load-balance vLLM; make the gateway stateless behind the proxy.

---

## 8. Suggested timeline (relative)

| Milestone | Scope | Rough effort |
|-----------|-------|--------------|
| **M1 — Hardened core** | Phase 1 | ~1 week |
| **M2 — Model live** | Phase 2 | ~2–4 days (driver/quantize/benchmark) |
| **M3 — Department agents** | Phase 3 | ~1–1.5 weeks |
| **M4 — Request Manager** | Phase 4 + dashboards | ~1.5–2 weeks |
| **M5 — Production deploy** | Phase 6 | ~3–5 days |
| **M6 — Quality gates** | Phases 5 & 7 | ~1 week, ongoing |

---

## 9. Immediate next actions (this week)

1. Add **Alembic** migrations and boot-time secret validation (Phase 1.1–1.2).
2. Add **CI** (ruff + pytest) so every change stays green (Phase 22).
3. Acquire the real **model weights** and bring vLLM up on the GPU (Phase 2.7–2.8).
4. Scaffold the **department policy packs** + `departments` table (Phase 15) — unblocks both agents and the RM.

> Say the word and I'll start with any of these — the Request Manager scaffold (`app/request_manager/`)
> and the department policy packs are the highest-leverage next build.
