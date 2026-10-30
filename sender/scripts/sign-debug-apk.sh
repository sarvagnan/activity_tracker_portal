#!/bin/sh
# Local development only. Password "android" is non-secret; never commit this
# key. Keep it across builds so Android accepts subsequent updates.
# Usage: npm run sign:android --prefix sender -- [unsigned.apk [signed.apk]]
# ACTIVITY_TRACKER_DEBUG_KEYSTORE overrides ~/.config/activity-tracker/android-debug.keystore.
# ACTIVITY_TRACKER_SIGNING_JAVA_HOME selects a preprovisioned JDK; otherwise use
# gradle.sh's checksum-pinned JDK. ANDROID_BUILD_TOOLS_VERSION pins signing tools;
# otherwise prefer generated android.buildToolsVersion, falling back to the
# highest installed stable numeric major.minor.patch version.
set -eu
sender_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
repo_root=$(CDPATH= cd -- "$sender_root/.." && pwd -P)
fail() { printf '%s\n' "sign:android: $*" >&2; exit 1; }
[ "$#" -le 2 ] || fail 'usage: sign-debug-apk.sh [unsigned.apk [signed.apk]]'
input=${1:-$sender_root/app/android/app/build/outputs/apk/debug/app-debug-unsigned.apk}
output=${2:-$sender_root/app/android/app/build/outputs/apk/debug/app-debug.apk}
[ -f "$input" ] || fail 'unsigned APK missing; run npm run build:android --prefix sender first'

# Resolve symlinks and missing parent components before creating anything.
canonical_path() (
  target=$1
  if [ -d "$target" ]; then
    CDPATH= cd -P -- "$target" && pwd -P
  elif [ -L "$target" ]; then
    link=$(readlink "$target")
    case "$link" in
      /*) canonical_path "$link" ;;
      *) canonical_path "$(dirname -- "$target")/$link" ;;
    esac
  else
    parent=$(canonical_path "$(dirname -- "$target")") || exit 1
    case "$(basename -- "$target")" in
      .) printf '%s\n' "$parent" ;;
      ..) dirname -- "$parent" ;;
      *) printf '%s/%s\n' "${parent%/}" "$(basename -- "$target")" ;;
    esac
  fi
)
keystore=$(canonical_path "${ACTIVITY_TRACKER_DEBUG_KEYSTORE:-$HOME/.config/activity-tracker/android-debug.keystore}")
case "$keystore" in
  "$repo_root"|"$repo_root"/*) fail 'development keystore must be outside the repository checkout' ;;
esac
[ "$(canonical_path "$input")" != "$(canonical_path "$output")" ] || fail 'input and output APK paths must differ'

ANDROID_HOME=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}
export ANDROID_HOME
[ -d "$ANDROID_HOME" ] || fail 'Android SDK missing; set ANDROID_HOME or ANDROID_SDK_ROOT'
version=${ANDROID_BUILD_TOOLS_VERSION:-}
gradle_properties=$sender_root/app/android/gradle.properties
if [ -z "$version" ] && [ -f "$gradle_properties" ]; then
  version=$(sed -n 's/^android\.buildToolsVersion=\([0-9][0-9.]*\)[[:space:]]*$/\1/p' "$gradle_properties" | tail -n 1)
fi
if [ -z "$version" ]; then
  version=$(
    for directory in "$ANDROID_HOME"/build-tools/*; do
      [ -d "$directory" ] || continue
      basename -- "$directory"
    done | LC_ALL=C grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | LC_ALL=C sort -t . -k1,1n -k2,2n -k3,3n | tail -n 1
  )
fi
case "$version" in ''|*/*|.|..) fail 'apksigner missing; install Android SDK build-tools' ;; esac
apksigner=$ANDROID_HOME/build-tools/$version/apksigner
[ -x "$apksigner" ] || fail "apksigner missing in Android SDK build-tools $version"
JAVA_HOME=${ACTIVITY_TRACKER_SIGNING_JAVA_HOME:-$("$sender_root/scripts/jdk.sh")}
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"
[ -x "$JAVA_HOME/bin/keytool" ] || fail 'keytool missing from signing JDK'

umask 077
if [ ! -e "$keystore" ]; then
  mkdir -p -- "$(dirname -- "$keystore")"
  "$JAVA_HOME/bin/keytool" -genkeypair -noprompt -keystore "$keystore" \
    -storetype PKCS12 -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname 'CN=Activity Tracker Local Development' || fail 'development keystore creation failed'
fi
mkdir -p -- "$(dirname -- "$output")"
printf '%s\n' "Signing with Android SDK build-tools $version"
"$apksigner" sign --ks "$keystore" --ks-key-alias androiddebugkey \
  --ks-pass pass:android --key-pass pass:android --out "$output" "$input" \
  || fail 'APK signing failed'
"$apksigner" verify "$output" || fail 'signed APK verification failed'
printf '%s\n' "Verified signed development APK: $output"
