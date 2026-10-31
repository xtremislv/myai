# Handoff — for the next session ("future you")

**Last updated:** 2026-10-31 · **Tests:** 32 passing · **Head:** `09b01b4`

Read this first. It is the single orientation doc for anyone (human or a future Claude Code session,
local or on the server) picking up this project. Pair it with `docs/PROGRESS_AND_ROADMAP.md` (the
detailed plan) — this file is the fast summary + the open questions.

---

## 1. What this project is

A self-hosted AI platform: a local LLM (26B/A4B MoE by default, Nemotron-switchable) served by **vLLM**
on a **rented GPU from aiccloud.in**, fronted by a **FastAPI** gateway, with a **LangGraph** orchestrator
(single-agent + multi-agent team), an app-managed **vault**, a deny-by-default **ACL**, a **Request
Manager** control plane (department routing, quotas, approvals, NSFW/forwarding guards), and a full
**observability** stack (OpenTelemetry → Prometheus / Loki / Grafana).

Everything below the GPU is pure code/config and runs via `docker-compose`.

---

## 2. What is built (done)

| Area | Module(s) | Notes |
|------|-----------|-------|
| Gateway (auth/chat/admin/request, `/metrics`, `/healthz`) | `app/main.py`, `app/api/` | JWT + bcrypt |
| LLM client (vLLM OpenAI API) | `app/llm/client.py` | metrics + traces |
| Orchestrator — single agent | `app/orchestrator/graph.py`, `nodes.py` | Postgres checkpoints |
| Orchestrator — team (planner→researcher→writer) | `app/orchestrator/multi_graph.py`, `agents.py` | |
| Per-department tool registries (Phase 3) | `app/orchestrator/tools.py` | `tools_for()`; prompt + dispatch scoped by dept |
| Vault (AES-256-GCM envelope + rotation) | `app/vault/aesgcm_vault.py` | master key out of DB |
| ACL engine (deny-default, glob, deny-override, **owner `$self`**) | `app/acl/engine.py` | owner-only personal data |
| Department packs + `departments` table + seed | `app/departments/` | idempotent; see §5 of ROADMAP |
| Request Manager (classify→authz→quota→critical→execute) | `app/request_manager/router.py` | injectable runner/quota |
| Classifier / quota / approvals / guards / retention | `app/request_manager/*.py` | |
| NSFW guard (block + report hr/admin) | `guards.py`, `Report` table | `/v1/reports` |
| Order/bill/service forwarding → accounts inbox | `guards.py`, `DepartmentTask` table | `/v1/inbox` + `/process` |
| Approvals (critical actions) | `approvals.py` | `/v1/approvals` |
| Chat-history 3-day retention | `retention.py`, `scripts/purge_history.py` | text kept, artifacts purged |
| Storage (MinIO, ACL-enforced) | `app/storage/objects.py` | |
| Observability + 2 Grafana dashboards | `app/observability.py`, `deploy/grafana/` | LLM&ACL + Diagnostics/Logs |
| Compose stack (gateway, vLLM, PG, Redis, MinIO, OTel, Prom, Loki, Grafana) | `docker-compose.yml`, `deploy/` | |
| Docs | `ARCHITECTURE.md`, `README.md`, `docs/*` | cloud setup, user guide, roadmap |
| Tests — 32 | `tests/` | SQLite/in-memory + fakes; no GPU/Redis needed |

**Departments:** `account, hr, logistics, researcher, superuser, admin`.
**Data schema & rules:** see `docs/PROGRESS_AND_ROADMAP.md` §5 and `app/departments/packs.py` (the source of truth).

---

## 3. What to do next (ordered)

### Build here (no GPU; pure code, testable)
1. **In-graph approval interrupt.** Replace the RM pre-execution keyword gate with LangGraph
   `interrupt()` on the actual critical tool call (`initiate_payment`, `manage_bill`). Resume from the
   Postgres checkpoint on approve. (Phase 3.13 remainder — the biggest correctness upgrade.)
2. **Model-based NSFW + intent classifier.** Swap the keyword heuristics in `guards.py`/`classifier.py`
   for an LLM/zero-shot classifier behind the same function signatures. Safety-relevant.
3. **Phase 1 hardening:** Alembic migrations (replace `create_all`), boot-time secret validation,
   `/readyz`, global JSON error handler (`PermissionError`→403 + trace id), SSE streaming, Redis
   rate-limit middleware.
4. **CI:** ruff + pytest on push.
5. **Concurrency limiter** (per-department, Redis) — rate is done, max-concurrency is not.
6. **Alert rules** (Prometheus): denial spikes, quota exhaustion, vLLM down, queue backlog.

