#!/bin/sh
# Run the ingest-protocol@1 acceptance pack in both languages that implement it.
# Exits non-zero when either language fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_ingest_protocol_1.py' || status=1
node --test sender/protocol/test/ingest-protocol-1.test.ts || status=1
exit "$status"
