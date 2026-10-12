#!/bin/sh
# Run the operational-store@1 acceptance pack on the pinned Portal runtime.
# Exits non-zero when the runtime check or any acceptance test fails.
cd "$(dirname "$0")/../.." || exit 2
uv run --frozen portal runtime || exit 1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_operational_store_1.py'
