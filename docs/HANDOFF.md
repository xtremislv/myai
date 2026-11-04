# Handoff for the session on the server

> Start with `docs/SESSION_WRAPUP.md` (current summary and architecture). This file is the detailed log.

You are Claude Code running on the rented GPU server. The code was built and tested on a Windows machine with
**no Docker, GPU, real model or real MinIO**. Your job is first bring-up and tuning. Setup steps are in
`docs/SERVER_SETUP.md`.

## What this is
A self-hosted AI platform. Users get one chat box (`/ui/`). `guards.triage` sorts each message:
- **Private:** read-only web tools only (search, news, weather, pages, browser; nothing that books, buys or sends),
  no audit, encrypted with a per-user key that only their password or recovery code unlocks.
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
- **Private chat gets read-only web access** (owner, 2026-11-03): `PRIVATE_TOOLS` in `packs.py`, granted to everyone
  (`*`) as `web:search|fetch|browse` read. No company data, mail or `browser_commit` in private chat. Private chat
  has its own browser session per person, separate from work. Queries do leave the server (to Bing, or to Tavily/
  Serper if a key is set); results are stored only inside the encrypted chat.

- **Private chat also gets the person's OWN mailbox, files and reminders** (owner, 2026-11-03, second decision
  that day): personal mail category only, "My files" encrypted with their key, their own reminders. Company data
  still never enters private chat. Agents create, edit, move and restore files freely (old versions kept);
  **deleting a file needs a go-ahead**, like sending mail. In private chat the person confirms for themselves.
- **Reminders reach people in the app (bell + browser pop-up) and by email if they choose; a morning mail digest
  is opt-in per person** (owner, 2026-11-03).
- **Server alerts go to the admin's in-app bell** (owner, 2026-11-03), not email or chat services.

## This server (found 2026-11-02)
- **It is a container, not a VM** (vast.ai behind aiccloud.in): no `docker` command and no `CAP_SYS_ADMIN`, so
  `docker compose` can't run here. Run services directly: vLLM via `scripts/run_vllm.sh`
  (see "Without Docker" in `docs/SERVER_SETUP.md`). Postgres, Redis and MinIO still need native installs.
- vLLM 0.32.0 is installed in `/root/vllm-env` (Python 3.12). It sees the GPU (compute capability 7.5). On this card it
  needs `--dtype half` (bf16 is refused below 8.0) and uses the Triton attention backend. AWQ, GPTQ and
  compressed-tensors INT4 all accept compute capability 7.5, and AWQ and GPTQ have MoE kernels.
- **huggingface.co is unreachable from here:** DNS returns fake addresses and connections time out.
  `/root/models/gemma-4-26b` is empty. The owner must decide how to get weights onto the box.
