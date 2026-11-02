# Handoff for the session on the server

You are Claude Code running on the rented GPU server. The code was built and tested on a Windows machine with
**no Docker, GPU, real model or real MinIO**. Your job is first bring-up and tuning. Setup steps are in
`docs/SERVER_SETUP.md`.

## What this is
A self-hosted AI platform. Users get one chat box (`/ui/`). `guards.triage` sorts each message:
- **Private:** no tools, no audit, encrypted with a per-user key that only their password or recovery code unlocks.
- **Work:** runs in a department through the Request Manager, with tools, quotas, audit and approvals. It is stored
  apart from the encrypted chat because an approver must resume it without the user's key, then copied into the chat.
- **Orders, bookings, bills:** go to the accounts inbox.
- **Explicit or illegal:** blocked, with that one message reported to HR and the admin.

## Decisions the owner made (don't reverse without asking)
- Chat text is kept forever and encrypted per user. Attachments are deleted after 3 days.
- There is one admin, who may approve their own requests. Accounts is notified of the admin's finance work, except
  private chat and personal data.
- NSFW means explicit or illegal. When triage is unsure, a message stays private.
- `initiate_payment`, `manage_bill`, `manage_order` and `book_service` always need approval.
- **No limit on failed logins.** The owner declined it.
- A person can hold several departments. Roles are read from the database on every request.
- Grafana runs on the server only.

## Not verified yet: check these first
1. **`docker compose up --build` has never run.** Check that the Dockerfile builds, that multi-word `VLLM_EXTRA_ARGS`
   splits into separate flags, and that the non-root app user works.
2. **Collector to Loki.** The old `loki` exporter no longer exists, so `deploy/otel-collector.yaml` now uses
   `otlphttp` to `http://loki:3100/otlp`. Confirm logs reach Grafana's log panels with `service_name` and
   `severity_text`. The telemetry setting and all image tags were checked against upstream, but nothing has run.
3. **MinIO image.** Docker Hub returned nothing for `minio/minio`, so compose uses the tag from MinIO's own compose
   file on quay.io. Quay needed auth to confirm the tag: check that it pulls and that `mc ready local` works.
4. **Attachments and retention against real MinIO.** Only fake storage has been used.
5. **`scripts/backup.sh` and `scripts/restore.sh` with real Docker.** They were tested with fake `docker` and `rclone`
   commands. Do a real restore test.
6. **Does vLLM log prompts?** If so, turn it off with whatever flag the pinned version offers: it would bypass the
   encryption. Check `docker compose logs vllm` after a chat.
7. **`role: tool` messages.** The single-agent graph sends them with no `tool_call_id`, and some chat templates reject
   the order of roles. Test a work request early.

## Then do these
1. Confirm the real model id and choose quantization for the card. `gemma-3-26b-a4b-it` is a placeholder.
2. Benchmark, and tune `--max-model-len`, `--max-num-seqs`, and `max_concurrency` in `app/departments/packs.py`.
3. **Test triage accuracy with the real model** (`TRIAGE_PROMPT` in `guards.py`). A personal message wrongly sorted as
   work is stored outside the user's encryption, so measure that error especially. The NSFW and forwarding prompts
   have also only seen a fake model.
4. Real TLS and DNS, load test, security review.
5. Alert notifications: rules exist in `deploy/prometheus-rules.yml`, but nothing sends them. Add a Grafana contact point.

## Known limits
- Payment, email/SMS, ERP and web search tools are stubs until vendor accounts exist.
- Attachments work in private messages only. The AI reads text and PDF only, and unreadable files skip the NSFW check.
- Each work request runs in one department.
- A request already waiting for approval keeps the roles its requester had when they asked.
- SQLAlchemy 2.1 is newer than the OpenTelemetry instrumentor supports, so database spans are skipped and it logs one
  error at startup.
- No way yet to delete a user's data on request, and no script for rotating the vault master key.
- `docs/PROGRESS_AND_ROADMAP.md` is historical. This file is the current state.

## Working here
- Read `CLAUDE.md` for conventions. Tests: `python -m pytest tests/ -q`. Lint: `ruff check .`.
- `tests/test_postgres.py` needs `POSTGRES_INTEGRATION=1` and a **throwaway** `POSTGRES_DSN`.
- After changing a model, run `alembic revision --autogenerate`. A test fails if models and migrations differ.
- Never put message content in logs, spans or exception text.
- The owner lets you commit to the current branch unless a change is alarming. Don't commit `.claude/`.
