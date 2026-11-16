---
name: myai-android-release
description: "Android app: signing key in ~/.config/twai-android (never replace), publish_android.sh, owner's go-ahead needed"
metadata:
  type: project
---

The Android app (`android/`) is downloaded from the website and updates itself from what
`scripts/publish_android.sh` publishes into `/var/lib/ai-platform/android`. 1.0.81 was the last release on the GPU
server (2026-11-14, voice input). Version = commit count, so numbers jump.

- Release signing key: `/root/.config/twai-android/` (release.jks + keystore.properties, mode 600). Never regenerate
  or sign with another key: installed phones refuse the update. It moves with `migrate_export.sh --with-android-key`.
- Building needs JDK 17 + Android SDK 35 on this server (not installed by install_server.sh).
- Publishing needs the owner's go-ahead. Test Android 13+ on an emulator (a 1.0.70 crash only showed there).

**Why:** losing or changing the key forces every user to uninstall and reinstall.
**How to apply:** after changing `android/`, commit, then publish when the owner agrees (docs/ANDROID_APP.md).