### Do on the server (Claude Code in the aiccloud.in instance) — see `docs/CLOUD_SETUP_GUIDE.md`
7. Install NVIDIA driver + Container Toolkit; verify GPU-in-container.
8. Pull/quantize the **real weights**; pick FP8 (Ada/Hopper) vs AWQ (Ampere) for the rented card; tune
   `--max-model-len`/`--max-num-seqs`; benchmark.
9. End-to-end run against the live model; **prompt/agent-quality tuning**.
10. Real TLS/DNS (Cloudflare tunnel or Caddy); confirm vLLM scrape + GPU panels show live data.
11. Schedule `scripts/purge_history.py` (cron/schedule skill); run `scripts/init_db.py` on real Postgres.
12. Load test; production secrets; security review.

### Blocked on procurement (external accounts)
- Payment gateway (account), email/SMS (logistics notify), ERP/inventory source, web-search API
  (researcher). Until these exist, those tools stay **stubs** (`[stub]` in `tools.py`).

---

## 4. Known limitations / open questions (for future discussion)

1. **Approval gate is pre-execution, not in-graph.** It fires on a keyword before the agent runs, not on
   the real tool call. Robust fix = LangGraph interrupt (next-step #1). *Risk:* a critical action phrased
   without a trigger word could run; a benign message with a trigger word is gated unnecessarily.
2. **NSFW & intent detection are keyword heuristics.** Will miss obfuscated/novel phrasing and can
   false-positive. Safety path (NSFW→hr/admin) should move to a real classifier. **Discuss desired
   recall/precision + escalation policy.**
3. **Forwarding routes *all* order/bill/service requests to accounts**, including when the requester is
   already in accounts. Decide whether members should execute directly instead of self-queuing.
4. **Classifier ambiguity.** A user in multiple departments with no keyword match falls through; there's
   no `misc` role any more. Decide a default/behavior for unroutable multi-role users.
5. **Approver authz is admin-only.** USER_GUIDE implies departmental approvers too. Decide if e.g. an
   `account` lead can approve account requests.
6. **No DB migrations yet** (`create_all`). Schema changes aren't versioned — add Alembic before the
   schema is live on the server, or data migrations will hurt.
7. **Secrets in `.env` for DB/MinIO.** Fine for dev; production should use Docker secrets or the vault.
   `VAULT_MASTER_KEY`/`JWT_SECRET` are not yet validated at boot (can start insecure).
8. **Personal-data retention is partial.** Chat-history artifacts purge after 3 days, but there is no
   purge/expiry for other personal data, and no "right to be forgotten" endpoint. Clarify policy.
9. **`summarize`/`web_search`/all external tools are stubs.** Agent "research quality" can't be judged
   until a real model + real tools exist (server-side).
10. **Model id `gemma-3-26b-a4b-it` is a placeholder.** The exact HF repo for the 26B/A4B weights must be
    confirmed before vLLM can pull it. Same for the Nemotron repo id.
11. **vLLM on the rented card is unvalidated.** FP8 vs AWQ, VRAM fit, throughput — all TBD on real
    hardware (aiccloud has no Quadro 8000; L40S/A100/H100 instead).
12. **No rate limit on `/v1/chat` (raw path)** — only `/v1/request` is governed. Decide whether to retire
    direct `/v1/chat` for non-admins.
13. **Single-node, no HA/backups wired.** Backup commands are documented, not automated; no restore test.
14. **Tests use SQLite + fakes.** They verify logic, not Postgres-specific behavior, real LLM output, or
    Redis quota under contention. Add integration tests on the server.

---

## 5. How to run / verify (local dev)

```bash
cd ai-platform
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows path
./.venv/Scripts/python.exe -m pytest tests/ -q        # 32 passing, no services needed
```
Full stack: `docker compose up -d --build` then `docker compose exec gateway python -m scripts.init_db --admin-pass '<pw>'`.
Governed entrypoint: `POST /v1/request`. See `docs/USER_GUIDE.md`.

## 6. Conventions (so future edits stay consistent)

- **Source of truth for access** = `app/departments/packs.py` (grants → ACL policies via `registry.py`).
  Add a department or data type there, not by hand-writing ACL rows.
- **Every new tool** is ACL-gated (`_enforce` in `tools.py`) and added to `DEPARTMENT_TOOLS`.
- **Keep it testable without infra:** inject `runner`/`quota`; back new stores with a Session so tests use
  SQLite; new metrics go in `observability.py`.
- **Commit attribution:** end messages with `Co-Authored-By: Claude <noreply@anthropic.com>` (use the
  model line the session provides).
- Windows dev host; services run in Docker. Line-ending CRLF warnings on commit are harmless.
