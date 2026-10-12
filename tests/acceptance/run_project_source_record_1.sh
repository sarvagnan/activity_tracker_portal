#!/bin/sh
# Run the project-source-record@1 acceptance pack: Python schema and vector
# conformance, then the Kotlin projection suite on the pinned Sender toolchain.
# Exits non-zero when either language fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_project_source_record_1.py' || status=1
if [ ! -d sender/app/android ]; then
  npm run prebuild --prefix sender || exit 2
fi
sender/scripts/gradle.sh :health-connect:testDebugUnitTest --tests 'org.activitytracker.healthconnect.projection.*' --rerun || status=1
exit "$status"
