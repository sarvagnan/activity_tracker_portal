#!/bin/sh
# Run the begin-reconciliation-snapshot@1 acceptance pack: the Python contract check and the
# TypeScript reconciliation suite on the pinned Node. Exits non-zero when either part
# fails, so Vouch records one outcome. Exit statuses alone are not trusted: the
# TypeScript suite passes only when a fresh report lists exactly its bound
# tests, none failed, errored, or skipped.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_begin_reconciliation_snapshot_1.py' || status=1

reports=$(mktemp -d) || exit 2
trap 'rm -rf "$reports"' EXIT HUP INT TERM
ts_suite=sender/app/test/begin-reconciliation-snapshot-1.test.ts
ts_report="$reports/begin-reconciliation-snapshot-1.junit.xml"
node --test --test-reporter=spec --test-reporter-destination=stdout \
  --test-reporter=junit --test-reporter-destination="$ts_report" "$ts_suite" || status=1

uv run --frozen python - "$ts_suite" "$ts_report" <<'PY' || status=1
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ts_suite, ts_report = (Path(argument) for argument in sys.argv[1:])
expected = re.findall(r'^test\("([^"]+)"', ts_suite.read_text(encoding="utf-8"), re.MULTILINE)
if not ts_report.is_file():
    sys.exit(f"begin-reconciliation-snapshot@1: TypeScript: no fresh report at {ts_report}")
cases = list(ET.parse(ts_report).getroot().iter("testcase"))
names = [case.get("name") for case in cases]
problems = []
if sorted(names) != sorted(expected):
    problems.append(f"executed tests differ from the suite: {sorted(set(names) ^ set(expected))}")
unclean = [
    case.get("name")
    for case in cases
    if any(case.find(outcome) is not None for outcome in ("failure", "error", "skipped"))
]
if unclean:
    problems.append(f"{len(unclean)} tests did not pass")
if problems:
    sys.exit("begin-reconciliation-snapshot@1: TypeScript: " + "; ".join(problems))
print(f"begin-reconciliation-snapshot@1: {len(expected)} TypeScript acceptance tests passed")
PY
exit "$status"
