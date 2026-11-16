# Branches and recent changes

A map of the branches and what each adds, so they can be pulled and merged in the right order.
Update this file whenever a new branch is cut.

## Branch map

```
main                 6ee1b2e  first commit (the original Docker-based platform)
 └─ server-native-setup   +42 commits  (runs natively on the GPU host, tw-ai.in; everything since 2026-11-02)
     └─ android-app        +5 commits  (Android app, phone APIs, download + self-update; server-native-setup merged in)
         └─ claude-api-migration  (Claude through the Anthropic API, no-GPU server setup and migration scripts)
```

`android-app` contains all of `server-native-setup` (merged in on 2026-11-10), so folding it back is a
fast-forward as long as nothing new lands on `server-native-setup` first.

| Branch | Based on | What it is | Status |
|---|---|---|---|
| `main` | — | Original platform | Owner's rule: don't push to it directly |
| `server-native-setup` | `main` @ 6ee1b2e | The platform as deployed on the server (tw-ai.in) | Live on the server |
| `android-app` | `server-native-setup` @ 83e5e06 (merged) | Android app + device sign-in and push APIs | Live on the server since 2026-11-10 |
| `claude-api-migration` | `android-app` @ cefbdbc | Claude as the model (`LLM_PROVIDER=anthropic`), install/migrate scripts for an 8 vCPU / 16 GB no-GPU Ubuntu server | Ready, not deployed (2026-11-16) |

## `claude-api-migration` (2026-11-16): what changed

Moving off the GPU host: Claude through the Anthropic API instead of Gemma on vLLM, on an ordinary Ubuntu 24.04
server. Runbook: `docs/MIGRATION_CLAUDE.md`. With `LLM_PROVIDER` unset (= `vllm`) everything behaves as before,
so the branch can be merged before the move.

- New: `app/llm/anthropic_backend.py` (OpenAI-shaped messages/tools ⇄ Messages API, fast model by task, thinking
  settings, prompt caching), `scripts/install_server.sh`, `scripts/migrate_export.sh`, `scripts/migrate_import.sh`,
  `scripts/env_set.py`, `deploy/systemd/ai-platform-native.service`, `deploy/systemd/ai-platform-backup-native.service`,
  `docs/MIGRATION_CLAUDE.md`, `tests/test_llm_claude.py`, `tests/test_migration.py`.
- Changed: `app/llm/client.py` (dispatch to Claude; usage log records the model), `app/config.py` (`LLM_PROVIDER`,
  `ANTHROPIC_API_KEY`, `LLM_FAST_MODEL`, `LLM_FAST_TASKS`, `LLM_THINKING`, `LLM_EFFORT`, `LLM_BACKGROUND_CONCURRENCY`),
  `app/llm/governor.py` (no vLLM metrics with Claude), `app/health.py` (`/readyz` checks the API key),
  `scripts/start_native.sh` (no vLLM with Claude, `stop`, `TUNNEL_OFF`, `SCHEDULER_OFF`, `STT_DEVICE`/`STT_MODEL`
  from .env, Prometheus without the vLLM job), `scripts/watchdog.sh` + `scripts/public_urls.sh` (`TUNNEL_OFF`),
  `scripts/generate_env.py` (`--provider anthropic`), `scripts/llm_cost.py` (actual Claude spend),
  `pyproject.toml`/`Dockerfile` (`anthropic`), `.env.example`; `backup.sh`/`restore.sh` made executable.
- No migration, no Android change.

## `android-app` (2026-11-10): what changed

One commit, 9ac9e9b, "Add the native Android app and the phone APIs it needs".

**Server (needs a migration and a gateway restart when deployed):**
- New: `app/devices.py`, `app/push.py`, `app/api/routes_devices.py`, `migrations/versions/0009_device_sessions.py`
  (table `device_sessions`), `tests/test_devices.py` (18 tests).
- New endpoints: `POST /v1/auth/device/login`, `POST /v1/auth/device/refresh`, `GET|POST /v1/devices`,
  `DELETE /v1/devices/{id}`, `PUT /v1/devices/{id}/push`, `GET /v1/app/config`.
