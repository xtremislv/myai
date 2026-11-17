# Claude Code on the Claude-API server: background, setup, processes

For Claude Code sessions on the new server (8 vCPU / 16 GB / 100 GB, Ubuntu 24.04, no GPU, model = Claude via
the Anthropic API). Read this, then `docs/SESSION_WRAPUP.md` (current state) before changing anything.
Set up a session's memory and permissions with `scripts/setup_claude_code.sh` (below).

No secrets here (this file is pushed to GitHub): passwords, tokens, keys and the proxy VPS address live only
in `/root/myai/.env` (mode 600), the vault, and the files listed under "Secrets on this server".

## 1. Background

- **Owner:** runs the YouTube channels TechWiser (@techwiser) and GadgetWiser (@wisergadget) and the TechWiser
  Instagram. The platform is the company's private AI assistant: TW-ai, at https://tw-ai.in (Grafana at
  https://grafana.tw-ai.in), plus an Android app that self-updates from the website.
- **Users:** about 6 people in departments `account, hr, logistics, researcher, superuser, admin`; 4-6 at once.
- **What they use it for:** private chat (encrypted per person), work requests through the Request Manager
  (approvals for critical tools), web research and shopping with a headless browser, mail summaries/digests,
  documents from templates, data analysis in a sandbox, reminders, voice input, and **scouts**: standing
  product-research jobs (TechWiser Instagram, GadgetWiser, ...) that find gadgets for videos, know what our
  channels already covered and what worked for competitors (@beebomco, @sillycornsmedia), and learn from the
  team's review reasons. Research = specific products with prices, store links and videos, India-orderable.
- **History:** built 2026-11-02 to 11-16 on a vast.ai GPU container (Gemma 4 26B on vLLM). Moved to this
  server with Claude as the model (branch `claude-api-migration`, `docs/MIGRATION_CLAUDE.md`), because the API
  removes the GPU rental. Same domain, tunnel, data, accounts.
- **Docs:** `docs/SESSION_WRAPUP.md` (state, requirements, next steps: keep it current), `docs/HANDOFF.md`
  (detailed history), `CLAUDE.md` (code conventions), `BRANCHES.md` (branch map), `docs/ANDROID_APP.md`,
  `docs/MIGRATION_CLAUDE.md`, `docs/SERVER_SETUP.md` (older, GPU/container setup).

## 2. Owner's standing rules (follow without being told)

1. **Git:** commit and push freely, **never to `main`**. Push with plain `git push` (repo-local credential helper;
   never print or commit the credentials file). Commit as the repo identity `xtremislv`
   (`git -c user.name=xtremislv -c user.email=xtremislv@users.noreply.github.com commit ...`). New work: new
   branch, and record it in `BRANCHES.md`.
2. **Official mailboxes** (vault `mail/contact`, `mail/career`, `mail/finance`): never use or touch them without
   asking, not even for a login test or as a fallback sender. Service mail (alerts, links, reminders, welcomes)
   goes only from `mail/notify`; if that's broken, say so and wait.
3. **No secrets in pushed files:** no passwords, tokens, keys or the VPS address in docs (SESSION_WRAPUP.md is
   pushed). Don't print `.env` values.
4. **Android:** the signing key in `/root/.config/twai-android/` must never be regenerated or replaced (phones
   refuse updates signed with another key). Publishing a release needs the owner's go-ahead.
5. **Live data:** never run `tests/test_postgres.py` against the live database (use a throwaway DB, §5); don't
   sign tokens as real users to probe; never put private chat text in logs.
6. **Network:** agents' web traffic goes through the owner's VPS SOCKS proxy (`OUTBOUND_PROXY`), set up at their
   request. Don't invent other ways around blocked sites on your own: suggest them and let the owner decide.
7. **Keep `docs/SESSION_WRAPUP.md` current** at the end of each working session (update, not a diary).
8. **Costs:** every model call now costs money. Ask before bulk re-runs (re-scoring all scout finds, evals over
   hundreds of cases, load tests); state the expected spend.

## 3. Setup of this server

