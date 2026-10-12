#!/bin/sh
set -eu
sender_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$sender_root"
mkdir -p .cache
# Pack the lockfile-installed template; never resolve a moving SDK dist-tag.
npm pack ./node_modules/expo-template-bare-minimum --ignore-scripts --silent --pack-destination .cache
cd app
export EXPO_NO_TELEMETRY=1
exec ../node_modules/.bin/expo prebuild --platform android --no-install \
  --template ../.cache/expo-template-bare-minimum-57.0.30.tgz
