#!/bin/sh
# Run the operational-store@4 acceptance pack on the pinned Portal runtime:
# operational-store@3's bound runner unchanged (which runs @2's and @1's), then
# the version 4 suite for Processing Jobs, leases, outcomes, and item
# quarantine. Exits non-zero when any part fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
tests/acceptance/run_operational_store_3.sh || status=1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_operational_store_4.py' || status=1
exit "$status"
