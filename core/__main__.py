"""Командний рядок: `python -m core check` та `python -m core init`."""

from __future__ import annotations

import argparse
import sys

from core.config import get_settings
from core.db import open_db
from core.system_check import format_report, run_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core", description="Автомонтаж: службові команди")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="перевірити FFmpeg, NVENC, CUDA")
    sub.add_parser("init", help="створити папки і базу даних")
    args = parser.parse_args(argv)

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "check":
        report = run_all()
        print(format_report(report))
        return 0 if report.ok else 1

    settings = get_settings()
    settings.ensure_dirs()
    conn = open_db(settings.database_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    print(f"База: {settings.database_path} (схема v{version})")
    print(f"Бібліотека: {settings.library_path}")
    print(f"Inbox: {settings.inbox_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
