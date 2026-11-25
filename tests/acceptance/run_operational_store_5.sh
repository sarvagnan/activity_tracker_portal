#!/bin/sh
# Run the operational-store@5 acceptance pack on the pinned Portal runtime:
# operational-store@4's bound runner unchanged (which runs @3's, @2's, and
# @1's), then the version 5 suite for policy records, policy reads, known
# origins, and the reprocessing a Fallback Timezone Policy affects. Exits
# non-zero when any part fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
tests/acceptance/run_operational_store_4.sh || status=1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_operational_store_5.py' || status=1
exit "$status"
