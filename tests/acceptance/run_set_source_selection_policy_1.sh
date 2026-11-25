#!/bin/sh
# Run the set-source-selection-policy@1 acceptance pack on the pinned Portal
# runtime. operational-store@5's suites run under their own bound runner.
# Exits non-zero when any test fails, so Vouch records one outcome.
cd "$(dirname "$0")/../.." || exit 2
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_set_source_selection_policy_1.py'
