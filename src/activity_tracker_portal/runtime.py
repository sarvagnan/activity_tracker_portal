"""SQLite runtime requirements for the Portal."""

from __future__ import annotations

import sqlite3

MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 51, 3)


class UnsupportedSQLiteVersion(RuntimeError):
    """The SQLite runtime is older than the Portal's patched minimum."""


def is_sqlite_version_supported(version: tuple[int, int, int] = sqlite3.sqlite_version_info) -> bool:
    """Return whether the SQLite version meets the patched minimum."""
    return version >= MIN_SQLITE_VERSION


def require_supported_sqlite(version: tuple[int, int, int] = sqlite3.sqlite_version_info) -> None:
    """Raise UnsupportedSQLiteVersion when the SQLite version is too old."""
    if not is_sqlite_version_supported(version):
        actual = ".".join(str(part) for part in version)
        minimum = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise UnsupportedSQLiteVersion(f"SQLite {actual} is unsupported; Portal requires SQLite {minimum} or newer.")
