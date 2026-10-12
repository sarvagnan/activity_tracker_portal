"""Tests for the Portal runtime requirement and command skeleton."""

from __future__ import annotations

import io
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import version
from unittest.mock import patch

from activity_tracker_portal import cli, runtime


class SQLiteRuntimeTests(unittest.TestCase):
    def test_supported_versions(self) -> None:
        for sqlite_version in ((3, 51, 3), (3, 51, 4), (3, 52, 0), (3, 53, 1), (4, 0, 0)):
            with self.subTest(version=sqlite_version):
                self.assertTrue(runtime.is_sqlite_version_supported(sqlite_version))
                runtime.require_supported_sqlite(sqlite_version)

    def test_unsupported_versions(self) -> None:
        for sqlite_version in ((3, 51, 2), (3, 50, 4)):
            with self.subTest(version=sqlite_version):
                self.assertFalse(runtime.is_sqlite_version_supported(sqlite_version))
                with self.assertRaisesRegex(runtime.UnsupportedSQLiteVersion, "requires SQLite 3\\.51\\.3 or newer"):
                    runtime.require_supported_sqlite(sqlite_version)

    def test_running_sqlite_meets_minimum(self) -> None:
        self.assertGreaterEqual(sqlite3.sqlite_version_info, runtime.MIN_SQLITE_VERSION)
        self.assertTrue(runtime.is_sqlite_version_supported())
        runtime.require_supported_sqlite()


class PortalCommandTests(unittest.TestCase):
    def test_runtime_reports_versions(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(["runtime"])
        self.assertEqual(result, 0)
        self.assertIn(f"Python {sys.version.split()[0]}", stdout.getvalue())
        self.assertIn(f"SQLite {sqlite3.sqlite_version}", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_runtime_refuses_unsupported_sqlite(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sqlite3, "sqlite_version_info", (3, 50, 4)), patch.object(sqlite3, "sqlite_version", "3.50.4"):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli.main(["runtime"])
        self.assertEqual(result, 1)
        self.assertIn("SQLite 3.50.4", stdout.getvalue())
        self.assertIn("SQLite 3.50.4 is unsupported", stderr.getvalue())
        self.assertIn("requires SQLite 3.51.3 or newer", stderr.getvalue())

    def test_help_exits_successfully(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as exit_result:
            cli.main(["--help"])
        self.assertEqual(exit_result.exception.code, 0)
        self.assertIn("usage: portal", stdout.getvalue())
        self.assertIn("runtime", stdout.getvalue())

    def test_version_reports_package_version(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as exit_result:
            cli.main(["--version"])
        self.assertEqual(exit_result.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"portal {version('activity-tracker-portal')}")


if __name__ == "__main__":
    unittest.main()
