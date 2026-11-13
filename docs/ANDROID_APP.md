# TW-ai for Android

A native Android app (Kotlin, Jetpack Compose, Material 3) for the platform at https://tw-ai.in. Source: `android/`.
It talks to the same API as the web app, plus a few endpoints added for phones (below).

## What it does

| Area | In the app |
|---|---|
| Chat | Streaming replies (`/v1/chat/stream`) with live progress steps, Markdown (headings, lists, tables, code, links), dashboards (KPIs, bar/line charts, tables, share as CSV), "Handled by <department>" for work, work-request progress, confirm cards (send mail / delete / **"Personal: place it" / "Work: send to Accounts"**), secure box for OTP/CVV, the live browser (tap to click, type, keys, scroll), attachments (upload, open, delete), answer mode (automatic / quick / specialists / team), voice input, copy/share a reply. A reply keeps going on the server if you leave the app; you get a notification when it's ready. |
| Notifications | Reminders, mail digests, finished jobs, shares, alerts and "reply ready" as Android notifications in their own channels. **Reply** straight from the notification shade, **Mark read**, **Snooze 10 min** on reminders. Approvers get "Approval needed" (approving happens in the app, behind the lock). |
| Alerts | The bell (mark read / all read), "Digest now", reminders: add (free text like "tomorrow 9am" or the native date/time picker, repeat, email copy), cancel. |
| Files | My files with storage use, upload into a folder, open/share (through a FileProvider, read-only), delete, trash and restore. |
| Work | Approvals (admin; approve asks for fingerprint/face/screen lock), the Accounts inbox (account members), reports (hr, admin, account). Tabs show only for people who have them; the server checks every call anyway. |
| Settings | Account and departments, new recovery code, notification settings, email and morning digest, **app lock** (biometric/screen lock, re-locks after 30 s away), theme, signed-in phones (sign others out), open the web app (Custom Tab) for mailbox sign-in, saved details and admin pages, sign out. |
| Android | Share text or a document from any app into a new chat, launcher shortcuts (New chat, Reminders), `twai://` deep links, splash screen, themed/monochrome icon, edge-to-edge, dark theme, sign-in excluded from backups. |

Not in the app yet (use the web app): connecting a mailbox, saved details (wallet), company templates, the admin's
users/policies/secrets pages, department shared files.

## How signing in works on a phone

The web app keeps its session in an HttpOnly cookie. A phone app is closed most of the time but must still wake up
for reminders, so it signs in as a **device** and keeps a **refresh token** (`app/devices.py`):

- `POST /v1/auth/device/login {username, password, name, push_token?}` returns an access token (the usual JWT,
  `JWT_TTL_SECONDS`) and `refresh_token` + `device_id`.
