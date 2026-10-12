#!/bin/sh
set -eu
sender_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
JAVA_HOME=$("$sender_root/scripts/jdk.sh")
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"
export GRADLE_USER_HOME="$sender_root/.cache/gradle"
export ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
cd "$sender_root/app/android"
exec ./gradlew --no-daemon --console=plain \
  -Porg.gradle.java.installations.auto-detect=false \
  -Porg.gradle.java.installations.auto-download=false \
  "-Porg.gradle.java.installations.paths=$JAVA_HOME" \
  -Pandroid.builder.sdkDownload=true "$@"
