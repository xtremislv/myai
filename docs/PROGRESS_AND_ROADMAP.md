# AI Platform — Progress & Roadmap

**Date:** 2026-10-31
**Target host:** single rented NVIDIA cloud GPU on [aiccloud.in](https://aiccloud.in/) (L40S 48 GB recommended; A100/H100 also fine)
**Default model:** 26B / A4B MoE (`LLM_MODEL=gemma-3-26b-a4b-it`) · Nemotron switchable via `.env`
**Setup guide:** docs/CLOUD_SETUP_GUIDE.md · **User guide:** docs/USER_GUIDE.md

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
| Department packs + `departments` table + seed (ACL grants) | ✅ | `app/departments/`, `scripts/seed_departments.py` |
| Request Manager: classify, authorize, quota, critical-approval gate | ✅ | `app/request_manager/`, `app/api/routes_request.py` |
| Approvals store + `/v1/request` + `/v1/approvals` API | ✅ | `app/db/session.py` (`approvals`), `routes_request.py` |
| RM per-department metrics + Grafana row | ✅ | `app/observability.py`, `deploy/grafana/` |
| **Data-access schema** (owner-based `$self`, mail categories, inventory, admin exclusions) | ✅ | `app/acl/engine.py`, `app/departments/packs.py` |
| **Per-department tool registries** (Phase 3) | ✅ | `app/orchestrator/tools.py`, `nodes.py`, `agents.py` |
| **NSFW guard** (block + report to hr/admin) & **order/bill forwarding** to accounts inbox | ✅ | `app/request_manager/guards.py` |
| Inbox + reports API; chat-history **3-day retention** | ✅ | `routes_request.py`, `retention.py`, `scripts/purge_history.py` |
| Tests — 32 passing | ✅ | `tests/` |

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

### Phase 3 — Department agents (see §5) — **done**
11. **Tool registry per department.** ✅ `DEPARTMENT_TOOLS` + `tools_for()`; agent prompt and dispatch are department-scoped (`tools.py`, `nodes.py`, `agents.py`). An agent cannot call a tool outside its department.
12. **Department team graphs.** ✅ The single/team graphs run with department context in state; tools + ACL both scope by department.
13. **Human-in-the-loop approval.** ✅ at the RM level (approvals store + API). *Remaining:* move the gate *in-graph* via LangGraph `interrupt()` so it triggers on the actual critical tool call rather than a pre-execution keyword heuristic.

### Phase 4 — Request Manager (see §6) — **largely done**
14. **Request classification + routing.** ✅ Resolve department from JWT/role + explicit hint + keyword fallback (`classifier.py`); router runs the orchestrator with department context. *Remaining:* per-department **tool registries** (depends on Phase 3) so routing also narrows the toolset, not just ACL.
15. **Department policy packs.** ✅ Done (`app/departments/`). Cross-department access denied by default.
16. **Per-department quotas.** ✅ Redis token bucket w/ in-memory fallback (`quota.py`). *Remaining:* max-concurrency limiter (rate done).
17. **Critical-data guard.** ✅ Critical detection + approval gate + `approvals` store + API (`approvals.py`, `routes_request.py`). *Remaining:* replace the pre-execution keyword heuristic with in-graph **LangGraph interrupts** once Phase 3 agents exist.

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

## 5. Department & data model (implemented — see `app/departments/packs.py`)

**Departments (roles):** `account, hr, logistics, researcher, superuser, admin`.

| Department | Responsibilities | Tools | Data scope |
|-----------|------------------|-------|------------|
| **account** | accounts data, balance sheets, finance mail, orders/bills/bookings | `balance_lookup`, `read_mail`, `manage_order`, `manage_bill`, `book_service`, `inventory_lookup` | `accounts:*`, `balance_sheet:*`, `mail:finance/*`, `order/bill/booking:*`, inventory (read) |
| **hr** | career mail, NSFW reports | `read_mail`, `review_report` | `mail:career/*`, `report:nsfw/*` |
| **logistics** | inventory, shipments, notifications, reminders | `inventory_lookup/adjust`, `track_shipment`, `send_notification`, `set_reminder` | `inventory:*` (write), `shipments:*` |
| **researcher** | gather/synthesize/report | `read_document`, `list_documents`, `web_search`, `summarize` | `research:*`, `reports:*` |
| **superuser** | manage inventory | `inventory_lookup/adjust` | `inventory:*` (write) |
| **admin** | manage everything **except** history & personal data | `*` | `*` minus `chat_history:*`, `personal_info:*`, `mail:personal/*` |

**Data types & rules**
- `accounts:*`, `balance_sheet:*` → **account** only.
- `personal_info:<owner>/*`, `mail:personal/<owner>/*`, `chat_history:<owner>/*` → **owner only** (`$self`
  ACL rule). Not even admin can read another user's personal data.
- Mail categories: `finance`→account, `career`→hr, `contact`→admin, `personal`→owner.
- `inventory:*` → **read by anyone** (global `*` grant), **write by logistics + superuser** only.
- **Chat history:** text retained; non-text artifacts purged after **3 days** (`retention.py`).

**Workflow rules**
- Deny-by-default across departments.
- Order/product/service/booking/bill requests → **forwarded to the accounts inbox** (`DepartmentTask`).
- NSFW requests → **blocked and reported to hr + admin** (`Report`).
- Account payments/critical actions → **approval-gated** (RM approvals).

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

**Strategy:** single rented GPU instance on aiccloud.in runs the whole platform; split stateful services
out later if needed. All services already defined in `docker-compose.yml`. **Full step-by-step:
docs/CLOUD_SETUP_GUIDE.md.**

### 7.1 Host prerequisites (rented instance)
1. Provision a GPU on aiccloud.in (see CLOUD_SETUP_GUIDE §2 for card choice) with an Ubuntu 22.04 + CUDA image and ≥ 100 GB disk; add your SSH key.
2. Install Docker + **NVIDIA Container Toolkit** on the instance (so the `vllm` container sees the GPU).
3. Verify: `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` lists the rented GPU.
4. **Billing is per-minute** — stop/terminate the instance when idle; keep off-box backups (§7.4) since the disk may be wiped on terminate.

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

> **Done:** department policy packs + `departments` table + seed (`app/departments/`). **Next:** the
> Request Manager scaffold (`app/request_manager/`).

---

## 10. Build-now vs. build-on-server split

Division line: **pure code/config is built & tested here and pulled on the server; anything needing the
real GPU, real DNS/secrets, or an external vendor account happens on the server (Claude Code in the
instance terminal) or waits on procurement.**

### Build now — in the repo (no GPU/infra; tested with SQLite/in-memory + fakes)
- **Core hardening (Phase 1):** Alembic migrations, boot-time secret validation, `/readyz`, global JSON
  error handler, SSE streaming code, Redis rate-limit middleware, CI (ruff + pytest).
- **Request Manager (Phase 4):** classifier, policy packs *(done)*, router, quota, approval gate,
  `/v1/request` + `/v1/approvals`, per-department metrics.
- **Department agents (Phase 3):** tool registries, prompts, team graphs, approval gate; tools that touch
  only our Postgres/MinIO. External-connector tools get an **interface + stub** now.
- **Ops artifacts:** more dashboards + Prometheus alert rules, Caddy/Cloudflare config templates,
  `pg_dump` backup script, systemd unit, runbooks.

### Build on the server — Claude Code in the instance terminal
- NVIDIA driver + Container Toolkit; GPU-in-container verify.
- Pull/quantize the **real weights**, launch vLLM, choose FP8 vs AWQ **for the rented card**, tune
  `--max-model-len`/`--max-num-seqs`, **benchmark**.
- End-to-end runs against the real model; **prompt/agent-quality tuning**.
- Real TLS + DNS (Cloudflare tunnel / Caddy); confirm vLLM metric scrape + GPU panels show live data.
- Load testing at real concurrency; production secrets; security review of the live deploy.

### Blocked on procurement (not code, not the server)
External accounts before the "wire for real" step: **payment gateway** (finance), **email/SMS provider**
(logistics), any **ERP/inventory source** we don't own, and a **web-search API** (research). Until these
exist, those tools run as stubs.

**Highest-leverage path here:** Request Manager → department agents (stubbed connectors) → Phase-1
hardening + CI. That's ~80% of remaining engineering with zero GPU dependency; the server session then
only does GPU/model/DNS bring-up and live prompt tuning.
