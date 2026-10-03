#!/usr/bin/env bash
# Builds e2e-probe.apk with the bare SDK tools (no Gradle). The APK is
# committed; rebuild only when the source changes.
#   ANDROID_HOME=~/Library/Android/sdk ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
SDK=${ANDROID_HOME:?set ANDROID_HOME}
BT=$SDK/build-tools/${BUILD_TOOLS:-36.0.0}
JAR=$SDK/platforms/${PLATFORM:-android-35}/android.jar
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
"$BT/aapt2" link -o "$OUT/base.apk" --manifest AndroidManifest.xml -I "$JAR" --min-sdk-version 30 --target-sdk-version 35
mkdir -p "$OUT/classes" "$OUT/dex"
javac --release 11 -cp "$JAR" -d "$OUT/classes" $(find src -name '*.java')
"$BT/d8" --release --min-api 30 --lib "$JAR" --output "$OUT/dex" $(find "$OUT/classes" -name '*.class')
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT/dex" && zip -q "$OUT/unsigned.apk" classes.dex)
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
# A throwaway key: this APK is only ever installed on disposable emulators.
keytool -genkeypair -keystore "$OUT/k.jks" -storepass e2e-pass -keypass e2e-pass -alias e2e \
  -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=emulator-hub e2e" >/dev/null 2>&1
"$BT/apksigner" sign --ks "$OUT/k.jks" --ks-pass pass:e2e-pass --out e2e-probe.apk "$OUT/aligned.apk"
echo "built $(pwd)/e2e-probe.apk"
