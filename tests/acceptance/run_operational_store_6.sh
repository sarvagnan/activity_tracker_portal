#!/bin/sh
# Run the operational-store@6 acceptance pack on the pinned Portal runtime:
# operational-store@5's bound runner unchanged (which runs @4's, @3's, @2's,
# and @1's), then the version 6 suite for Processor Generations, Summary
# Refresh Jobs, candidate summaries, policy summary work, the evidence-change
# bookmark, and summary evidence reads. Exits non-zero when any part fails, so
# Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
tests/acceptance/run_operational_store_5.sh || status=1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_operational_store_6.py' || status=1
exit "$status"