- Small hooks in existing files (nothing removed or renamed):
  - `app/db/session.py`: `DeviceSession` model.
  - `app/main.py`: registers `routes_devices`.
  - `app/reminders/service.py`: `notify()` sends a content-free push.
  - `app/request_manager/approvals.py`: pushes when an approval is created or decided.
  - `app/api/routes_chat.py`: pushes when a streamed reply or confirmation finishes (`_run_and_relay(..., bind=)`).
  - `app/api/routes_auth.py`: admin password reset signs the person's phones out.
  - `app/privacy.py`: erasing a person deletes their phones.
- Optional vault secret `push/fcm` (Firebase service-account JSON) turns push on; optional env `PUSH_PROXY`.

**Android app:** the whole `android/` folder (Kotlin, Jetpack Compose). Build with `cd android && ./gradlew assembleDebug`.
`android/app/google-services.json` (Firebase) is deliberately not committed.

**Download and self-update (2026-11-10):** `scripts/publish_android.sh` publishes a signed APK into
`ANDROID_RELEASE_DIR`; the website offers it (Settings, sign-in page) and the app updates itself
(`android/.../update/`). Signing key in `~/.config/twai-android/` (back it up). See ANDROID_APP "Publishing".

**Docs:** `docs/ANDROID_APP.md` (new), plus short additions to `README.md`, `CLAUDE.md`, `docs/USER_GUIDE.md` §6b.

**Verified 2026-11-10:** `./gradlew assembleDebug` builds as committed (JDK 17, SDK platform 35, AGP 8.7.3), and
every one of the 46 API calls in `data/Api.kt` matches a server route and method. The app has not yet been run on a
phone against tw-ai.in. `server-native-setup` (Help card, UI asset tagging) is merged in; 648 tests pass.
Deployed 2026-11-10 (live checkout on `android-app`, migration 0009); 1.0.70 published the same day.

**2026-11-13:** 1.0.70 crashed right after sign-in on Android 13+ (old `androidx.fragment` from `biometric`; see
docs/ANDROID_APP.md "Tests, crash reports and debugging"). Fixed, plus: response bodies read off the main thread,
crash reports (`POST /v1/app/crash`, needs a gateway restart), Robolectric tests, and the app restyled like the website.

## `server-native-setup` (2026-11-02 → 2026-11-10): what changed since `main`

42 commits. In short: runs natively on the vast.ai GPU host without Docker. Other changes:
- Gemma 4 with tool calling.
- Specialist agents (web search, browser, scraping, mail).
- Private chat with web access.
- Files, reminders and digests.
- Documents from templates.
- Shopping with a live browser and personal/work orders.
- Google/Microsoft mail sign-in.
- A Python sandbox and dashboards.
- Big-file summaries.
- Background research jobs.
- Sharing.
- The TW-ai redesign.
- Auto-start and watchdog.
- Served at tw-ai.in through a named Cloudflare tunnel.

`git log --oneline main..server-native-setup` lists every commit. `docs/SESSION_WRAPUP.md` and `docs/HANDOFF.md`
have the details.

## How to pull and merge

**Get the branches on another machine (or the server):**
```bash
git fetch origin
git switch android-app              # or: git switch -c android-app --track origin/android-app
```

**Deploy android-app on the server** (it's server-native-setup plus the phone APIs):
```bash
cd /root/myai
git fetch origin && git switch android-app && git pull
.venv/bin/alembic upgrade head      # 0009: device_sessions
kill $(cat /var/log/ai-platform/gateway.pid); scripts/start_native.sh
curl -s https://tw-ai.in/v1/app/config
scripts/publish_android.sh "First release"   # the website offers it from then on
```

**When the app is tested, fold it into server-native-setup** (fast-forward, no merge commit, no conflicts):
```bash
git switch server-native-setup && git pull
git merge --ff-only android-app
git push origin server-native-setup
```

**Bringing everything into main** (only if and when you decide to; main is protected by your rule):
```bash
git switch main && git pull
git merge --ff-only server-native-setup     # after android-app is in it
```

If `--ff-only` ever refuses, someone committed to the target branch in the meantime. Then use
`git merge android-app` (or rebase), and run `.venv/bin/python -m pytest tests/ -q` before pushing.

**Windows note:** a Linux tool may report files under `tests/` as modified when the only difference is CRLF vs LF
line endings. `git diff --ignore-cr-at-eol --stat` shows no real change in that case. Don't commit those.