| What | Where |
|---|---|
| Code (live checkout) | `/root/myai` on branch `claude-api-migration` (until merged) |
| Settings and secrets | `/root/myai/.env` (600): `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_MONTHLY_BUDGET`, `LLM_USD_RATE`, `LLM_TIER_TARGETS`, `LLM_*_MODEL`, `LLM_THINKING`, `VAULT_MASTER_KEY`, `POSTGRES_*`, `REDIS_*`, `MINIO_*`, `CF_TUNNEL`, `PROXY_SSH*`, `OUTBOUND_PROXY`, `STT_DEVICE=cpu`, `TUNNEL_OFF`, `SCHEDULER_OFF`, `LLM_USAGE_LOG` |
| Python env | `/root/myai/.venv` (uv); sandbox interpreter `/opt/agent-sandbox` (runs as `nobody`) |
| Logs, PID files | `/var/log/ai-platform/` (`gateway.log`, `scheduler.log`, `watchdog.log`, `boot.log`, `<service>.pid`, `llm_usage.jsonl`) |
| Data | Postgres 16 (`ai_platform`, role `ai`), MinIO `/var/lib/minio`, published Android app `/var/lib/ai-platform/android`, jobs `/var/lib/ai-platform/work` |
| Monitoring | Prometheus `/var/lib/prometheus/native` (15 days), Loki `/var/lib/loki`, Grafana `/var/lib/grafana` |
| Whisper model | `/root/models/whisper-large-v3-turbo` (CPU, int8) |
| Tunnel | `/root/.cloudflared/` (tunnel `tw-ai`, `tw-ai.yml` routes tw-ai.in → :8000, grafana.tw-ai.in → :3000) |
| Backups | `/var/backups/ai-platform/`, nightly 02:30 (`ai-platform-backup.timer`); off-server only once `BACKUP_DEST` is set |

**Secrets on this server** (never print, copy into the repo, or send anywhere): `.env`, `/root/.cloudflared/*`,
`/root/.ssh/proxy_vps`, `/root/.config/twai-android/*`, `/root/.config/git/myai-credentials`, the vault (in Postgres,
encrypted with `VAULT_MASTER_KEY`).

**Services** (all on 127.0.0.1; the Cloudflare tunnel is the only way in):

| Service | Port | Notes |
|---|---|---|
| Postgres, Redis | 5432, 6379 | systemd (Ubuntu); Redis has a password: use `REDISCLI_AUTH`, never `-a` |
| Gateway (FastAPI, uvicorn) | 8000 | `/healthz`, `/readyz` (also checks the Claude API key) |
| Whisper (scripts/stt_server.py) | 8011 | voice input |
| MinIO | 9000 | files |
| Scheduler | — | reminders, digests, scouts, welcome mails, cleanup |
| Retention | — | deletes chat attachments after 3 days |
| Loki, Alloy, Prometheus, Grafana | 3100, 4317/12345, 9090, 3000 | logs/metrics/dashboards |
| proxy-tunnel | 1080 | SSH SOCKS to the owner's VPS for agents' web traffic |
| tunnel (cloudflared) | metrics 20260 | domain |
| Watchdog | — | restarts anything down every minute, emails links via `mail/notify` |

## 4. Processes

**Status and restarts**
```bash
cd /root/myai
scripts/start_native.sh status            # every line "up" (claude-api = key + route to the API)
systemctl restart ai-platform             # everything (stop + start through start_native.sh)
scripts/start_native.sh                   # start whatever is down (safe to rerun)
kill "$(cat /var/log/ai-platform/gateway.pid)"; scripts/start_native.sh   # only the gateway
kill "$(cat /var/log/ai-platform/scheduler.pid)"; scripts/start_native.sh # only the scheduler
```
Restart the scheduler only when no scout round is running (it would leave the round marked running; check
`scheduler.log`). The gateway and scheduler load code at start: restart them after a pull.

**Deploying a change**
```bash
git pull                                       # or commit here, then push (never main)
.venv/bin/python -m alembic upgrade head       # if migrations/ changed (start_native.sh also does it)
uv pip install --python .venv/bin/python -e .  # if pyproject.toml changed
kill "$(cat /var/log/ai-platform/gateway.pid)" "$(cat /var/log/ai-platform/scheduler.pid)"; scripts/start_native.sh
```

