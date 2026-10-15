#!/bin/sh
# Run the accept-ingest-batch@1 acceptance pack on the pinned Portal runtime.
# Exits non-zero when the runtime check or any acceptance test fails.
cd "$(dirname "$0")/../.." || exit 2
uv run --frozen portal runtime || exit 1
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_accept_ingest_batch_1.py'
