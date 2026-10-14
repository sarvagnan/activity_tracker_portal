"""Command-line entry point for the Portal."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from importlib.metadata import version

from activity_tracker_portal.runtime import UnsupportedSQLiteVersion, require_supported_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portal", description="Activity Tracker Portal")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('activity-tracker-portal')}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("runtime", help="Report and check the Python and SQLite runtime")
    serve = commands.add_parser("serve", help="Run the single-process Portal ingress")
    serve.add_argument("--config", required=True, help="Path to the Portal JSON configuration")
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        from activity_tracker_portal.ingress import PortalSettings, create_server_config

        try:
            settings = PortalSettings.from_file(args.config)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        uvicorn.Server(create_server_config(settings)).run()
        return 0

    print(f"Python {sys.version.split()[0]}")
    print(f"SQLite {sqlite3.sqlite_version}")
    try:
        require_supported_sqlite(sqlite3.sqlite_version_info)
    except UnsupportedSQLiteVersion as error:
        print(error, file=sys.stderr)
        return 1
    return 0
