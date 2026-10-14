#!/bin/sh
# Run the manage-sender-installation@1 acceptance pack.
# The suite passes only when every test method in the bound suite was
# loaded, ran, and passed, with none failed, errored, skipped, or expected
# to fail, so a loader or runner change cannot shrink what is checked.
cd "$(dirname "$0")/../.." || exit 2
exec uv run --frozen python - tests/acceptance/test_manage_sender_installation_1.py <<'PY'
import ast
import sys
import unittest
from pathlib import Path

suite_path = Path(sys.argv[1])
tree = ast.parse(suite_path.read_text(encoding="utf-8"))
expected = sorted(
    f"{node.name}.{item.name}"
    for node in tree.body
    if isinstance(node, ast.ClassDef)
    for item in node.body
    if isinstance(item, ast.FunctionDef) and item.name.startswith("test_")
)


def flatten(group):
    for item in group:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


suite = unittest.defaultTestLoader.discover(str(suite_path.parent), pattern=suite_path.name, top_level_dir=str(suite_path.parent))
# Running a suite releases its tests, so record what was loaded first.
loaded = sorted(f"{type(test).__name__}.{test._testMethodName}" for test in flatten(suite))
result = unittest.TextTestRunner(verbosity=2).run(suite)
problems = []
if loaded != expected:
    problems.append(f"loaded tests differ from the suite's test methods: {sorted(set(loaded) ^ set(expected))}")
if result.testsRun != len(expected):
    problems.append(f"ran {result.testsRun} of {len(expected)} tests")
outcomes = (
    ("failed", result.failures),
    ("errored", result.errors),
    ("skipped", result.skipped),
    ("expected failures", result.expectedFailures),
    ("unexpected successes", result.unexpectedSuccesses),
)
for label, entries in outcomes:
    if entries:
        problems.append(f"{len(entries)} {label}")
if problems:
    sys.exit("manage-sender-installation@1: " + "; ".join(problems))
print(f"manage-sender-installation@1: {len(expected)} acceptance tests passed")
PY
