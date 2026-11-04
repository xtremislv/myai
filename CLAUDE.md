# CLAUDE.md — orientation for Claude Code sessions

**Read `docs/SESSION_WRAPUP.md` first**: the current state, the owner's requirements, capabilities, architecture,
limitations and next steps in one place. `docs/HANDOFF.md` has the detailed history of each change.

## What this is
Self-hosted AI platform: vLLM-served local LLM (26B/A4B MoE default, Nemotron-switchable) on a rented
aiccloud.in GPU, FastAPI gateway, LangGraph orchestrator (single, team, and `agents`: a supervisor over research/browser/mail/booking
specialists with real web search, scraping and a headless browser in `app/browsing/`), app-managed AES-GCM vault,
deny-by-default ACL (with owner-only `$self` data), a Request Manager (department routing, quotas,
approvals, NSFW + order-forwarding guards), and OpenTelemetry → Prometheus/Loki/Grafana.

## Key docs
- `docs/HANDOFF.md` — state + next steps + limitations (start here)
- `docs/PROGRESS_AND_ROADMAP.md` — phased plan; §5 = department/data schema
- `docs/SERVER_SETUP.md` — deploy on aiccloud.in
- `docs/USER_GUIDE.md` — how users send requests
- `ARCHITECTURE.md` — design

## Conventions
- Access control source of truth: `app/departments/packs.py` (grants → ACL via `registry.py`, which also
  removes built-in rows packs.py dropped). Don't hand-write ACL rows. A department's mail and documents are for
  its members only: never add `"admin" in roles` shortcuts, and never give the admin role a department's data.
- Departments: `account, hr, logistics, researcher, superuser, admin`. Governed entrypoint: `POST /v1/request`.
- Every tool is ACL-gated and listed in `DEPARTMENT_TOOLS` (`app/orchestrator/tools.py`).
- Keep logic testable without infra (inject `runner`/`quota`; back stores with a Session). Tests:
  `.venv/bin/python -m pytest tests/ -q` (Windows: `./.venv/Scripts/python.exe`) (no GPU/Redis/Postgres needed); lint: `ruff check .`.
- Metrics go in `app/observability.py`. Models live in `app/db/session.py`; after changing one, run
  `alembic revision --autogenerate -m "..."` (a test fails if models and migrations differ).
- Critical tools are listed as `tool:<name>` in `critical_resources` in `packs.py`. They pause in the graph
  (`run_tool` in `app/orchestrator/nodes.py`) until approved. Code above that interrupt re-runs on resume,
  so it must have no side effects.
- Web and browser access goes through `app/browsing/netguard.py` (public addresses only). Committing browser
  clicks and sending mail are critical tools (`browser_commit`, `send_mail`). Wrap web/mail text with `untrusted()`.
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
  have `PRIVATE_TOOLS`: read-only web, plus the person's OWN mailbox and the official mailboxes of their own
  departments (members only, ACL-checked; `_mailbox` makes the agent ask which when unclear), files and
  reminders; no logging or audit of content; only content flagged as explicit or illegal leaves them,
  reported to hr + admin. Work runs through the Request Manager, apart from the encrypted chat because an
  approver must resume it without the user's key. There is one admin, whose finance activity as work is
  reported to accounts (`guards.notify_accounts`). A user's `roles` can hold several departments.
- Personal files (`app/storage/userfiles.py`) are encrypted with the person's chat key, which tools get from
  the `user_key` context var (`app/orchestrator/context.py`) during a private run, never from state. Work file
  tools use `app/storage/deptfiles.py` (ACL-checked, `shared/<department>/` by default, undo copies in `_versions/`).
- `ALWAYS_CRITICAL` in `packs.py` (`send_mail`, `delete_file`, `browser_commit`) pauses in every department and
  in private chat. In private chat the person confirms for themselves (`/v1/chat/threads/<id>/confirm`,
  `execution.resume_private`); a new message instead of confirming declines it.
- Reminders and notifications (`app/reminders/`) are encrypted with the notes key in the vault
  (`app/vault/notes.py`), because the scheduler (`scripts/scheduler.py`) must read them while the owner is away.
- Mailboxes a person configures are `guarded`: public addresses on mail ports only (`connector.check_servers`).
- Once a conversation has read mail or files, web tools refuse long URLs/queries (`_leaks` in `tools.py`).
- After changing the triage prompt, run `scripts/eval_triage.py` (cases in `tests/eval/triage_cases.jsonl`).
- Templates (`app/documents/`): fill with the sandboxed Jinja env in `engine.py` only (never a plain
  `jinja2.Template`: templates come from users). Company templates are work-only; private chat uses the
  person's own templates and attachments (`attachment_files` context var).
- The UI renders model text with `md()` (app.js), which builds DOM nodes; never use innerHTML for model or page text.
- UI look: tokens at the top of `style.css` (light + dark, dark repeated under `:root[data-theme="dark"]`); use
  `mi(name)` for icons (add paths to `MI` from Material Symbols Outlined 400), `heading(title, text, {eyebrow})`
  for page titles, and only show real data (no mock status or features from the design files).
- Saved details (`app/wallet.py`) are private-chat only, encrypted with the person's key; tools fill them into
  pages via `Browser.fill_value` with a site check, and must never return values to the model.
- Personal purchases stay private (`guards.triage` drops an action on a private route); `browser_commit` in private
  chat uses `web:commit-private` and is always confirmed by the person. Sign-in/OTP/payment are the person's, via
  the live view (`routes_browser.py`); snapshots hide sensitive field values (`_MARK_JS`).
- `OUTBOUND_PROXY` is for agents' web traffic only (fetch, search APIs, Chromium); keep internal traffic direct.
- Mailboxes may be signed in with OAuth (`app/mail/oauth.py`, secret `"auth": "oauth"`): always build
  `MailConfig.from_secret(raw, name=<vault name>)` so refreshed tokens are saved back. App clients are the
  vault secrets `oauth/google` and `oauth/microsoft`.
- Agent code runs only through `app/sandbox.py` (as `nobody`, no network, copies of files). Never `exec` model
  code in the gateway. Dashboards are JSON specs checked by `app/dashboards.py` and drawn by `renderDashboard`
  (app.js); they ride on the final assistant message via `context.with_dashboards`, never in a `str` state field.
- Data bigger than the context goes through `app/bigsummary.py` (`summarize_large`): parts in a private temp
  folder under `WORK_DIR`, always deleted; never put the whole text in a prompt or in graph state.
- Web tools must never pass a block/CAPTCHA page off as content (`extract.block_reason`, `tools._readable`).
  `scripts/check_connectivity.py` shows what this network lets through; it changes over time.

## Build-now vs build-on-server
Pure code/config is built & tested here; GPU bring-up, real model/weights, TLS/DNS, load/security testing
happen on the server (see HANDOFF §3 and PROGRESS_AND_ROADMAP §10).
