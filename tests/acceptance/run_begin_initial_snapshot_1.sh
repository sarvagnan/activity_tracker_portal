#!/bin/sh
# Run the begin-initial-snapshot@1 acceptance pack: the Python contract check,
# the TypeScript durable-begin suite on the pinned Node, and the Kotlin token
# issuance suite on the pinned Sender toolchain. Exits non-zero when any part
# fails, so Vouch records one outcome. Exit statuses alone are not trusted:
# each suite passes only when a fresh report lists exactly its bound tests,
# none failed, errored, or skipped.
cd "$(dirname "$0")/../.." || exit 2
status=0
uv run --frozen python -m unittest discover -s tests/acceptance -p 'test_begin_initial_snapshot_1.py' || status=1

reports=$(mktemp -d) || exit 2
trap 'rm -rf "$reports"' EXIT HUP INT TERM
ts_suite=sender/app/test/begin-initial-snapshot-1.test.ts
ts_report="$reports/begin-initial-snapshot-1.junit.xml"
node --test --test-reporter=spec --test-reporter-destination=stdout \
  --test-reporter=junit --test-reporter-destination="$ts_report" "$ts_suite" || status=1

if [ ! -d sender/app/android ]; then
  npm run prebuild --prefix sender || exit 2
fi
kt_suite=sender/app/modules/health-connect/android/src/test/java/org/activitytracker/healthconnect/token/IssueChangesToken1Test.kt
kt_report=sender/app/modules/health-connect/android/build/test-results/testDebugUnitTest/TEST-org.activitytracker.healthconnect.token.IssueChangesToken1Test.xml
rm -f "$kt_report"
sender/scripts/gradle.sh :health-connect:testDebugUnitTest --tests 'org.activitytracker.healthconnect.token.*' --rerun || status=1

uv run --frozen python - "$ts_suite" "$ts_report" "$kt_suite" "$kt_report" <<'EOF' || status=1
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ts_suite, ts_report, kt_suite, kt_report = (Path(argument) for argument in sys.argv[1:])
problems = []


def verify(label: str, expected: list[str], report: Path) -> None:
    if not report.is_file():
        problems.append(f"{label}: no fresh report at {report}")
        return
    cases = list(ET.parse(report).getroot().iter("testcase"))
    names = [case.get("name") for case in cases]
    if sorted(names) != sorted(expected):
        problems.append(f"{label}: executed tests differ from the suite: {sorted(set(names) ^ set(expected))}")
    unclean = [
        case.get("name")
        for case in cases
        if any(case.find(outcome) is not None for outcome in ("failure", "error", "skipped"))
    ]
    if unclean:
        problems.append(f"{label}: {len(unclean)} tests did not pass")
    else:
        print(f"begin-initial-snapshot@1: {len(expected)} {label} acceptance tests passed")


verify("TypeScript", re.findall(r'^test\("([^"]+)"', ts_suite.read_text(encoding="utf-8"), re.MULTILINE), ts_report)
verify("Kotlin", re.findall(r"@Test\s+fun\s+(\w+)\s*\(", kt_suite.read_text(encoding="utf-8")), kt_report)
if problems:
    sys.exit("begin-initial-snapshot@1: " + "; ".join(problems))
EOF
exit "$status"
