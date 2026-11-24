#!/bin/sh
# Run the operational-store@3 acceptance pack on the pinned Portal runtime:
# operational-store@2's bound runner unchanged (which runs @1's), then the
# version 3 suite for the Source Record index. Exits non-zero when any part
# fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
tests/acceptance/run_operational_store_2.sh || status=1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_operational_store_3.py' || status=1
exit "$status"
