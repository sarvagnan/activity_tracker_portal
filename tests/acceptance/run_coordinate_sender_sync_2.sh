#!/bin/sh
# Run the coordinate-sender-sync@2 acceptance pack: the version 2 contract check,
# coordinate-sender-sync@1's bound runner unchanged, and the TypeScript suite for the
# absolute deadlines that @2 adds, on the pinned Node. Exits non-zero when any part
# fails, so Vouch records one outcome. Exit statuses alone are not trusted: the new
# suite passes only when a fresh report lists exactly its bound tests, none failed,
# errored, or skipped, and the @1 runner verifies its own report the same way. A
# per-test timeout fails a test that waits on timers, and an external deadline ends
# each TypeScript part with every process it started, so even a microtask-only loop
# in either suite becomes a failure.
cd "$(dirname "$0")/../.." || exit 2
status=0
reports=$(mktemp -d) || exit 2
trap 'rm -rf "$reports"' EXIT HUP INT TERM

kill_tree() {
  for child in $(pgrep -P "$1"); do kill_tree "$child"; done
  kill -TERM "$1" 2>/dev/null
}

# supervised LIMIT LABEL COMMAND...: run COMMAND, ending it and its descendants after LIMIT seconds.
supervised() {
  limit=$1 label=$2
  shift 2
  "$@" &
  pid=$!
  ( sleep "$limit"; : >"$reports/expired"; kill_tree "$pid" ) </dev/null >/dev/null 2>&1 &
  watchdog=$!
  wait "$pid"
  result=$?
  kill_tree "$watchdog"
  if [ -e "$reports/expired" ]; then
    echo "coordinate-sender-sync@2: $label exceeded $limit seconds" >&2
    rm -f "$reports/expired"
    result=1
  fi
  return "$result"
}

uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_coordinate_sender_sync_2.py' || status=1
supervised 1200 "coordinate-sender-sync@1's runner" tests/acceptance/run_coordinate_sender_sync_1.sh || status=1

ts_suite=sender/app/test/coordinate-sender-sync-2.test.ts
ts_report="$reports/coordinate-sender-sync-2.junit.xml"
supervised 900 "the TypeScript suite" node --test --test-timeout=60000 --test-reporter=spec --test-reporter-destination=stdout \
  --test-reporter=junit --test-reporter-destination="$ts_report" "$ts_suite" || status=1

uv run --frozen python - "$ts_suite" "$ts_report" <<'PY' || status=1
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ts_suite, ts_report = (Path(argument) for argument in sys.argv[1:])
expected = re.findall(r'^test\("([^"]+)"', ts_suite.read_text(encoding="utf-8"), re.MULTILINE)
if not ts_report.is_file():
    sys.exit(f"coordinate-sender-sync@2: TypeScript: no fresh report at {ts_report}")
try:
    cases = list(ET.parse(ts_report).getroot().iter("testcase"))
except ET.ParseError:
    sys.exit(f"coordinate-sender-sync@2: TypeScript: the report at {ts_report} is incomplete")
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
    sys.exit("coordinate-sender-sync@2: TypeScript: " + "; ".join(problems))
print(f"coordinate-sender-sync@2: {len(expected)} TypeScript acceptance tests passed")
PY
exit "$status"