- Gemma is trained in bf16. In fp16 it has a history of overflowing to inf or NaN. Check the first outputs for garbage.
- The root disk is 40 GB. The guide's 100 GB minimum isn't met, so keep the weights at INT4 and clear caches.
- The app's test venv is `.venv` (Python 3.12, installed with `uv`). Plain pip got stuck resolving dependencies.
- Running natively (2026-11-02): `.env` was made with `--native`. Postgres 16 (PGDG apt) and Redis 6.0 run on
  localhost, migrations are at head, and `tests/test_postgres.py` passes against a real Postgres. The gateway and
  retention loop run via `scripts/start_native.sh`. Still missing: MinIO (build from source, see SERVER_SETUP),
  the admin user, and the monitoring stack. Weights: `cyankiwi/gemma-4-26B-A4B-it-qat-AWQ-INT4` (compressed-tensors
  W4A16, from Google's QAT checkpoint) in `/root/models/gemma-4-26b`.
- Access is through a Cloudflare quick tunnel (`cloudflared`, started by `start_native.sh`): a random public
  `https://<words>.trycloudflare.com` address that changes on every restart (`start_native.sh status` prints it).
  Tailscale was tried and removed at the owner's request. The login page is public and failed logins are not
  limited (owner's decision), so prefer a named tunnel with Cloudflare Access once there is a domain.
- vLLM needs `gcc` (Triton compiles kernels at startup) and `deploy/patches/vllm-0.32.0-turing-head512.patch`:
  Gemma 4's 512-wide full-attention heads exceed Turing's 64 KB shared memory with vLLM's default tiles. With both,
  the model loads (16.6 GiB), has a 157k-token KV cache (about 19 concurrent 8k chats) and answers coherently in fp16
  at about 90 tokens/s for a single chat. vLLM does not log prompts (verified).
- Admin pages: Users (departments, reset password, disable/enable; migration 0005 adds `users.disabled`, checked at
  login and on every request) and Credentials (vault secrets; values are write-only). An admin reset removes the
  password's copy of the chat key but keeps the recovery code's copy, so "Forgot password?" still restores chats;
  chats encrypted under a key nobody can unlock any more are left out of the chat list.
- The web UI was restyled (brand mark, sign-in card, welcome screen, sidebar sections). Same CSP, same class names.
- `huggingface.co` works only because of `/etc/hosts` entries the owner added (real addresses from DNS-over-HTTPS).
  CDN addresses rotate, so redo them if downloads fail.

## Agents with web, browser and mail (2026-11-03)
- **`agents` mode** (`app/orchestrator/specialists.py`, `agents_graph.py`): a supervisor hands one task at a time to
  specialists (research, browser, mail, booking, operations) and a writer answers from their reports. Each
  specialist's tools are narrowed to the department's tools, and the ACL still checks every call. A specialist is
  offered only if the department has one of its core tools (researchers get no booking agent). Triage now sends
  multi-step messages here (`steps: many`), private ones included; `team` runs only when a caller asks for it. Bounds: 4 hand-offs, 6-12
  tool rounds per specialist, recursion limit 150.
- **Web tools** (`app/browsing/`): `web_search`, `news_search`, `fetch_page` (HTML, PDF, text), `scrape_page` (text,
  links, tables or a CSS selector), and an interactive headless Chromium (`browser_open/read/click/type/select/
  scroll/back`) with one isolated session per person, closed after 10 idle minutes. Pages come back as their text
  plus numbered controls. Web and mail text is wrapped as UNTRUSTED CONTENT (prompt-injection defence).
- **Safety, enforced in code:** `netguard` refuses anything that isn't a public http(s) address, on 80/443/8080/
  8443. httpx checks the very address it dials; Chromium checks every request and websocket. A DNS-rebinding
  race between Chromium's check and its own lookup is a known gap. Clicks that book, pay, send or submit a
  non-search form are refused by `browser_click` and must go through **`browser_commit`, which needs approval**,
  as does **`send_mail`**. Passwords, card numbers, CVVs and one-time codes are never typed.
- **Search from this server:** many sites are DNS-poisoned or have their TLS cut here (Google, DuckDuckGo,
  Wikipedia, Yahoo, Startpage, Times of India...). Bing works but degrades results for bots, so keyless web search
  uses Bing's results page plus its RSS feed, with irrelevant results dropped. News (Bing News RSS) is good. **For
  good web search, store a Tavily or Serper key** in the vault as `search/tavily` or `search/serper` (Credentials
  page). Both APIs are reachable from here.
- **Mail** (`app/mail/connector.py`): real IMAP/SMTP once a vault secret exists: `mail/<finance|career|contact>`, or
  `mail/personal/<user id>`, holding JSON `{imap_host, imap_port, smtp_host, smtp_port, username, password, from}`.
  Until then the mail tools answer `[stub]`. Departments can now send from their own box (write grants added).
- `weather` uses Open-Meteo (free, no key). `fetch_page` retries pages that show a bot challenge in real Chromium.
  Search queries have calendar dates stripped (`search.clean_query`), and agents are told the date is real: Gemma
  otherwise decides 2026 is "in the future" and refuses to report news. Private chat streams even with tools
  (`llm.chat(stream=True, tools=...)`); the UI replaces streamed text with the final reply.
- **Context:** vLLM runs with `--max-model-len 8192`. Older tool results are cut to 400 characters before going back
  to the model, and a page is at most 3,500 characters. Raising it to 16-32k would let agents read more per step.
- Playwright (Chromium headless shell) is installed in `.venv` and `~/.cache/ms-playwright`. After a rebuild:
  `.venv/bin/playwright install --with-deps chromium`. After changing `packs.py`: `python -m scripts.seed_departments`.

## Files, mail summaries, reminders, alerts (2026-11-03, later)
- **Tools** (all departments and private chat): `list_files, read_file, write_file, append_file, edit_file,
  move_file, copy_file, delete_file, restore_file`; `set_reminder, list_reminders, cancel_reminder`;
  `summarize_mail` (reads up to 20 emails in full and returns gist / action / deadline per email, via
  `app/mail/summary.py`, batches of 4). New specialists: `files` and `organizer`; `mail` got summarize and reminders.
- **Private files** (`app/storage/userfiles.py`, table `user_files`): path and every version encrypted with the
  person's chat key; MinIO keys `userfiles/<owner>/<id>/<version>`; 5 versions; trash 7 days; 500 MB each.
  **Work files** (`app/storage/deptfiles.py`): ObjectStore + ACL, default folder `shared/<department>/` (grants
  added per department), undo copies in `_versions/` kept 30 days. Verified encrypted at rest in real MinIO.
- **Confirmations in private chat:** `/v1/chat` returns `outcome: "confirm"`; the UI shows a card with the exact
  email or file; `POST /v1/chat/threads/<id>/confirm`. Read from the checkpoint's pending writes
  (`execution.private_pending`), resumed with the person's key (`resume_private`). Verified live with Gemma.
- **Reminders/notifications** (`app/reminders/`, tables `reminders`, `notifications`): text encrypted with the
  vault notes key. `scripts/scheduler.py` (started by `start_native.sh`) delivers due reminders every 30 s, runs
  digests every 5 min for people whose hour has come, and cleans up hourly. Time words are parsed in
  `when.py` in the person's timezone (prefs, default Asia/Kolkata).
- **Mail digest** (`digest.py`): opt-in hour under My account; reads unread mail since the last digest, summarizes,
  sets one reminder per deadline (deduplicated by Message-ID), optional email copy. **Not tested against a real
  mailbox**: no test mailbox exists here. Note this network cuts TLS to Google, so Gmail IMAP may be unreachable.
- **Personal mailbox setup** is self-service (`PUT /v1/me/mailbox`, tested login, `mail/personal/<id>` in the
  vault). Servers must be public addresses on 993 / 465 / 587 (SSRF guard). The admin can no longer read, change
  or list `mail/personal/*` or `system/*` secrets (ACL deny + Credentials API refuses them).
- **ACL change:** a `$self` allow now beats a role deny on the person's own data, so the admin can use their own
  personal mail (the admin's deny on `mail:personal/*` used to block that too).
- **Leak guard:** after a conversation reads mail or files, web tools refuse URLs with >100 characters of query
  data, URLs over 300 characters, and searches or typed text over 150 characters (`_leaks` in tools.py). This is a
  partial defence against prompt-injected exfiltration, not a proof.
- **Alerts:** Prometheus -> `POST /v1/ops/alerts/api/v2/alerts` (bearer `ALERT_WEBHOOK_TOKEN`) -> admin bell, once
  when firing and once when resolved (`ops_alerts`). New rules DiskAlmostFull/DiskFull on `host_disk_free_ratio`.
  Verified end to end on this server.
- **Erase a person's data:** Users page > Erase data (`POST /v1/auth/users/<id>/erase`, `app/privacy.py`): chats,
  attachments, files, reminders, notifications, mailbox login, their work requests; optionally the account.
  NSFW reports and the audit log are kept.
- **Vault key rotation:** `scripts/rotate_vault_key.py` (stop gateway + scheduler first). Not run on this server.
- **Context 16k:** `VLLM_MAX_MODEL_LEN=16384`; KV cache 280k tokens (17 full-length chats). Pages are clipped at
  6,000 characters and older tool results at 800. Load test (`scripts/load_test.py`, ~1.5k-token prompts):
  1 chat 73 tok/s; 4 at once 39 tok/s each; 8: 25; 16: 15 (215 tok/s total), first word under 0.6 s. An 8k-token
  prompt takes ~7.6 s to the first word (prefill on Turing).
- **Triage** now keeps "my files / my mail / remind me" private and sends team/department files and reminders for
  others to work. Eval: 65 cases x 3 runs x 2 personas = 390/390 correct, 0 personal sorted as work.
- **Backups natively:** `backup.sh` / `restore.sh` detect no-Docker hosts; `restore.sh --verify` restores into a
  scratch database and folder and checks them. Verified on this server (8 users, 342 checkpoints, migration 0006).
  `backup.sh` used to `source .env`, which broke on `VLLM_EXTRA_ARGS`; it now reads KEY=value lines.

## Connectivity, research quality, templates (2026-11-03, evening)
- **Network from this server** (`scripts/check_connectivity.py` re-checks it): DNS answers are poisoned on the
  path, even with `nameserver 1.1.1.1/8.8.8.8` already in /etc/resolv.conf (Wikipedia, Google, YouTube, Reddit
  resolve to Facebook/Twitter/Dropbox addresses), many TLS connections are cut mid-handshake (BBC, The Hindu,
  Livemint, Economic Times, data.gov.in), and others are "network unreachable" (Reuters, NDTV, DuckDuckGo, Brave,
  Times of India). Working: Bing (RSS + News RSS), Tavily API, arXiv, Crossref, PubMed, RBI, india.gov.in, World
  Bank, Moneycontrol, Booking, and at times GitHub, PyPI, Open-Meteo, Amazon, Flipkart. **Reachability also
  varies by the hour**: at 14:00 UTC almost all outbound HTTPS (even Bing and 1.1.1.1) timed out, while the
  tunnel stayed up. The owner was given steps to try encrypted DNS; that, and any proxy/VPN egress, is their call.
- **The `ddgs` (DuckDuckGo) library** was tried: every backend it uses is blocked here. Not added.
- **Keyless web search** returned results matching one query word (or none) for most queries. Now: Bing RSS ->
  Bing News -> rendered Bing, keeping results that match ~60% of the query words and dropping hosts this server
  can't reach (`search.relevant`, `search.reachable`). A Tavily or Serper key is still the real fix.
- **`scholar_search`** (new, all departments and private chat): arXiv + Crossref + PubMed in parallel, years
  stripped from queries.
- **Block pages**: `fetch_page`/`scrape_page` share `_readable`: a 401/403/429/503 or a page that looks like a
  block/CAPTCHA (`extract.block_reason`) is retried in Chromium, and if still blocked the tool says so instead
  of returning the block page as content. Chromium gets past Investopedia, IMF, RBI, PIB; not Britannica, AP,
  Stack Overflow, Indian Express, Amazon.
- **Agents read sources**: answers built only on web/news search snippets get one nudge to open the best 2-3
  results (`READ_SOURCES`, single agent and specialists). Verified live: every research answer opened pages.
- **Templates** (`app/documents/`, table `doc_templates`, migration 0007): tools `list_templates,
  read_template, create_from_template, save_template`; a `documents` specialist; a Templates page. Placeholders
  (`{{ x }}`, loops; Jinja sandbox, StrictUndefined) are filled exactly; missing values come back as MISSING and
  nothing is saved. Word: per-paragraph fill keeping styles; table rows with `item.` repeat per `items`. A file
  without placeholders is an example: the model rewrites it (Word: paragraph by paragraph, so layout survives),
  keeping the issuer's details and marking unknown subject facts `[TO FILL: ...]`. Batches: JSON list or a
  CSV/JSON `data_file`, `output_path` with placeholders. Sources: chat attachments (private, via the
  `attachment_files` context var), My files `templates/`, company templates (work only; ACL
  `template:<dept>/*`, `template:global/*` read for everyone at work). Verified live with Gemma and real Word
  files. `write_file`/`append_file`/`edit_file` now refuse .docx/.pdf/.xlsx and other binary formats.
- Triage eval: 71 cases x 3 x 2 personas = 426/426, 0 personal as work (adds attached-template and how-to cases).

## Shopping, live browser, Markdown, proxy (2026-11-03, night)
- **Owner decisions:** personal orders from private chat (the person confirms each order on a card), and
  company orders through Accounts with an approver. Sign-in, OTP and payment happen in a **live browser panel**
  in the chat, by the person; agents never type them.
- **Tools:** `product_search` (Amazon, Flipkart, Croma, Reliance Digital; JSON-LD, Amazon layout, generic price
  cards), `product_page`, `ask_person_to_browse` (hands the browser over; the chat shows the panel), and
  `browser_commit` now in private chat (resource `web:commit-private`, granted to everyone for themselves; always
  confirmed). A `shopping` specialist. "Add to cart", "Proceed to checkout" and "Use this address" are ordinary
  clicks; "Buy now", "Place your order", "Pay", and any other form submit still need `browser_commit`.
- **Live view** (`app/api/routes_browser.py`): JPEG screenshots on a canvas (CSP: no image sources), clicks,
  typing, keys, scroll, back, open. Typed text isn't logged; the agent's snapshot hides values of password, card,
  OTP and UPI fields. **Saved logins** (`app/browsing/logins.py`): private ones encrypted with the person's key in
  MinIO (`browserstate/<id>`), work ones in the vault (`browser/<dept>/<id>`, denied to the admin, hidden on the
  Credentials page). Erased with the person's data.
- **Reality check (tested 15:00 UTC):** Flipkart search works; Amazon serves its automated-access block page to
  this server, and Croma and Reliance Digital refuse too. Whether signing in via the panel or the VPS proxy changes
  that is untested. Ordering end to end has **not** been run against a real shop.
- **Triage:** personal shopping is private; a purchase for the company is forwarded to Accounts. The code now drops
  an "order" action on a private message (it used to forward a personal order to Accounts). Eval 450/450.
- **Markdown in the UI:** replies (also while streaming), request results, notifications and .md previews in My
  files are rendered by `md()` in app.js, which builds elements (no innerHTML); links only http(s)/mailto.
- **Outbound proxy:** `OUTBOUND_PROXY` routes agents' pages, search APIs and Chromium through a proxy; the network
  guard re-checks names through DNS-over-HTTPS via the proxy when local DNS is poisoned. `PROXY_SSH` makes
  `start_native.sh` keep an SSH SOCKS tunnel (127.0.0.1:1080) to the owner's VPS (host, port, user and key in
  `.env` as `PROXY_SSH*`; the VPS user is restricted to port forwarding). **Live since 2026-11-03 ~15:30 UTC**
  (`OUTBOUND_PROXY=socks5h://127.0.0.1:1080`), exiting in Mumbai (OVH). Connectivity check through it: 28 OK,
  10 site bot-blocks, no DNS or network blocks (was 12 OK, 22 cut, 2 poisoned). Amazon, Flipkart and Reliance
  Digital product search work; Croma, MakeMyTrip, IRCTC, NDTV, The Hindu and Reuters refuse automated visitors.
- **Keyless web search** now tries DuckDuckGo's HTML page first (reachable through the proxy; good results,
  ~2 s; sometimes rate-limits with HTTP 202), then Bing.

## Saved details, secure secrets, personal-or-work orders, simple mail setup (2026-11-03, late)
- **Owner reversed** "agents never type passwords or card details": they now may, from the person's own encrypted
  saved details, without the model seeing values (`app/wallet.py`, table `wallet_items`, migration 0008; tools
  `list_saved_details`, `fill_saved`, `ask_person_for_secret`; private chat only; admin denied `wallet:*`).
  Logins only on their own registrable domain; cards/UPI only on `PAYMENT_SITES` or sites added to the card.
  CVVs/PINs/OTPs are never stored. `browser_type` still refuses sensitive fields.
- **Secure box:** `ask_person_for_secret` → chat shows a password-style input → `POST /v1/browser/<scope>/secret`
  types into that field only if the page is still on the same site. Not logged or kept.
- **Personal or work:** the agent asks; the order card (`ConfirmOut.purchase`) offers "Personal: place it" /
  "Work: send to Accounts" (`as_work` → `guards.forward_to_account`, commit declined).
- **Mail:** address + app password; Gmail first (`app/mail/providers.py`); errors quote the provider and say how to
  fix it. Outlook/365 answer "Basic authentication is disabled" (OAuth needed, not built).

## Mail sign-in, data analysis and dashboards (2026-11-04)
- **Why Gmail "didn't work":** nothing was broken on the server side: Gmail's IMAP/SMTP are reachable, and a
  wrong app password gets Gmail's own `[AUTHENTICATIONFAILED]` explained back. No mailbox had ever been saved;
  every attempt came back 422 (before 2026-11-03 16:05, the old form; none since). Likely causes: a normal
  password instead of an app password, IMAP off, or an account that can't make app passwords.
- **OAuth sign-in** (`app/mail/oauth.py`): Google (auth code + PKCE; Desktop client, loopback address pasted
  back, or a web callback with a stable domain) and Microsoft (device code). Tokens live in the mailbox's vault
  secret (`"auth": "oauth"`), refreshed and saved back when they expire (Microsoft rotates refresh tokens). IMAP
  and SMTP log in with XOAUTH2 (`connector._imap`, `connector._smtp`). Both servers were checked live with a
  bogus token (they reject the token, so the handshake works). Needs the admin's `oauth/google` and
  `oauth/microsoft` secrets (SERVER_SETUP, "Mail sign-in"); **not yet tested with a real account**.
- Mail tool errors now say what the server said and what to do; the login check runs the server check once.
- **`run_python`** (`app/sandbox.py`): agent code runs as `nobody` in `/opt/agent-sandbox` (pandas, numpy)
  with rlimits, a 60 s wall clock, a clean environment and an audit hook (no sockets, processes, ctypes, or
  writes outside its folder). No user namespaces/seccomp here, so this is defence in depth, not a kernel
  sandbox. Install: `scripts/setup_sandbox.sh`.
- **`make_dashboard`** (`app/dashboards.py`): a JSON spec (kpis, bar/hbar/line charts, tables, notes), checked
  and stored on the final assistant message (`context.with_dashboards`), so it is encrypted with the private chat
  and returned in history. The UI draws it with DOM/SVG (`renderDashboard`), never innerHTML. New `analyst`
  specialist; `DATA_TOOLS` for every department and private chat; ACL grants `compute:python|dashboard` to all.
- Live run on Gemma: CSV -> run_python -> make_dashboard -> short reply, ~55-80 s, one or two self-corrected
  code errors on the way.

## UI redesign (2026-11-04)
- The web UI follows the owner's designs in `/root/stitch_tw_ai_ui_redesign/` (light "Executive Slate", dark
  "Executive Dark Slate"): TW-ai brand, serif display headings (Newsreader), Geist body, mono labels, a top bar
  with search (pages and chats, Ctrl/⌘K), theme toggle, bell and settings, and redesigned sign-in, chat,
  Overview and Users screens; every other page picks up the same styles.
