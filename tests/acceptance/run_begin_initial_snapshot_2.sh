#!/bin/sh
# Run the begin-initial-snapshot@2 acceptance pack: the version 2 contract check,
# begin-initial-snapshot@1's bound runner unchanged, and the TypeScript suite for the
# primitive that @2 adds. Exits non-zero when any part fails, so Vouch records one
# outcome. Exit statuses alone are not trusted: the new suite passes only when a
# fresh report lists exactly its bound tests, none failed, errored, or skipped,
# and the @1 runner verifies its own reports the same way.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_begin_initial_snapshot_2.py' || status=1
tests/acceptance/run_begin_initial_snapshot_1.sh || status=1

reports=$(mktemp -d) || exit 2
trap 'rm -rf "$reports"' EXIT HUP INT TERM
ts_suite=sender/app/test/begin-initial-snapshot-2.test.ts
ts_report="$reports/begin-initial-snapshot-2.junit.xml"
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
    sys.exit(f"begin-initial-snapshot@2: TypeScript: no fresh report at {ts_report}")
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
    sys.exit("begin-initial-snapshot@2: TypeScript: " + "; ".join(problems))
print(f"begin-initial-snapshot@2: {len(expected)} TypeScript acceptance tests passed")
PY
exit "$status"
