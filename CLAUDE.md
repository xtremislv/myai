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
- `docs/CLOUD_SETUP_GUIDE.md` — deploy on aiccloud.in
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
- Never use a client `thread_id` as a checkpoint key directly; use `scoped_thread()`.

## Build-now vs build-on-server
Pure code/config is built & tested here; GPU bring-up, real model/weights, TLS/DNS, load/security testing
happen on the server (see HANDOFF §3 and PROGRESS_AND_ROADMAP §10).
