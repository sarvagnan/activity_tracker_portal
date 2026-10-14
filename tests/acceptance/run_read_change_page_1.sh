#!/bin/sh
# Run the read-change-page@1 acceptance pack: the Python contract check,
# then the Kotlin change reader suite on the pinned Sender toolchain.
# Exits non-zero when either language fails, so Vouch records one outcome.
# The Gradle exit status alone is not trusted: the Kotlin suite passes only
# when a fresh JUnit report lists exactly the bound suite's tests, none
# failed, errored, or skipped.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_read_change_page_1.py' || status=1
if [ ! -d sender/app/android ]; then
  npm run prebuild --prefix sender || exit 2
fi
suite=sender/app/modules/health-connect/android/src/test/java/org/activitytracker/healthconnect/changes/ReadChangePage1Test.kt
report=sender/app/modules/health-connect/android/build/test-results/testDebugUnitTest/TEST-org.activitytracker.healthconnect.changes.ReadChangePage1Test.xml
rm -f "$report"
sender/scripts/gradle.sh :health-connect:testDebugUnitTest --tests 'org.activitytracker.healthconnect.changes.*' --rerun || status=1
uv run --frozen python - "$suite" "$report" <<'EOF' || status=1
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

suite, report = (Path(argument) for argument in sys.argv[1:])
expected = re.findall(r"@Test\s+fun\s+(\w+)\s*\(", suite.read_text(encoding="utf-8"))
if not report.is_file():
    sys.exit(f"read-change-page@1: no fresh JUnit report at {report}")
root = ET.parse(report).getroot()
cases = root.findall("testcase")
names = [case.get("name") for case in cases]
unclean = [
    case.get("name")
    for case in cases
    if any(case.find(outcome) is not None for outcome in ("failure", "error", "skipped"))
]
totals = {key: int(root.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}
problems = []
if sorted(names) != sorted(expected):
    problems.append(f"executed tests differ from the suite's @Test methods: {sorted(set(names) ^ set(expected))}")
if totals != {"tests": len(expected), "failures": 0, "errors": 0, "skipped": 0}:
    problems.append(f"report totals {totals} for {len(expected)} expected tests")
if unclean:
    problems.append(f"{len(unclean)} tests did not pass")
if problems:
    sys.exit("read-change-page@1: " + "; ".join(problems))
print(f"read-change-page@1: {len(expected)} Kotlin acceptance tests passed")
EOF
exit "$status"
