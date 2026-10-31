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
  `./.venv/Scripts/python.exe -m pytest tests/ -q` (32 passing, no GPU/Redis needed).
- Metrics go in `app/observability.py`. New tables in `app/db/session.py` (no Alembic yet — see HANDOFF).

## Build-now vs build-on-server
Pure code/config is built & tested here; GPU bring-up, real model/weights, TLS/DNS, load/security testing
happen on the server (see HANDOFF §3 and PROGRESS_AND_ROADMAP §10).
