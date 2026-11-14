# Session wrap-up: state of the platform on 2026-11-14

Read this first in the next session. It is the short, current picture. `docs/HANDOFF.md` has the detailed
history of each change, `CLAUDE.md` the coding conventions, `docs/SERVER_SETUP.md` the setup steps.
`ARCHITECTURE.md` and `docs/PROGRESS_AND_ROADMAP.md` are older (they assume Docker); where they disagree with
this file, this file is right.

- Repo: `/root/myai`, GitHub `xtremislv/myai`. The live checkout is on branch **`android-app`** since 2026-11-10
  (it contains all of `server-native-setup`; see `BRANCHES.md`). Never push to `main` (owner's rule).
- Android app: see `docs/ANDROID_APP.md`. **1.0.81** is published (2026-11-14: voice input via the server's Whisper,
  RECORD_AUDIO). 1.0.71 was published 2026-11-13. 1.0.70 crashed right after sign-in
  on Android 13+ (fixed in 1.0.71, see
  "Tests, crash reports and debugging" there); phones now send crash traces to the gateway log ("ANDROID CRASH").
- Tests: **724 passed, 13 skipped** (`.venv/bin/python -m pytest tests/ -q`). The 13 skipped are the Postgres
  integration tests: they pass on a throwaway database (see "Gotchas"). Lint clean (`.venv/bin/ruff check .`).
- Triage eval on the real model: **450/450**, 0 personal messages sorted as work (`scripts/eval_triage.py`).

---

## 1. Resume in five minutes

```bash
cd /root/myai
scripts/start_native.sh status          # every line should say "up"; prints the public URLs
scripts/start_native.sh                 # (re)start anything that's down: safe to rerun
curl -s localhost:8000/readyz           # {"ready":true,...}
.venv/bin/python -m scripts.check_connectivity   # what the agents can reach right now
```

- **App (public):** **https://tw-ai.in/ui/** (fixed since 2026-11-10: named Cloudflare tunnel `tw-ai`, config
  `~/.cloudflared/tw-ai.yml`, `CF_TUNNEL` in `.env`; SERVER_SETUP 9b). Remove `CF_TUNNEL` to fall back to quick tunnels.
- **Grafana:** **https://grafana.tw-ai.in/** (user `admin`, password `GRAFANA_PASSWORD` in `.env`).
- **Logs:** `/var/log/ai-platform/<service>.log`; PIDs next to them.
- **Container restarts:** `/etc/rc.local` (copy of `deploy/native/rc.local`) runs `start_native.sh` at container
  start, and `scripts/watchdog.sh` restarts what goes down, emails the admin new links, and after every container
  start emails "back up after a restart" (with the tw-ai.in links) once all services are up, from `mail/notify` only
  (SERVER_SETUP "Auto-start"); alerts and welcome emails use the same sender. **Never use official mailboxes
  (`mail/contact`, `mail/career`, ...) for service mail, or touch them at all, without asking the owner.** Before that existed, an outage on 2026-11-04 (stopped ~16:58 UTC, back 20:36)
  left everything down until a manual start. Postgres recovers from WAL by itself; Redis only keeps its last RDB
  snapshot. vLLM takes ~2 min to load; a quick tunnel can need ~1 min to register (530 until then).
- **UI caching:** Cloudflare makes browsers keep static files for 4 h, so `/ui/` serves `index.html` with
  `app.js?v=<hash>`/`style.css?v=<hash>` (`app/main.py`, `_ui_page`); UI edits show on the next page load.
- **Prompt caching (speed):** every call carries ~6.4k tokens of tool definitions, and this GPU prefills only
  ~1,000 tokens/s, so a cache miss costs ~6-8 s and a hit ~0.5 s. Keep the system prompt identical between calls:
  the time goes on the latest user message at send time (`app/orchestrator/clock.py`, `place_clock`), never in the
  system prompt. The native gateway runs with `OTEL_TRACES_EXPORT=false` (Alloy has no trace backend).
- **Security fixes (2026-11-13):** data/log folders are root-only (`start_native.sh` re-applies `chmod 700`), and the
  code sandbox may read only its own folder and Python/system libraries (`app/sandbox.py`). Redis has a password
  (`requirepass` in /etc/redis/redis.conf = `REDIS_PASSWORD` in .env; redis-cli uses `REDISCLI_AUTH`). Tokens carry
  `sv` = `users.session_version`, bumped by a password change, recovery, admin reset and web sign-out (password change
  and recovery also sign out phones); browser sessions end 30 days after the password was entered. The Google mail
  callback only finishes in the browser that started it (`tw_oauth` cookie). Attachment text goes on the latest user
  message, not the system prompt (keeps the prompt cache).
- **Tool selection (2026-11-13):** the single agent sees only the tool groups a message needs
  (`app/orchestrator/toolsel.py`: web, mail, files, reminders, data, templates; sorted by a short model call that
  runs alongside triage), plus ungrouped/department tools, groups used in the last 3 turns, and groups it asks for
  with `more_tools`. Dispatch and ACL are unchanged. Answers ~35-45% faster. After changing its prompt or groups run
  `.venv/bin/python -m scripts.eval_toolgroups` (60 cases, 58/60 at release). No faster attention backend works on
  this GPU (FlashInfer/Flex/SDPA/fp8 KV all tested 2026-11-13): stay on Triton.