- Fonts are bundled in `app/web/fonts/` and the CSP gained `font-src 'self'`; nothing loads from third parties.
- Mock content in the designs was left out on purpose: Okta/SAML sign-on, "remember device", SOC2/cluster/node
  labels, model picker, microphone, "hardware enclaves". Status lines use the real `/readyz`.
- Checked with a mocked-API Playwright run of every page in both themes and at phone width (no script errors).

## Not verified yet: check these first
1. **`docker compose up --build` has never run.** Check that the Dockerfile builds, that multi-word `VLLM_EXTRA_ARGS`
   splits into separate flags, and that the non-root app user works.
2. ~~**Collector to Loki.**~~ Verified natively (Alloy in place of the collector): gateway logs reach Loki with
   `service_name`, and both dashboards show live data. vLLM 0.32 renamed `vllm:gpu_cache_usage_perc` to
   `vllm:kv_cache_usage_perc`; the dashboard and the GpuCacheFull alert now use the new name.
3. **MinIO image.** Docker Hub returned nothing for `minio/minio`, so compose uses the tag from MinIO's own compose
   file on quay.io. Quay needed auth to confirm the tag: check that it pulls and that `mc ready local` works.
4. ~~**Attachments and retention against real MinIO.**~~ Attachments and personal files verified against real
   MinIO (2026-11-03): round trip works, nothing readable at rest. Retention purge still only tested with fakes.
