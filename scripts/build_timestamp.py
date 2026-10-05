#!/usr/bin/env python3
"""Emit / validate the canonical build timestamp stamp.

Format: ``yyyy-mm-dd HH:MM:SS.xxxx±xx:xx``
  - local wall clock with numeric UTC offset (ISO-8601 style offset)
  - fractional seconds truncated to 4 digits (100 µs resolution)
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime

# 2026-10-05 14:30:45.1234+08:00
BUILD_TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r" (?P<time>\d{2}:\d{2}:\d{2})"
    r"\.(?P<frac>\d{4})"
    r"(?P<offset>[+-]\d{2}:\d{2})$"
)
# Older packages only stamped the calendar day.
LEGACY_BUILD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def format_build_timestamp(when: datetime | None = None) -> str:
    """Return the canonical stamp for *when* (default: local now with offset)."""
    moment = when if when is not None else datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    else:
        moment = moment.astimezone()
    # Four fractional digits (truncate, do not round — stable across languages).
    frac = f"{moment.microsecond:06d}"[:4]
    offset = moment.strftime("%z")  # +0800 / -0530
    if len(offset) == 5 and offset[0] in "+-":
        offset = f"{offset[:3]}:{offset[3:]}"
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')}.{frac}{offset}"


def is_valid_build_timestamp(value: str, *, allow_legacy_date: bool = True) -> bool:
    text = str(value or "").strip()
    if not text or text.lower() == "dev":
        return False
    if BUILD_TIMESTAMP_RE.fullmatch(text):
        return True
    if allow_legacy_date and LEGACY_BUILD_DATE_RE.fullmatch(text):
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        metavar="VALUE",
        help="validate a stamp and exit 0/2",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="with --check, reject legacy YYYY-MM-DD",
    )
    args = parser.parse_args()
    if args.check is not None:
        ok = is_valid_build_timestamp(args.check, allow_legacy_date=not args.strict)
        raise SystemExit(0 if ok else 2)
    print(format_build_timestamp(), end="")


if __name__ == "__main__":
    main()