- The refresh token is `<rotating part>.<device secret>`. The server stores only the SHA-256 of the rotating part,
  replaces it on every `POST /v1/auth/device/refresh {device_id, refresh_token}`, and accepts the previous one once
  (a phone that lost the answer on a bad connection isn't signed out).
- The person's chat key is stored on the device row **wrapped by a key derived from the device secret**, which the
  server never stores. So, as with the password, the database, backups and the vault master key alone can't open
  private chats from a device row.
- On the phone the refresh token is encrypted with an AES key held in the Android Keystore and excluded from backups.
- First sign-in with the admin's password: the app makes the person choose their own password, shows the recovery
  code once, then calls `POST /v1/devices {device_id}` with the new token so the phone carries the new chat key.
- Signing out (`DELETE /v1/devices/{id}`), the admin resetting the password, disabling or erasing the account all stop
  the refresh token. Up to 10 phones per person; the oldest unused one is signed out to make room.

## Push notifications (Firebase Cloud Messaging)

Pushes carry **no content**: only what kind of thing happened (`notification`, `chat_done`, `chat_failed`,
`approval`, `approval_decided`), plus ids. The phone wakes up and fetches the text from this server itself, so no
reminder, mail or chat text goes through Google (`app/push.py`).

Where pushes are sent from:
- every in-app notification (`reminders.service.notify`: due reminders, digests, jobs, shares, alerts);
- a streamed chat reply or confirmation finishing (`routes_chat._run_and_relay`), so you hear back if you left the app;
- an approval being created (to admins) or decided (to the requester).

**Set up (once):**
1. Create a Firebase project (console.firebase.google.com), add an Android app with package `com.techwiser.twai`,
   download its `google-services.json` into `android/app/`. (It isn't committed: see `android/.gitignore`.)
2. Firebase console > Project settings > Service accounts > *Generate new private key*. Store the JSON in the vault:
   `PUT /v1/admin/secrets {"name": "push/fcm", "value": "<the whole JSON>"}` (or Admin > Credentials in the web app).
3. `GET /v1/app/config` now says `"push_enabled": true`. If this host can't reach `fcm.googleapis.com` /
   `oauth2.googleapis.com`, set `PUSH_PROXY` (e.g. the Mumbai VPS) in the gateway's environment.

Without Firebase the app still works: it checks for news every 15 minutes (WorkManager) and follows a running reply
in the background for up to 9 minutes.

## Build and install

Requirements: Android Studio Ladybug (2024.2) or newer, or JDK 17 + the Android SDK (platform 35).

```bash
cd android
./gradlew assembleDebug            # app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug             # onto a connected phone (USB debugging on)
```

Release (sideload or Play Store): create a signing key once and an `android/keystore.properties` (not committed):

```bash
keytool -genkeypair -v -keystore ~/twai-release.jks -keyalg RSA -keysize 4096 -validity 10000 -alias twai
cat > android/keystore.properties <<EOF
storeFile=/home/you/twai-release.jks
storePassword=...
keyAlias=twai
keyPassword=...
EOF
./gradlew assembleRelease          # or bundleRelease for Play (app/build/outputs/bundle/release/app-release.aab)
```

The server address defaults to `https://tw-ai.in` (`DEFAULT_SERVER` in `app/build.gradle.kts`) and can be changed on
the sign-in screen ("Server: ..."). Plain HTTP is allowed only for `10.0.2.2` and `localhost` (emulator testing).

## Tests, crash reports and debugging

```bash
cd android && ./gradlew testDebugUnitTest   # Robolectric: starts the app, signs in against a fake server (Android 13-15,
                                            # light/dark), and guards the 1.0.70 crash (PermissionCrashTest)
```

- **Crash reports:** `CrashReporter` saves an uncaught crash's stack trace (exception and frames only, no chat, mail or
  file text) and sends it on the next start to `POST /v1/app/crash` (no sign-in, at most 30 a minute). It lands in
  the gateway log: `grep -A40 "ANDROID CRASH" /var/log/ai-platform/gateway.log`.
- **Errors shown in the app** are logged with their cause (`friendly()` in `ui/Common.kt`, tag `TwApp`): with a
  phone on USB, `adb logcat -s TwApp AndroidRuntime`.
- **Emulator on this server** (no KVM, so slow: ~8 min to boot Android 13; removed after use to save disk:
  `sdkmanager "emulator" "system-images;android-33;default;x86_64"`): system image and AVD in `/dev/shm`
  (the root disk is too full; point the SDK's `.temp` there too), `emulator -avd ... -no-window -accel off`. Reach a
  private copy of the gateway with `adb reverse tcp:8765 tcp:8765` and server `http://localhost:8765` (10.0.2.2 did
  not connect). If "System UI isn't responding" sticks, choose "Close app".
- **1.0.70 crash (fixed in 1.0.71, published 2026-11-13):** right after sign-in on Android 13+, asking for notification permission threw
  "Can only use lower 16 bits for requestCode": `biometric` 1.1.0 pulled in `fragment` 1.2.5, whose
  `FragmentActivity` rejects the request codes `activity` 1.9 uses. `fragment` 1.8.5 is now a direct dependency.
  Also fixed: response bodies were read on the main thread (`Api.exchange` now reads on the I/O dispatcher).

## Publishing and auto-update

The server hands out the app itself: the website offers it (**Settings > TW-ai for Android**, and a link on the
sign-in page), and installed apps keep themselves at the published version. To ship a change:

```bash
git commit ...                                   # android/ must be committed
scripts/publish_android.sh "What's new text"     # build, sign, publish (no restart needed)
scripts/publish_android.sh "..." --required      # older apps must update before they go on
```

- **Version:** the version code is the number of commits (`git rev-list --count HEAD`), the name `1.0.<code>`.
  Publishing the same commit twice is refused.
- **Where:** `ANDROID_RELEASE_DIR` (default `/var/lib/ai-platform/android`): `twai.apk` + `release.json`
  (version, SHA-256, size, notes, `min_version_code`). `app/app_release.py` reads it on each call.
- **Endpoints (no sign-in; the APK holds no secrets):** `/v1/app/config` has `android: {version_code, url, sha256, ...}`,
  `/v1/app/android/<code>.apk` serves it (only the current code, so a cached old file never passes as new),
  `/v1/app/android/latest.apk` redirects to it.
- **In the app (`update/`):** checks on every start/resume and every 6 hours, signed in or not. A newer version is
  downloaded, checked against the SHA-256 and installed with PackageInstaller. In the app: an "Update available"
  dialog ("Update needed", without "Later", below `min_version_code`); Settings > Check for updates. In the
  background: installed silently where Android allows it (Android 12+, after the app has installed itself once,
  while it isn't open), otherwise an "update ready" notification. Android asks once to allow TW-ai to install apps.
- **Signing key:** every published APK must be signed with the same key, or phones refuse the update and people
  must uninstall and reinstall. The key is `~/.config/twai-android/release.jks` with its passwords in
  `keystore.properties` next to it (mode 600, never committed). **Back up that folder somewhere safe.**
- **Toolchain:** JDK 17 + Android SDK in `TWAI_ANDROID_TOOLCHAIN` (default `/root/.twai-android-toolchain`: `jdk/`,
  `sdk/`, `gradle/`; about 2.3 GB). To set it up again: Temurin 17 into `jdk/`, Android command-line tools into
  `sdk/cmdline-tools/latest/`, accept licences with `sdkmanager --licenses`; Gradle fetches the rest.
- **What auto-update covers:** changes to the app's own screens (`android/`) reach phones only when published.
  Everything the server does (agents, answers, rules, data) changes for the app at once, since it calls the same
  API. A server change that old apps can't handle: publish with `--required`.

## Deploying the server side

The phone endpoints ship with the gateway. On the server:

```bash
git pull                                   # branch with this change
.venv/bin/alembic upgrade head             # migration 0009: device_sessions
kill $(cat /var/log/ai-platform/gateway.pid); scripts/start_native.sh
curl -s https://tw-ai.in/v1/app/config     # {"api_version":1,"push_enabled":...}
```

## Code map (`android/app/src/main/java/com/techwiser/twai/`)

| File | What |
|---|---|
| `TwApp.kt` | Application: single instances, notification channels, schedules sync when signed in |
| `data/Api.kt` | Every API call, token refresh (mutex), SSE streaming, uploads/downloads |
| `data/SecureStore.kt` | Keystore-encrypted storage for the device sign-in |
| `data/Models.kt` | JSON models (mirror `app/api/*.py`) |
| `notify/` | `Push` (FCM), `Sync` + `SyncWorker` (fetch news, post notifications), `FollowRunWorker`, `Notifier`, actions + `ReplyWorker` |
| `ui/MainActivity.kt` | Splash, app lock, deep links/share intents, bottom navigation |
| `ui/chat/` | Chat list, chat screen + ViewModel, Markdown renderer, dashboards |
| `ui/alerts`, `ui/files`, `ui/work`, `ui/settings`, `ui/login` | The other screens |
