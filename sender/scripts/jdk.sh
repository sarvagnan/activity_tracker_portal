#!/bin/sh
# Provision an exact, checksum-verified JDK under the Sender workspace only.
set -eu
sender_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)
    archive=OpenJDK17U-jdk_aarch64_mac_hotspot_17.0.20.1_1.tar.gz
    checksum=196d13ba5f10414bef7f6a05a9b3f00edacb18ebacef2b99485db9e2ee18f0e8
    java_home="$sender_root/.cache/jdk-17.0.20.1+1/Contents/Home"
    ;;
  Linux-x86_64)
    archive=OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz
    checksum=3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e
    java_home="$sender_root/.cache/jdk-17.0.20.1+1"
    ;;
  *) echo "Unsupported host: need macOS arm64 or Linux x86_64" >&2; exit 1 ;;
esac
if [ ! -x "$java_home/bin/java" ]; then
  mkdir -p "$sender_root/.cache"
  temporary=$(mktemp -d "$sender_root/.cache/jdk-download.XXXXXX")
  trap 'rm -rf "$temporary"' EXIT HUP INT TERM
  curl --fail --location --show-error \
    "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/$archive" \
    --output "$temporary/$archive"
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$temporary/$archive" | cut -d ' ' -f 1)
  else
    actual=$(shasum -a 256 "$temporary/$archive" | cut -d ' ' -f 1)
  fi
  [ "$actual" = "$checksum" ] || { echo "JDK checksum mismatch" >&2; exit 1; }
  mkdir "$temporary/unpacked"
  tar -xzf "$temporary/$archive" -C "$temporary/unpacked" --strip-components=1
  mv -f "$temporary/unpacked" "$sender_root/.cache/jdk-17.0.20.1+1"
fi
printf '%s\n' "$java_home"
