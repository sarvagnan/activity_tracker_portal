#!/bin/sh
# Run the operational-store@2 acceptance pack on the pinned Portal runtime:
# operational-store@1's bound runner unchanged, then the version 2 suite for the
# delivery-cycle read. Exits non-zero when any part fails, so Vouch records one
# outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
tests/acceptance/run_operational_store_1.sh || status=1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_operational_store_2.py' || status=1
exit "$status"
