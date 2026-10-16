#!/bin/sh
# Run the receipt-bound-delivery@1 acceptance pack: the Python contract check and end-to-end evidence,
# which drives the Sender as Node processes, and the TypeScript transport suite on the pinned Node.
# Exits non-zero when either part fails, so Vouch records one outcome. Exit statuses alone are not
# trusted: the TypeScript suite passes only when a fresh report lists exactly its bound tests, none
# failed, errored, or skipped. A per-test timeout fails a test that waits on timers, and external
# deadlines end each part and its child processes, so even a microtask-only loop becomes a failure.
cd "$(dirname "$0")/../.." || exit 2
status=0

uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_receipt_bound_delivery_1.py' &
python_pid=$!
( sleep 1200; echo "receipt-bound-delivery@1: Python suite exceeded 1200 seconds" >&2
  pkill -TERM -P "$python_pid"; kill -TERM "$python_pid" ) </dev/null >/dev/null 2>&1 &
python_watchdog=$!
wait "$python_pid" || status=1
pkill -P "$python_watchdog" 2>/dev/null
kill "$python_watchdog" 2>/dev/null
wait "$python_watchdog" 2>/dev/null

reports=$(mktemp -d) || exit 2
trap 'rm -rf "$reports"' EXIT HUP INT TERM
ts_suite=sender/app/test/receipt-bound-delivery-1.test.ts
ts_report="$reports/receipt-bound-delivery-1.junit.xml"
node --test --test-timeout=60000 --test-reporter=spec --test-reporter-destination=stdout \
  --test-reporter=junit --test-reporter-destination="$ts_report" "$ts_suite" &
node_pid=$!
( sleep 900; echo "receipt-bound-delivery@1: TypeScript suite exceeded 900 seconds" >&2
  pkill -TERM -P "$node_pid"; kill -TERM "$node_pid" ) </dev/null >/dev/null 2>&1 &
watchdog=$!
wait "$node_pid" || status=1
pkill -P "$watchdog" 2>/dev/null
kill "$watchdog" 2>/dev/null
wait "$watchdog" 2>/dev/null

uv run --frozen python - "$ts_suite" "$ts_report" <<'PY' || status=1
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ts_suite, ts_report = (Path(argument) for argument in sys.argv[1:])
expected = re.findall(r'^test\("([^"]+)"', ts_suite.read_text(encoding="utf-8"), re.MULTILINE)
if not ts_report.is_file():
    sys.exit(f"receipt-bound-delivery@1: TypeScript: no fresh report at {ts_report}")
try:
    cases = list(ET.parse(ts_report).getroot().iter("testcase"))
except ET.ParseError:
    sys.exit(f"receipt-bound-delivery@1: TypeScript: the report at {ts_report} is incomplete")
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
    sys.exit("receipt-bound-delivery@1: TypeScript: " + "; ".join(problems))
print(f"receipt-bound-delivery@1: {len(expected)} TypeScript acceptance tests passed")
PY
exit "$status"
