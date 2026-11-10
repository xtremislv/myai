# Branches and recent changes

A map of the branches and what each adds, so they can be pulled and merged in the right order.
Update this file whenever a new branch is cut.

## Branch map

```
main                 6ee1b2e  first commit (the original Docker-based platform)
 └─ server-native-setup   +42 commits  (runs natively on the GPU host, tw-ai.in; everything since 2026-11-02)
     └─ android-app        +1 commit   (native Android app + phone APIs)
```

Each branch starts from the tip of the one above it, so **nothing conflicts**: every merge below is a
fast-forward. `android-app` contains all of `server-native-setup`.

| Branch | Based on | What it is | Status |
|---|---|---|---|
| `main` | — | Original platform | Owner's rule: don't push to it directly |
| `server-native-setup` | `main` @ 6ee1b2e | The platform as deployed on the server (tw-ai.in) | Live on the server |
| `android-app` | `server-native-setup` @ 4a2b157 | Android app + device sign-in and push APIs | New, not deployed yet |

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

**Docs:** `docs/ANDROID_APP.md` (new), plus short additions to `README.md`, `CLAUDE.md`, `docs/USER_GUIDE.md` §6b.

**Not yet verified:** the Android project hasn't been built yet (the session that wrote it couldn't reach Google's
Maven). The first build in Android Studio may need small fixes. The server tests pass, apart from
`test_a_department_mailbox_is_managed_by_its_members_only`, which needs a Postgres host and fails on
`server-native-setup` too.

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
