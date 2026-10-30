#!/bin/sh
# Run the coordinate-sender-sync@3 acceptance pack: the version 3 contract check,
# coordinate-sender-sync@2's bound runner unchanged, which runs @1's, and the TypeScript
# suite for the quiet change reads that @3 adds, on the pinned Node. Exits non-zero when any part
# fails, so Vouch records one outcome. Exit statuses alone are not trusted: the new
# suite passes only when a fresh report lists exactly its bound tests, none failed,
# errored, or skipped, and the @1 runner verifies its own report the same way. A
# per-test timeout fails a test that waits on timers. Each TypeScript part also runs
# under a supervisor with an external deadline, so even a microtask-only loop in
# either suite becomes a failure: see supervised below.
cd "$(dirname "$0")/../.." || exit 2
status=0
reports=$(mktemp -d) || exit 2
supervisor=
cleanup() {
  if [ -n "$supervisor" ]; then
    kill -TERM "$supervisor" 2>/dev/null
    wait "$supervisor"
  fi
  rm -rf "$reports"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# supervised LIMIT LABEL COMMAND...: run COMMAND in its own process group and return its
# status. If COMMAND runs past LIMIT seconds, or the supervisor is interrupted, the whole
# group gets TERM, then KILL after a 10-second grace, and the supervisor confirms the group
# is gone. That outcome, a COMMAND that leaves processes running in its group, a COMMAND
# that cannot start, and a COMMAND ended by a signal all fail with a message. A process
# that starts its own session leaves the group; neither Node's test runner nor uv does.
supervised() {
  uv run --frozen python - "$@" <<'PY' &
import os
import signal
import subprocess
import sys
import time

limit, label, command = float(sys.argv[1]), sys.argv[2], sys.argv[3:]
GRACE_SECONDS = 10
SIGNALS = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}


class Interrupted(Exception):
    pass


def interrupted(signum, _frame):
    raise Interrupted(signal.Signals(signum).name)


def fail(message):
    print(f"coordinate-sender-sync@3: {label} {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def group_remains():
    """Whether any process is left in the child's group, reaping the child once it exits."""
    child.poll()
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(reason):
    for signum in SIGNALS:
        signal.signal(signum, signal.SIG_IGN)
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
        end = time.monotonic() + GRACE_SECONDS
        while group_remains():
            if time.monotonic() >= end:
                break
            time.sleep(0.05)
        else:
            fail(reason)
    fail(f"{reason}, and processes in its group survived KILL")


for signum in SIGNALS:
    signal.signal(signum, interrupted)
# Hold signals until the child is recorded, so an interruption can never orphan it. The
# child unblocks them before it executes.
signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
try:
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True,
                             preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, set()))
except OSError as error:
    fail(f"could not start: {error}")
try:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
    code = child.wait(timeout=limit)
    if group_remains():
        stop("exited but left processes running in its group")
except subprocess.TimeoutExpired:
    stop(f"exceeded {limit:g} seconds")
except Interrupted as error:
    stop(f"was interrupted by {error}")
if code < 0:
    fail(f"was ended by signal {signal.Signals(-code).name}")
sys.exit(code)
PY
  supervisor=$!
  wait "$supervisor"
  result=$?
  supervisor=
  return "$result"
}

uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_coordinate_sender_sync_3.py' || status=1
supervised 2400 "coordinate-sender-sync@2's runner" tests/acceptance/run_coordinate_sender_sync_2.sh || status=1

ts_suite=sender/app/test/coordinate-sender-sync-3.test.ts
ts_report="$reports/coordinate-sender-sync-3.junit.xml"
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
    sys.exit(f"coordinate-sender-sync@3: TypeScript: no fresh report at {ts_report}")
try:
    cases = list(ET.parse(ts_report).getroot().iter("testcase"))
except ET.ParseError:
    sys.exit(f"coordinate-sender-sync@3: TypeScript: the report at {ts_report} is incomplete")
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
    sys.exit("coordinate-sender-sync@3: TypeScript: " + "; ".join(problems))
print(f"coordinate-sender-sync@3: {len(expected)} TypeScript acceptance tests passed")
PY
exit "$status"
