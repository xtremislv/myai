# CLAUDE.md — orientation for Claude Code sessions

**Read `docs/HANDOFF.md` first** — it is the full state, next steps, and open limitations.

## What this is
Self-hosted AI platform: vLLM-served local LLM (26B/A4B MoE default, Nemotron-switchable) on a rented
aiccloud.in GPU, FastAPI gateway, LangGraph orchestrator (single + team), app-managed AES-GCM vault,
deny-by-default ACL (with owner-only `$self` data), a Request Manager (department routing, quotas,
approvals, NSFW + order-forwarding guards), and OpenTelemetry → Prometheus/Loki/Grafana.

## Key docs
- `docs/HANDOFF.md` — state + next steps + limitations (start here)
- `docs/PROGRESS_AND_ROADMAP.md` — phased plan; §5 = department/data schema
- `docs/SERVER_SETUP.md` — deploy on aiccloud.in
- `docs/USER_GUIDE.md` — how users send requests
- `ARCHITECTURE.md` — design

## Conventions
- Access control source of truth: `app/departments/packs.py` (grants → ACL via `registry.py`). Don't
  hand-write ACL rows.
- Departments: `account, hr, logistics, researcher, superuser, admin`. Governed entrypoint: `POST /v1/request`.
- Every tool is ACL-gated and listed in `DEPARTMENT_TOOLS` (`app/orchestrator/tools.py`).
- Keep logic testable without infra (inject `runner`/`quota`; back stores with a Session). Tests:
  `./.venv/Scripts/python.exe -m pytest tests/ -q` (no GPU/Redis/Postgres needed); lint: `ruff check .`.
- Metrics go in `app/observability.py`. Models live in `app/db/session.py`; after changing one, run
  `alembic revision --autogenerate -m "..."` (a test fails if models and migrations differ).
- Critical tools are listed as `tool:<name>` in `critical_resources` in `packs.py`. They pause in the graph
  (`run_tool` in `app/orchestrator/nodes.py`) until approved. Code above that interrupt re-runs on resume,
  so it must have no side effects.
- Never use a client `thread_id` as a checkpoint key directly; use `scoped_thread()`, or `private_thread()`
  for `/v1/chat`.
- Private chat text is encrypted per user (`app/vault/user_keys.py`). Keep conversation text out of `str`
  fields on `AgentState`: PostgresSaver stores those as plain JSON, skipping encryption.
- Attachment text goes to the model through `app/orchestrator/context.py`, never graph state: state is
  checkpointed forever, and attachments must be gone after 3 days.
- The web UI is plain files in `app/web/`, served at `/ui/` under a strict CSP: no inline scripts or styles,
  no third-party assets. The token stays in JS memory because it can open private chats.
- `tests/test_postgres.py` runs only with `POSTGRES_INTEGRATION=1` and a throwaway `POSTGRES_DSN`.
- `/v1/chat` is the one chat box. `guards.triage` sorts each message, defaulting to private. Private messages
  have no tools, no logging or audit of content; only content flagged as explicit or illegal leaves them,
  reported to hr + admin. Work runs through the Request Manager, apart from the encrypted chat because an
  approver must resume it without the user's key. There is one admin, whose finance activity as work is
  reported to accounts (`guards.notify_accounts`). A user's `roles` can hold several departments.

## Build-now vs build-on-server
Pure code/config is built & tested here; GPU bring-up, real model/weights, TLS/DNS, load/security testing
happen on the server (see HANDOFF §3 and PROGRESS_AND_ROADMAP §10).
