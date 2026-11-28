#!/bin/sh
# Run the range-summary-contract@1 acceptance pack on the pinned Portal runtime.
# Exits non-zero when any test fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_range_summary_contract_1.py'