5. **`scripts/backup.sh` and `scripts/restore.sh` with real Docker.** Natively verified with `restore.sh --verify`
   (2026-11-03). The Docker path is still only tested with fake `docker` and `rclone`. A full (non-verify) native
   restore has not been run, since it replaces live data.
6. **Does vLLM log prompts?** In 0.32.0 `--enable-log-requests` defaults to off, and `scripts/run_vllm.sh` refuses
   it. Still confirm in the vLLM log after a chat.
7. ~~**`role: tool` messages.**~~ Fixed 2026-11-02. Gemma 4's template silently dropped them, so the model never saw
   tool output and repeated the call. `llm.chat` now sends them as "Tool result:" user turns. Gemma also sometimes
   answers in its own `call:name(k="v")` syntax: `_parse_tool_call` accepts that too (literal arguments only).

## Then do these
1. Get the weights. The model id is now `google/gemma-4-26B-A4B-it`. The card is a Quadro RTX 8000 (48 GB, Turing,
   driver 580, CUDA 13.0): no FP8 and no bf16, so use AWQ or GPTQ INT4 weights with `--dtype half`.
2. ~~Benchmark and tune.~~ Done 2026-11-03: 16k context, numbers above. `--max-num-seqs 16` and the private-chat
   limits (2 per person, 16 total) fit the measured throughput.