- **After code changes:** restart the gateway: `kill $(cat /var/log/ai-platform/gateway.pid); scripts/start_native.sh`.
  After changing `app/departments/packs.py`: `.venv/bin/python -m scripts.seed_departments`. After a model change in
  `app/db/session.py`: `alembic revision --autogenerate`, then `alembic upgrade head`.
- **Test accounts** for live end-to-end runs: `e2e_res` (researcher), `e2e_acct`, `e2e_logi`, `e2e_hr`, `e2e_none`
  (their password is in the owner's notes, not in the repo). Real accounts: `admin`, `techwiser`.

---

## 2. What the owner wants (motives and requirements)

**The goal:** a self-hosted AI assistant for a small company, running on one rented GPU, where each person has
one chat box that handles both their **private life** (encrypted, nobody else can read it) and **company work**
(routed to the right department, with approvals, audit and limits), and agents that actually *do* things: research
the web, read and answer mail, manage files, make documents from templates, set reminders, shop and order.

**Decisions the owner made (don't reverse without asking):**

| Area | Decision |
|---|---|
| Privacy | Private chat text kept forever, encrypted per person (only their password or recovery code unlocks it). Attachments deleted after 3 days. No logging or audit of private content. |
| Triage | One chat box; the model sorts each message. When unsure, it stays private. NSFW = explicit or illegal: blocked and that one message reported to HR + admin. |
| Department data | A department's official mailbox (finance@ = Accounts, careers = HR, contact@ = Admin) and its documents (shared files, templates, records) are for its members only: not the superuser department, and not the admin unless also a member (owner, 2026-11-04). Members connect their department mailbox under Settings. |
| Admin | Exactly one admin, who may approve their own requests. Accounts is notified of the admin's finance work. The admin can't read anyone's private chats, personal files, personal mail, mailbox logins or saved website logins. |
| Approvals | `initiate_payment`, `manage_bill`, `manage_order`, `book_service` always need approval. `send_mail`, `delete_file`, `browser_commit` always need a go-ahead: an approver at work, the person themselves in private chat. |
| Logins | No limit on failed logins (owner declined it). |
| Departments | `account, hr, logistics, researcher, superuser, admin`; a person can hold several; roles read from the DB on every request. |
| Department mailboxes in private chat | Members may read, summarize and send from their department's official mailbox in private chat too; sending is confirmed on a card that names the mailbox. With more than one mailbox and none named, the agent asks which (owner, 2026-11-04). |
| Private chat may use | Read-only web, the person's OWN mailbox, files, reminders and templates, product research, **personal shopping/ordering** (confirmed by the person). Never company data. |
| Files | Agents create/edit/move/restore freely (versions kept); deleting needs a go-ahead. |
| Reminders | In-app bell + browser pop-up, optional email, opt-in morning mail digest. |
| Alerts | Server alerts go to the admin's in-app bell. |
| Ordering | Every order asks **"personal or work?"** (agent asks first; the order card enforces it with "Personal: place it" / "Work: send to Accounts"). Personal orders use the person's **saved details** (logins, cards, UPI, addresses), encrypted with their key; only they can view them (after re-entering their password). The agent fills them into pages without seeing them, and only on the right site. Secrets that aren't saved, CVVs and OTPs are asked through a **secure box** in the chat (typed straight into the page). Work orders go to Accounts with company details. CVVs are never stored. |
| Mail setup | "Sign in with Google / Microsoft" (OAuth; the company's app clients are the vault secrets `oauth/google`, `oauth/microsoft`), or address + app password (Gmail first, then the provider named by the domain). Outlook only via Microsoft sign-in. |
| Data | Agents may run Python (pandas/numpy) in a sandbox and show **temporary dashboards** under replies (owner, 2026-11-04). |
| Network | The owner rents a VPS in Mumbai and asked for the agents' web traffic to go through it (done). |
| Infra | Grafana on the server only. No Docker on this host (it's a container). |

---

## 3. Current status

| Thing | State |
|---|---|
| Host | vast.ai container behind aiccloud.in. Quadro RTX 8000 48 GB (Turing, CC 7.5). No Docker, no systemd. Disk 40 GB, **~4.3 GB free** on 2026-11-13 (watch it; `/tmp/pip-unpack-*` holds 1.4 GB of stale pip temp files the owner may delete). |
| Model | `google/gemma-4-26B-A4B-it`, AWQ/compressed-tensors INT4 (`cyankiwi/...qat-AWQ-INT4` in `/root/models/gemma-4-26b`), vLLM 0.32.0 in `/root/vllm-env`, `--dtype half`, Turing patch applied, **64k context** (`LLM_CONTEXT_TOKENS` must match), KV cache 683k tokens, `--max-num-seqs 16`, tool parser `gemma4`. |
| Speed | 1 chat ~73 tok/s; 16 at once ~15 tok/s each (215 total); first word < 0.6 s for ~1.5k-token prompts, ~7.6 s for 8k-token prompts. |
| Sandbox | `/opt/agent-sandbox` (system Python 3.10 + numpy, pandas) for `run_python`, run as `nobody`; install with `scripts/setup_sandbox.sh`. |
| Services | postgres, redis, minio, vllm, gateway, retention, **scheduler**, loki, alloy, prometheus, grafana, app-tunnel, grafana-tunnel, **proxy-tunnel**, **watchdog**: all started by `scripts/start_native.sh`. |
| Database | Postgres 16, migration **0012** (head, scout review notes + mutes). |
| Network | Agents' web traffic goes through the owner's VPS (see §5.6). Without it this network poisons DNS and cuts many sites. |
| Backups | `scripts/backup.sh` works natively and was verified with `restore.sh --verify`; **no off-server destination set** (`BACKUP_DEST` empty), no schedule on this host. |

---

## 4. Capabilities (what a person can do today)

**Chat**
- One box; replies stream and render as formatted Markdown (tables, lists, links). Modes: `single` agent,
  `agents` (supervisor + specialists, chosen automatically for multi-step requests), `team` (on request).
- Live progress steps ("Searching the web for…", "Reading…").
- Attachments (text, PDF, Word, Excel): read for 3 days, encrypted.
- New chat shows "Recent chats" under the box (6 newest, "Show all", search); click to reopen.
- Files of any size: `summarize_large` splits them into parts (24k chars), condenses each, combines, and
  answers in chat, as a Word document, or as an Excel/CSV table of records (`app/bigsummary.py`).

**Research** (all departments with web access, and private chat)
- `web_search` (Tavily/Serper if a key is in the vault; else DuckDuckGo HTML → Bing RSS → Bing News → rendered
  Bing, keeping only relevant, reachable results), `news_search` (Bing News RSS; `source=` one outlet),
  `scholar_search` (arXiv + Crossref + PubMed), `fetch_page` / `scrape_page` (Chromium fallback for bot checks
  and JS pages; block pages reported as blocked), `weather` (Open-Meteo), interactive browser.
- Agents that only searched are made to open 2-3 sources before answering.
- **Forums and code hosts** (`app/browsing/community.py`, 2026-11-05): `forum_search` (Hacker News via Algolia
  incl. "Show HN" launches, Reddit, Lemmy, and ANY other site named in `sites=`: Discourse forums through
  their JSON, everything else through a site-limited web search), `read_thread` (any link: HN/Reddit/Lemmy
  threads with top comments, Discourse topics, GitHub READMEs, else the general page reader with its
  Chromium fallback), `github_search` (stars, created/pushed dates, topics; `created_after` for new projects).
  Reddit: its JSON/HTML answer 403 here, so without a key we read its public RSS, one request every 6 s,
  cached 15 min, and a 429 is reported (never retried around). Optional vault secrets: `reddit/oauth`
  (`{"client_id","client_secret"}` of an app from reddit.com/prefs/apps: official API with scores) and
  `github/token` (higher rate limit).
- **`web_research`** (`app/research.py`): a topic across many sites at once, in the background: finds
  sources (forums, any named domains, GitHub, 4 web-search phrasings, news), the model picks the most
  useful from their titles, reads up to 60 (4 at a time), extracts rows from each (bigsummary's row
  extraction), merges duplicates across sources and saves an Excel sheet ranked by how many sources
  mention each item, with links. `criteria=` (plus the person's own words) filters sources, rows and the
  final list. Live 2026-11-05: "android apps", 20 sources -> 58 apps in ~5 min.

**Gadget and product research** (2026-11-14, the owner's main research use)
- `gadget_search` (`app/browsing/stores.py`): ~20 Indian brand stores at once through Shopify's public storefront
  JSON (boAt, Noise, Portronics, Zebronics, Ambrane, UGREEN India, Stuffcool, Callmate, Wecool, Ultraprolink,
  Lifelong, Agaro = agarolifestyle.com, Bigsmall, Atomberg, Geeklane = geeklane.in, Sharge, Conceptkart, Instacuppa,
  Xech; DailyObjects via site-limited web search). Price, MRP, discount, stock, listing date, link; sorts, price
  filters, `days` for newest listings, `save=true` for a sheet. ~10 s for all stores. List: `GADGET_STORES` in .env.
  Left out: octor.in (now an unrelated blog). Shopify throttles bursts from this server (429
  "local_rate_limited", product `.js` pages most): answers cached 10 min; `product_page` falls back to the browser.
- `web_research` adds store products as rows (sites "all" includes `stores`; `sites="stores"` alone).
- Prompts (`PRODUCT_HINT`): brand stores first, then `product_search` (Amazon/Flipkart/Reliance), `product_page`,
  YouTube reviews. The research specialist now really has `product_search`/`product_page`.
- **Link check** (`app/orchestrator/links.py`): every final answer's links are compared with this turn's tool
  results; a link on a returned site that isn't one of them (Gemma shortens long Shopify links) is replaced by
  the closest real one.

**YouTube** (2026-11-14, `app/browsing/youtube.py`, official YouTube Data API v3, key = vault secret `youtube/api`)
- The company's channels are **TechWiser** (`@techwiser`) and **GadgetWiser** (`@wisergadget`, chosen 2026-11-14: the
  `@gadgetwiser` handle is an empty channel); change them with `YOUTUBE_OWN_CHANNELS`. Agents call them "ours".
- `youtube_channel` (subs, views, latest uploads with views/day, Shorts vs long, engagement, upload pace; "ours" or
  any channel), `youtube_search` (video links + stats; order, days, channel), `youtube_video` (details, tags,
  description, top comments). Public numbers only: watch time, CTR, revenue and audience need the YouTube Analytics
  API with the channel owner's OAuth sign-in (not built). Quota 10k units/day, a search = 100; answers cached 10 min.
- `fetch_page` on a watch link also shows YouTube's shopping shelf (products with shop links), which the API lacks.
  Instagram and TikTok send everything to a login page from here.
- Questions about "our channel" triage as **work** (admin/researcher); other channels and topics stay private.

**Scouts: research that keeps running** (2026-11-14, `app/scout.py`, page **Scouts**, `routes_scouts.py`)
- A department's standing research task. The owner's two (Research), separate in everything (findings, runs,
  file, notifications, review lessons; all keyed by scout id, never mixed in a prompt):
  - **"TechWiser Instagram"** (was "Rare gadget finds"; old file `Scout/Rare gadget finds.xlsx` left in place):
    Reels brief, `count` rule, any 2 of the 7 criteria below.
  - **"GadgetWiser"**: `score` rule, weights affordable 30 / everyday problem 25 / aesthetic 22 / looks one way,
    acts another 18 / fresh 15, pass mark 6.5, plus **"Available in India" required** (weight 0, met at 7); market
    "India" adds a "<product> price India" web search to the evidence. Presets: `scout.PRESETS`.
- Per scout: `brief` (who it's for, in every prompt), `market`, criteria with weight 0-100, "Met at" and a
  `required` flag (must be met under either rule). Every
  `interval_minutes` (default 120) the scheduler (`scripts/scheduler.py`, own thread per scout) runs a round:
  search ideas (round-robin + model's fresh ones) → YouTube results page, Reddit, gadget stores, web → specific
  products named by the model → **shortlist** of the most promising → per product a reference video (YouTube API,
  must really show it) + store/web evidence → each criterion scored 0-10.
- **Pass rule** (owner): `count` = at least `min_met` (2) criteria met, each met at its "Met at" score (7); or
  `score` = weighted average ≥ threshold and every criterion met. Criteria: daily use, multi-feature, transformation,
  rare find, unexpected function, camera wow, cool factor. All editable on the Scouts page; editing re-sorts the
  judged findings at once (`scout.rescore`); people keep/reject finds (rejections steer later searches), with an
  optional **reason** (`review_note`): `scout.lessons` feeds this scout's own kept/rejected finds and reasons into its
  idea, pick, shortlist and judge prompts, so it learns the team's taste. Each person can **mute** each scout
  (`scout_mutes`, `PUT /v1/scouts/{id}/mute`); the bell skips them for that scout only.
- The Scouts page never reloads itself (owner): rounds run in the background and new finds arrive as a
  notification; the page changes only on Refresh. Keep/Reject/reason/Mute update just that card or button.
- Everything judged is stored (`scout_findings`, statuses new/kept/rejected/below), so nothing is judged twice;
  rounds in `scout_runs` (migration **0011**). Report file `shared/<dept>/Scout/<name>.xlsx` (Findings, Below
  threshold, Runs, Settings) rewritten every round; department members get a bell notification for new finds.
- Access = the department's files ACL on `Scout/` (members only). YouTube budget: `daily_lookups` (30 each) video
  searches per scout per day (100 units each of the shared 10k: two scouts = 6k, the rest for chat). Run now on a paused scout runs one round, then pauses.

**Voice input** (2026-11-14): a mic button in the chat box on the website and in the Android app. The recording goes
to `POST /v1/voice/transcribe` (`routes_voice.py`: signed in, 12 MB, 20 a minute per person) → the local Whisper
service `scripts/stt_server.py` (127.0.0.1:8011, **Whisper large-v3-turbo** via faster-whisper on the GPU, ~2.3 GB,
~0.5 s for 11 s of speech; CPU int8 fallback). Text lands in the box to check and send (then the usual triage);
audio and text are never stored or logged. Android records AAC in the app (RECORD_AUDIO) and falls back to the
phone's recognizer if the mic can't be used. Setup: docs/SERVER_SETUP.md "Voice input".

**Sharing with colleagues** (2026-11-05)
- "Send this to sayan": `share_with` delivers a note and files to another user (notification + Files > Shared
  with me). Private chat: the sender confirms; work: same-department members only. Accounts: notifications
  only, no files, nothing crucial (patterns + model check). Nobody shares passwords, OTPs or card numbers.

**Long tasks in the background** (2026-11-05)
- `start_background_task` (`app/background.py`): any big task; the agent works alone for up to 30 tool
  rounds (no approvals/confirmations/live browser/wallet, no nested jobs) and saves a Word document, an
  Excel sheet or both. `save_table` saves a table the agent compiled as .xlsx/.csv.
- Every job (`app/jobs.py`, also mail_to_sheet) ends with the bell + browser pop-up; with `email=true` the
  result is emailed (from `mail/notify`) and with `remind_at` a reminder to look at it is set.

**Shopping**
- `product_search`, `product_page`. From this server (2026-11-04): Reliance Digital works to checkout;
  Flipkart search works but its product pages are blocked (429); Amazon and Croma block the browser. Blocked
  shops are reported as such, with a fallback to Reliance Digital and the shop's link for the person.
- A shopping specialist asks "personal or work?", fills the cart, signs in and pays with the person's **saved
  details** (`list_saved_details`, `fill_saved`: values never reach the model; logins only on their own site,
  cards/UPI only on known shops and payment gateways), asks for CVV/OTP/unsaved passwords through a **secure box**
  (`ask_person_for_secret`), and places the order with `browser_commit` after the Personal/Work card.
- `ask_person_to_browse` shows the **live browser** for CAPTCHAs and app approvals.
- Saved website sessions (cookies): private ones encrypted with the person's key; work ones in the vault.
- My account > **Saved details**: add/delete logins, cards, UPI IDs, addresses; "Show" needs the account password.

**Mail** (real IMAP/SMTP once a mailbox is connected)
- People connect their own mailbox under My account: **Sign in with Google** (paste back the 127.0.0.1 address
  the browser ends on) or **Sign in with Microsoft** (device code), logging in with XOAUTH2 and refreshing
  tokens automatically (`app/mail/oauth.py`); or address + **app password** (provider chosen from the address,
  `app/mail/providers.py`). The sign-in buttons stay grey until the admin adds `oauth/google` / `oauth/microsoft`
  (docs/SERVER_SETUP.md "Mail sign-in"). Gmail answers a normal password with AUTHENTICATIONFAILED (explained to
  the person); Outlook refuses passwords entirely.
- `summarize_mail` (gist, action, deadline per email), read, search, open, mark, send (confirmed).
- **Counting by time** (2026-11-05): `read_mail`/`search_mail`/`summarize_mail` take `hours` (24 = last day):
  exact arrival time (IMAP INTERNALDATE), read and unread, every folder on Gmail minus sent/drafts. Before
  this, "how many emails in the last hours" made the agent call `summarize_mail`, which by default counts only
  UNREAD mail, and it told the admin contact@/career@ were empty. An empty unread-only result now says read
  mail may exist.
- Opt-in morning digest that also sets reminders for deadlines found in mail.

**Files and documents**
- "My files": encrypted with the person's key, 5 versions, 7-day trash, 500 MB each; View/Upload/Download in UI.
- Work: department folder `shared/<dept>/` (+ data areas like `research/`), undo copies kept 30 days.
- Templates: `{{ placeholders }}` filled exactly (text and Word, line-item table rows), or an example document
  imitated (Word layout kept); batches from a JSON list or CSV. Company templates on the Templates page.

**Data and dashboards**
- `run_python`: pandas/numpy code in a sandbox (`app/sandbox.py`: user `nobody`, no network, rlimits, 60 s, audit
  hook), with copies of chosen files (attachments, My files, department files) in `input/`; `output/` files can
  be saved. `make_dashboard`: KPIs, bar/hbar/line charts, tables, notes (`app/dashboards.py`), drawn under the
  reply by `renderDashboard` (hover tooltips, data tables, CSV download, light/dark). Dashboards are stored on the
  final assistant message (encrypted with private chats). An `analyst` specialist uses both.

**Reminders and notifications**
- Natural times ("tomorrow 9am", "every weekday"), repeats, bell + pop-ups, optional email.

**Work and admin**
- Departments with quotas, approvals (Approvals page), accounts inbox (forwarded orders/bills/bookings), NSFW
  reports, Users page (departments, reset password, disable, **erase data**), Credentials page (vault),
  Overview dashboard, server alerts under the admin's bell.

**Operations**
- Grafana dashboards (LLM & ACL; Diagnostics & Logs), Prometheus alert rules (incl. disk space) delivered to the
  admin bell, backups with verified restore, vault key rotation script, load test, connectivity check.

---

## 5. Architecture

### 5.1 Processes and ports (all on this one host, all bound to 127.0.0.1)

```
                     Internet
                        │  (Cloudflare quick tunnels: random https URLs)
          ┌─────────────┴───────────────┐
     app-tunnel                  grafana-tunnel
          │                             │
   gateway :8000  (FastAPI, uvicorn) ── metrics :9100 ◄── prometheus :9090 ──► alert rules
     │   │   │  └─ /ui/  (plain JS, strict CSP)          │        └─► POST /v1/ops/alerts (admin bell)
     │   │   │                                           └──► grafana :3000 ◄── loki :3100 ◄── alloy :4317 (OTLP logs)
     │   │   └──────► vLLM :8001  (Gemma 4 26B INT4, GPU)
     │   └──────────► postgres :5432 (app tables + LangGraph checkpoints)
     │               redis :6379 (quotas, concurrency slots)
     │               minio :9000 (attachments, files, versions, saved logins; encrypted blobs)
     │
     └──► agents' web traffic ──► SOCKS 127.0.0.1:1080 ══SSH══► owner's VPS (Mumbai) ──► websites
          (httpx + headless Chromium, through the network guard)

   scheduler  (scripts/scheduler.py): due reminders every 30 s, digests every 5 min, cleanup hourly, scouts
   stt :8011  (scripts/stt_server.py): Whisper speech to text for the chat box's mic, GPU
   retention  (scripts/purge_history.py hourly): deletes attachments older than 3 days
```

### 5.2 A chat message, end to end

```
POST /v1/chat/stream ── app/api/routes_chat.py
  │ token → subject (roles re-read from DB) + chat key (sealed in the JWT)
  ▼
guards.triage (LLM, app/request_manager/guards.py) ── NSFW? → block + report to hr/admin
  │                                       │
  │ private (default)                     │ work (company data/systems) or company purchase
  ▼                                       ▼
execution.run(chat_key=…)                Request Manager (app/request_manager/router.py)
 checkpoint "private:<user>:<thread>"     authorize department → quota → concurrency → run graph
 encrypted with the person's key          checkpoint "<user>:<thread>" (NOT encrypted: an approver
 tools = PRIVATE_TOOLS                     must resume it); tools = DEPARTMENT_TOOLS[dept]
  │                                       │ order/bill/booking from anyone → accounts inbox
  ▼                                       ▼
LangGraph graph: single (agent ⇄ tools) | agents (supervisor → specialist ⇄ tools → writer) | team
  │ every tool: ACL check (app/acl/engine.py) + department/private scoping (app/orchestrator/tools.py)
  │ critical tool → interrupt() → pause
  │    private: confirm card → POST /v1/chat/threads/<id>/confirm → execution.resume_private
  │    work:    Approvals page → /v1/approvals/<id>/approve → execution.resume
  ▼
reply streamed (Markdown) → UI; work exchanges are also copied into the encrypted chat
```

### 5.3 Code map

| Path | What |
|---|---|
| `app/main.py` | FastAPI app, routers, CSP headers, error handling |
| `app/api/` | `routes_chat` (chat, confirm, attachments), `routes_request` (/v1/request, approvals, inbox, reports), `routes_auth` (login, users, erase), `routes_admin` (policies, vault secrets), `routes_me` (prefs, mailbox, notifications, reminders, My files), `routes_templates`, `routes_browser` (live view), `routes_ops` (alerts), `routes_dashboard` |
| `app/request_manager/` | `guards` (triage, NSFW, forwarding), `router` (RM pipeline), `approvals`, `quota`, `concurrency`, `retention` |
| `app/orchestrator/` | `tools.py` (all 53 tools, ACL-gated, `DEPARTMENT_TOOLS`), `nodes.py` (single agent, run_tool + interrupt), `specialists.py` (9 specialists + supervisor + writer), `graph.py`/`agents_graph.py`/`multi_graph.py`, `execution.py` (run/resume, private confirm), `context.py` (context vars that must never enter state), `progress.py` |
| `app/departments/packs.py` | **source of truth** for departments, grants, tools, critical tools, `PRIVATE_TOOLS` |
| `app/acl/engine.py` | deny-by-default PDP; `$self` owner rules; audit log |
| `app/browsing/` | `netguard` (public addresses only; DoH via proxy), `fetch` (httpx, proxy-aware), `search` (DDG/Bing/APIs/scholarly), `extract` (text, block-page detection), `browser` (Chromium sessions, commit/sensitive rules, live view), `products`, `logins`, `weather` |
| `app/mail/` | `connector` (IMAP/SMTP, password or XOAUTH2, guarded for self-configured boxes), `oauth` (Google/Microsoft sign-in, token refresh), `providers`, `summary` (LLM reading of batches) |
| `app/sandbox.py`, `app/dashboards.py` | code runner for agents; dashboard spec checking |
| `app/storage/` | `objects` (MinIO + ACL), `attachments`, `userfiles` (My files), `deptfiles` |
| `app/documents/` | `engine` (sandboxed Jinja, Word fill/imitate), `store` (company templates) |
| `app/reminders/` | `when` (time parsing), `service` (reminders, notifications, delivery), `delivery` (email), `digest` |
| `app/vault/` | `aesgcm_vault` (envelope encryption, master key in .env), `user_keys` (per-person chat key), `notes` (server notes key) |
| `app/privacy.py` | erase a person's data |
| `app/web/` | `index.html`, `app.js` (no framework; `md()` renderer, live browser panel, `renderDashboard`), `style.css` ("Executive Slate" light / "Executive Dark Slate" dark design, TW-ai brand, theme toggle kept in localStorage), `fonts/` (Newsreader, Geist, JetBrains Mono, bundled: OFL). Icons are Material Symbols paths inlined in `MI` (app.js). |
| `scripts/` | `start_native.sh`, `run_vllm.sh`, `scheduler.py`, `backup.sh`/`restore.sh`, `rotate_vault_key.py`, `eval_triage.py`, `load_test.py`, `check_connectivity.py`, `seed_departments.py`, `init_db.py`, `generate_env.py` |
| `deploy/native/` | prometheus (incl. alerting → gateway), loki, alloy, grafana provisioning; `deploy/prometheus-rules.yml` |

### 5.4 Data (Postgres tables, migrations 0001-0007)

`users` (roles, wrapped chat keys, prefs, digest time), `acl_policies`, `departments`, `vault_secrets`,
`approvals`, `department_tasks` (accounts inbox), `reports` (NSFW, finance notices), `audit_log`, `chat_threads`
(encrypted titles), `chat_history` (attachment refs), `user_files`, `reminders`, `notifications`, `ops_alerts`,
`doc_templates`, `wallet_items` (saved details, encrypted per person; migration 0008), plus LangGraph's `checkpoints`, `checkpoint_writes`, `checkpoint_blobs`.

MinIO keys: `chat_attachments/<user>/<id>`, `userfiles/<user>/<id>/<version>`, `browserstate/<user>`,
`shared/<dept>/…`, `research/…`, `reports/…`, `_versions/<key>/<time>`.

### 5.5 Who can read what (encryption layers)

| Data | Protected by | Readable by |
|---|---|---|
| Private chats, chat titles, attachments, My files, private saved website sessions, **saved details** (logins, cards, UPI, addresses) | the person's **chat key** (scrypt-wrapped by password and recovery code; carried sealed in the JWT) | only that person (and the server while handling their request) |
| Vault secrets: mailbox logins, search keys, work saved logins, notes key | **vault master key** (`VAULT_MASTER_KEY` in `.env`) | the server; admin can't read `mail/personal/*`, `system/*`, `browser/*` |
| Reminders, notifications | **notes key** (a vault secret) | the server (so they fire while the person is away) |
| Work runs, approvals, inbox, reports, audit | plain DB rows | the department / approver / admin per ACL |

`.env` is not in backups; with the DB it unlocks the vault, so keep them apart.

### 5.6 Network path

This server's own network poisons DNS (Wikipedia, Google → Facebook/Twitter IPs) even with 1.1.1.1 configured,
cuts TLS to many sites, and has hour-long outages. So the agents' traffic goes through **the owner's VPS** in Mumbai:
`start_native.sh` keeps `ssh -N -D 127.0.0.1:1080` to it up (host, port and key are `PROXY_SSH*` in `.env`; the
VPS user may only port-forward), and `OUTBOUND_PROXY=socks5h://127.0.0.1:1080`
routes httpx, the search APIs and Chromium through it. The network guard still refuses private addresses,
re-checking names over DNS-over-HTTPS through the proxy. Model server, DB, MinIO, mail and tunnels stay direct.
Through the proxy (2026-11-05): 28 test sites OK, Booking.com and Amazon OK in Chromium only (AWS WAF's
JS check passes by itself: `browser._pass_challenge` waits up to 12 s for it), 7 refuse even Chromium.
**Why they refuse** (diagnosed 2026-11-05): the VPS is an OVH address (AS16276, a data-centre network).
MakeMyTrip, IRCTC and Croma sit behind Akamai, which answers "Access Denied" at the edge to data-centre
addresses before any page code runs; Reuters (DataDome), NDTV, The Hindu, Britannica likewise. No browser
setting changes that. Not built, on purpose: stealth plugins, CAPTCHA solvers, fingerprint spoofing or
rotating residential proxies (evasion; against the sites' terms; risks people's accounts). Real routes:
official/partner APIs (Booking Demand/Affiliate API, Amadeus Self-Service for flights/hotels, IRCTC
authorised agents), or handing the link to the person (shops already do this).

---

## 6. Limitations and known gaps

**Not verified end to end**
- Signing in with saved details and paying through the secure box on a real shop (unit-tested with a fake
  browser; the agent was seen asking "personal or work?" live).
- A real order on any shop (product search works on Amazon/Flipkart/Reliance; checkout + live-panel sign-in +
  confirm path is built and unit-tested, not run against a real account).
- Mail summaries and the digest against a real mailbox (no test mailbox exists).
- Google/Microsoft sign-in with a real account (needs the owner's app registrations; flows unit-tested, and
  both IMAP servers checked live to accept the XOAUTH2 handshake).
- The Docker/compose path (no Docker here) and a full (non-verify) restore.

**Stubs (need vendor accounts):** payment gateway, SMS, ERP/ledger (`balance_lookup`, `manage_order`,
`manage_bill`, `inventory_*`, `track_shipment`, `send_notification` return `[stub]`).

**Security / ops gaps**
- Public login page on https://tw-ai.in (no Cloudflare Access yet, also not on Grafana), no failed-login limit (owner's choice).
- DNS-rebinding window between the guard's check and Chromium's/the proxy's own lookup.
- Saved details are typed by the server into pages the agent chooses: the site checks (login = its own site;
  card/UPI = allowlisted shops and gateways in `app/wallet.py`) are the main defence against a page tricking the
  agent; review the allowlist before adding sites.
- The leak guard (refusing long URLs/queries after reading mail or files) is a partial defence against
  prompt-injected exfiltration, not a proof.
- The code sandbox is user `nobody` + rlimits + an audit hook, not kernel isolation (no user namespaces,
  seccomp or firewall rights in this container). Code that defeats the hook could reach 127.0.0.1 services as
  `nobody` (Redis has a password since 2026-11-13).
- Backups stay on this server (`BACKUP_DEST` empty), and nothing runs them on a schedule here.
- `GatewayDown` can't reach the admin bell (it's delivered by the gateway). No email/chat alert channel.
- The proxy depends on one VPS with a shared IP; if it's down, agents' web access fails (no automatic fallback
  to direct).
- Disk: ~3.9 GB free of 40 GB (2026-11-14).

**Product limits**
- 64k context; pages are clipped (20,000 chars per page, older tool results to 3,000); overlong prompts drop their oldest turns. Prompts near 50k tokens take minutes to prefill on the Quadro.
- Gemma sometimes needs nudges (it asked "are you sure?" in words before deleting; fixed by prompt wording).
- Keyless web search is decent through DuckDuckGo but rate-limits; a Tavily/Serper key would be better.
- Reloading the app keeps you signed in for as long as the session is valid (1 h, renewed while open).
- Keyless Reddit is RSS only (no scores) and rate-limited: a second request straight after the first gets 429.
  A `reddit/oauth` app key fixes both. Background jobs live in memory: a gateway restart loses them.
- Many shops/news sites block automated browsers regardless of network.
- Department mailboxes have no automatic digest; reminders are per person (no team reminders).
- Attachments in work chats aren't read (private only). Bell polls once a minute; pop-ups only while the app is open.
- Each work request runs in one department.

---

## 7. Next steps (suggested order)

0. **Register the mail sign-in apps** (Google Cloud: Desktop OAuth client, Gmail API, publish the consent screen;
   Entra: public client, IMAP.AccessAsUser.All + SMTP.Send) and add `oauth/google`, `oauth/microsoft` on the
   Credentials page; then connect a real mailbox both ways.
1. **Try a real personal order** (cheap item) via the globe button: sign in to Amazon in the live panel, ask
   the assistant to order, confirm the card. Fix whatever the real checkout throws up.
2. **Connect a real mailbox** (sign-in or app password) and check `summarize_mail` + the morning digest.
3. **Add a Tavily (free tier) key** on the Credentials page as `search/tavily`; optionally `reddit/oauth` and
   `github/token` for forum/GitHub research (see §4 Research).
4. **Off-server backups:** set `BACKUP_DEST` (rclone) and run `backup.sh` daily from the scheduler or
   `onstart.sh`.
5. **Cloudflare Access** in front of grafana.tw-ai.in (and maybe the app). Domain + named tunnel: done 2026-11-10.
6. **Proxy resilience:** health-check the tunnel and fall back to direct (or alert the admin) when it's down.
7. **Disk space:** clear caches (`uv cache clean`, old logs, `/root/.cache`), or move weights.
8. Vendor integrations when accounts exist (payments, SMS, ERP).

---

## 8. Gotchas for the next session

- **Specialists that answer without a tool** (2026-11-14): Gemma refused a research task naming 2026 as "the
  future" without searching, and the refusal became the answer. `specialists.NO_TOOL_NUDGE` sends a first reply
  without a tool call back once. Gemma follows the supervisor's task wording far more than the system prompt:
  routing hints (e.g. "use youtube_search") belong in `SUPERVISOR_PROMPT`.

- **Never run `tests/test_postgres.py` against the live DB.** Use a throwaway one:
  `su postgres -c "createdb -O ai ai_platform_itest"`, run with
  `POSTGRES_INTEGRATION=1 POSTGRES_DSN=<.env DSN with /ai_platform_itest>`, then `dropdb`. A run against the live
  DB on 2026-11-03 left a `user-<hex>` test account and checkpoints; deleting them needs the owner's OK
  (SQL in that day's conversation: delete rows whose ids start with `user-`).
- **Don't `source .env` in shell scripts:** `VLLM_EXTRA_ARGS` has spaces. Read KEY=value lines (see `backup.sh`).
- **After changing the triage prompt**, rerun `scripts/eval_triage.py` (~3 min). Watch "personal sorted as WORK":
  it must stay 0.
- **Chat requests run independently of the browser** (`routes_chat.chat_stream` worker thread,
  `app/inflight.py`): never start work from inside the streaming generator again.
- **Per-request graph state** (counters, reports, tasks) is reset on every new message in `execution.run`
  (`state.TURN_COUNTERS`/`TURN_LISTS`). Add any new per-request field there, or long threads stop working.
- **Python edits with `str.replace` can silently miss.** Assert the old text exists. And watch apostrophes inside
  single-quoted prompt strings (broke `guards.py` once).
- **`cloudflared tunnel login` can't finish here** (login.cloudflareaccess.org is unreachable): the owner
  downloads `cert.pem` in the browser and uploads it to `~/.cloudflared/`. The tunnel API itself works directly.
- **The network flaps.** If many sites fail at once, check `start_native.sh status` (proxy-tunnel) and
  `check_connectivity.py` before blaming code.
- **Context vars, not state:** the chat key (`user_key`), attachments (`attachment_text`, `attachment_files`) and
  the browser handover (`browser_request`) live in `app/orchestrator/context.py`; anything in graph state is
  checkpointed forever (and `str` fields are stored unencrypted).
- **Commits:** use the repo's existing author identity (see `git log`; no global git identity is set); push with
  plain `git push` (credentials in a repo-local helper, never print it). Live branch: `android-app`; never `main`.
- Routing around this network's filtering *on Claude's own initiative* was blocked as containment escape on
  2026-11-03; the VPS proxy was set up at the owner's explicit request with their own server.