**Tests and checks** (no infrastructure needed, no model calls):
```bash
.venv/bin/python -m pytest tests/ -q      # ~4 min; 768 passed, 13 skipped on 2026-11-16
.venv/bin/ruff check .
```
`scripts/eval_triage.py` and `scripts/eval_toolgroups.py` call the real model (they cost money now: ask first).
After changing the triage prompt or tool groups they are still required (CLAUDE.md).
Postgres integration tests only on a throwaway DB:
`su postgres -c "createdb -O ai ai_platform_itest"`, run with `POSTGRES_INTEGRATION=1 POSTGRES_DSN=<that db>`, then `dropdb`.

**Model and cost**
- Which model does what: the **model manager** (`app/llm/model_manager.py`) picks a model per call:
  Haiku 5.5 for background and short sorting work (triage, safety checks, tool picking, scout searches, mail and
  file summaries), Sonnet 5.5 for normal answers and scout judging, Opus 5.5 for multi-step work (supervisor and
  specialists, team mode, long tool chains, big contexts, analysis/planning requests), Fable 5.2 when a person
  asks for deep thinking. Monthly mix targets 50/35/10/5% (`LLM_TIER_TARGETS`) act as ceilings: a tier over its
  share steps down one tier. Budget `LLM_MONTHLY_BUDGET` (50,000 INR at `LLM_USD_RATE`): admin alerts (bell +
  email) when the month is projected over budget, at 80% and at 100%; economy mode (Fable/Opus step down) while
  projected over; only Haiku/Sonnet once it is used up (`LLM_BUDGET_HARD_STOP=true` refuses calls instead).
  This month's figures: `GET /v1/admin/llm-usage`, Prometheus `llm_month_spend_usd` / `llm_month_projected_usd`.
  Change settings with `python3 scripts/env_set.py KEY=value` + restart. Budget alerts run in the scheduler.
- The owner's budget is 50,000 INR a month: don't raise it, change the targets or turn off alerts without asking.
- Spend: `.venv/bin/python -m scripts.llm_cost --hours 24` (last table = actual Claude spend by model).
- **Prepaid credit** (`app/llm/credit.py`): the API has no balance endpoint, so the server counts down from the
  balance entered after each top-up: `.venv/bin/python -m scripts.llm_credit set <USD from the Console>` (no restart;
  `.venv/bin/python -m scripts.llm_credit` shows the estimate). At `LLM_CREDIT_ALERTS` ($10 and $5 left) the admins get a
  bell notice and an email (plus `LLM_CREDIT_ALERT_EMAILS`), once per level per top-up; an API "credit balance is too
  low" refusal alerts at once. Checked by the scheduler every 30 s; also in `GET /v1/admin/llm-usage` (`credit`).
- Token counts per task are in `llm_usage.jsonl` (counts only, never content) and Prometheus `llm_task_tokens_total`.
- API errors: `grep -i anthropic /var/log/ai-platform/gateway.log`; 429 = rate limit (lower
  `LLM_BACKGROUND_CONCURRENCY`), 401 = key, 529/5xx = Anthropic side (the SDK retries 3 times).

**Backups**
```bash
scripts/backup.sh                                          # by hand
scripts/restore.sh --verify /var/backups/ai-platform/<latest>.tar   # proves it, live data untouched
```

**Android release** (only with the owner's go-ahead): commit `android/` changes, then
`scripts/publish_android.sh "notes"` (needs the Android SDK on this server and the signing key; see docs/ANDROID_APP.md).

**Where to look when something breaks**
- Site down: `scripts/start_native.sh status`; tunnel line down → `tail /var/log/ai-platform/tunnel.log`.
- Assistant silent / errors: `/readyz`, `gateway.log`, Anthropic status, the key's spend limit in the Console.
- Research weak or failing: `proxy-tunnel` status first, then `.venv/bin/python -m scripts.check_connectivity`.
  A Tavily/Serper key (`search/tavily` / `search/serper` on the Credentials page) is the real fix for search quality.
- Disk: `df -h /`; Prometheus is capped at 2 GB, Loki keeps 14 days.

## 5. Working style the owner expects
- Plain explanations, real data only (no mock status in the UI), concrete next steps.
- Ask before anything outward-facing (mail to people, publishing the app, deleting data, paid bulk runs).
- Report test results honestly; say what wasn't verified.
