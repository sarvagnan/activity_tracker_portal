#!/bin/sh
# Run the read-change-page@1 acceptance pack: the Python contract check,
# then the Kotlin change reader suite on the pinned Sender toolchain.
# Exits non-zero when either language fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_read_change_page_1.py' || status=1
if [ ! -d sender/app/android ]; then
  npm run prebuild --prefix sender || exit 2
fi
sender/scripts/gradle.sh :health-connect:testDebugUnitTest --tests 'org.activitytracker.healthconnect.changes.*' --rerun || status=1
exit "$status"