3. ~~**Test triage accuracy with the real model.**~~ Done 2026-11-02 with `scripts/eval_triage.py` (54 labelled
   cases in `tests/eval/triage_cases.jsonl`, 3 runs, admin and logistics+research personas): 324/324 correct, and
   0/180 personal messages sorted as work, including 78 that use work words ("my bill", "order a pizza"). Triage
   now lists every department and marks the ones a person isn't in, so their requests name the real owner and the
   Request Manager refuses them, instead of landing in a department they have. The set is small and hand-written:
   add real misroutes to it as they appear, and re-run after any prompt or model change.
4. Real TLS and DNS (needs a domain from the owner). Load test done (above). Security review of the new code done
   2026-11-03 (SSRF guard on personal mailboxes, admin kept out of personal mail logins, leak guard, `pip-audit`
   clean); a full external review of the live deploy is still open.
5. ~~Alert notifications.~~ Done: Prometheus -> gateway -> admin bell. Email/chat delivery would need an SMTP or
   webhook target from the owner.
6. Connect a real mailbox to test summarize_mail and the digest end to end (and check whether this network reaches
   the provider's IMAP; Gmail may be cut like Google's other sites).
7. Add a Tavily or Serper key (Credentials page) for good web search.

## Known limits
- Payment, SMS and ERP tools are stubs until vendor accounts exist. Mail is real once its vault secret exists.
- Attachments work in private messages only. The AI reads text and PDF only, and unreadable files skip the NSFW check.
- Each work request runs in one department.
- A request already waiting for approval keeps the roles its requester had when they asked.
- SQLAlchemy 2.1 is newer than the OpenTelemetry instrumentor supports, so database spans are skipped and it logs one
  error at startup.
- Department mailboxes (finance, careers, contact) have no automatic digest; only personal mailboxes do. Agents can
  still summarize any mailbox they're allowed on request.
- Reminders are per person: there is no "remind the whole team" yet (logistics' `send_notification` is a stub).
- The in-app bell polls once a minute; browser pop-ups appear only while the app is open in a tab.
- `docs/PROGRESS_AND_ROADMAP.md` is historical. This file is the current state.

## Working here
- Read `CLAUDE.md` for conventions. Tests: `.venv/bin/python -m pytest tests/ -q`. Lint: `ruff check .`.
- `tests/test_postgres.py` needs `POSTGRES_INTEGRATION=1` and a **throwaway** `POSTGRES_DSN`.
- After changing a model, run `alembic revision --autogenerate`. A test fails if models and migrations differ.
- Never put message content in logs, spans or exception text.
- The owner lets you commit to the current branch unless a change is alarming. Don't commit `.claude/`.
