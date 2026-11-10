# TW-ai for Android

Native Android client (Kotlin, Jetpack Compose, Material 3) for the AI platform at https://tw-ai.in.

```bash
./gradlew assembleDebug     # app/build/outputs/apk/debug/app-debug.apk
```

Push notifications need `app/google-services.json` from your Firebase project (not committed) and the
`push/fcm` vault secret on the server. Everything else, including how sign-in and push stay private, is in
[`../docs/ANDROID_APP.md`](../docs/ANDROID_APP.md).
