#!/usr/bin/env bash
# Build the Android app from this checkout, sign it and publish it on this server.
#
#   scripts/publish_android.sh ["What's new" text] [--required]
#
# The website then offers it (Settings > TW-ai for Android, and the sign-in page), and installed
# apps update themselves to it (android/.../update/Updater.kt). --required makes older apps stop
# until they update (use it when the server API changes in a way old apps can't handle).
#
# The version code is the number of commits in the history, so every publish from a newer commit
# is newer. Every build must be signed with the same key, or phones refuse the update: keep
# ~/.config/twai-android/ (release.jks + keystore.properties) backed up.
#
# Needs JDK 17 + the Android SDK: TWAI_ANDROID_TOOLCHAIN (default /root/.twai-android-toolchain,
# with jdk/, sdk/ and gradle/ inside). No gateway restart is needed: /v1/app/config reads the
# release from disk on each call.
set -euo pipefail
trap 'echo "publish_android: failed at line $LINENO" >&2' ERR

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLCHAIN="${TWAI_ANDROID_TOOLCHAIN:-/root/.twai-android-toolchain}"
export TWAI_KEYSTORE_PROPERTIES="${TWAI_KEYSTORE_PROPERTIES:-/root/.config/twai-android/keystore.properties}"
OUT="${ANDROID_RELEASE_DIR:-$(cd "$ROOT" && { grep -s '^ANDROID_RELEASE_DIR=' .env || true; } | cut -d= -f2-)}"
OUT="${OUT:-/var/lib/ai-platform/android}"

NOTES=""
REQUIRED=0
for arg in "$@"; do
  case "$arg" in
    --required) REQUIRED=1 ;;
    *) NOTES="$arg" ;;
  esac
done

die() { echo "publish_android: $*" >&2; exit 1; }

[ -x "$TOOLCHAIN/jdk/bin/java" ] || die "no JDK in $TOOLCHAIN/jdk (see docs/ANDROID_APP.md, 'Publishing')"
[ -d "$TOOLCHAIN/sdk/platforms" ] || die "no Android SDK in $TOOLCHAIN/sdk"
[ -f "$TWAI_KEYSTORE_PROPERTIES" ] || die "no signing key: $TWAI_KEYSTORE_PROPERTIES is missing"
cd "$ROOT"
if [ -n "$(git status --porcelain -- android)" ]; then
  die "android/ has uncommitted changes: commit them first, so the version matches a commit"
fi

CODE="$(git rev-list --count HEAD)"
NAME="1.0.$CODE"
PREVIOUS="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["version_code"], m.get("min_version_code", 0))' "$OUT/release.json" 2>/dev/null || echo "0 0")"
PREV_CODE="${PREVIOUS% *}"
PREV_MIN="${PREVIOUS#* }"
[ "$CODE" -gt "$PREV_CODE" ] || die "version $CODE is already published (last: $PREV_CODE); commit something new first"
[ -n "$NOTES" ] || NOTES="$(git log -1 --format=%s -- android)"
MIN="$PREV_MIN"
[ "$REQUIRED" = 1 ] && MIN="$CODE"

export JAVA_HOME="$TOOLCHAIN/jdk" ANDROID_HOME="$TOOLCHAIN/sdk" GRADLE_USER_HOME="$TOOLCHAIN/gradle"
export PATH="$JAVA_HOME/bin:$PATH"
echo "sdk.dir=$ANDROID_HOME" > android/local.properties
echo "Building TW-ai $NAME ($CODE)..."
(cd android && bash gradlew --no-daemon -q assembleRelease -PversionCode="$CODE" -PversionName="$NAME")
APK="android/app/build/outputs/apk/release/app-release.apk"
[ -f "$APK" ] || die "no signed APK was built (is keystore.properties right?)"
APKSIGNER="$(ls -d "$ANDROID_HOME"/build-tools/*/apksigner | sort -V | tail -1)"
"$APKSIGNER" verify "$APK" || die "the APK's signature doesn't verify"

mkdir -p "$OUT"
cp "$APK" "$OUT/twai.apk.tmp"
python3 - "$OUT" "$CODE" "$NAME" "$NOTES" "$MIN" <<'PY'
import datetime, hashlib, json, os, sys
out, code, name, notes, minimum = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5])
data = open(os.path.join(out, "twai.apk.tmp"), "rb").read()
meta = {"version_code": code, "version_name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "notes": notes, "min_version_code": minimum,
        "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
with open(os.path.join(out, "release.json.tmp"), "w") as f:
    json.dump(meta, f, indent=2)
PY
# The APK first, then the description: in between, the two disagree and nothing is offered.
mv "$OUT/twai.apk.tmp" "$OUT/twai.apk"
mv "$OUT/release.json.tmp" "$OUT/release.json"
rm -rf android/app/build android/local.properties   # ~300 MB; the Gradle cache keeps rebuilds quick

echo "Published TW-ai $NAME ($(du -h "$OUT/twai.apk" | cut -f1)) in $OUT"
[ "$MIN" = "$CODE" ] && echo "Older apps must update before they go on (--required)."
echo "Check: curl -s localhost:8000/v1/app/config"
