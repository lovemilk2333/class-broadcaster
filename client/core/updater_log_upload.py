"""Forward detached updater logs to the server after the client restarts.

The Go updater writes JSON lines to ``data/updater.log`` while the client is
offline. On reconnect the client uploads unread lines as ClientLog entries with
``logger=mkcb.updater`` so operators can diagnose failed applies in the admin UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


UPDATER_LOGGER = "mkcb.updater"
_OFFSET_NAME = "updater.log.offset"


def updater_log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "updater.log"


def offset_path(data_dir: Path) -> Path:
    return Path(data_dir) / _OFFSET_NAME


def read_offset(data_dir: Path) -> int:
    path = offset_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        value = int(raw)
        return value if value >= 0 else 0
    except (OSError, ValueError):
        return 0


def write_offset(data_dir: Path, offset: int) -> None:
    path = offset_path(data_dir)
    try:
        path.write_text(str(max(0, int(offset))), encoding="utf-8")
    except OSError:
        pass


def _normalize_entry(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            msg = str(parsed.get("msg") or parsed.get("message") or "").strip()
            if not msg:
                return None
            entry = {
                "time": str(parsed.get("time") or ""),
                "level": str(parsed.get("level") or "INFO").upper(),
                "msg": msg,
                "logger": str(parsed.get("logger") or UPDATER_LOGGER),
                "source": str(parsed.get("source") or "updater"),
            }
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 16 * 1024:
                entry["msg"] = msg[:3000]
                encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            return encoded
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    # Legacy plain-text updater lines.
    entry = {
        "time": "",
        "level": "INFO",
        "msg": text[:4000],
        "logger": UPDATER_LOGGER,
        "source": "updater",
    }
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def collect_pending_updater_logs(data_dir: Path, *, max_entries: int = 200) -> tuple[list[str], int]:
    """Return (json_entries, new_offset) for unread updater.log lines."""
    path = updater_log_path(data_dir)
    if not path.is_file():
        return [], read_offset(data_dir)
    offset = read_offset(data_dir)
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        # Log rotated / truncated — re-read from start.
        offset = 0
    if size == offset:
        return [], offset
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
            new_offset = stream.tell()
    except OSError:
        return [], offset
    text = chunk.decode("utf-8", errors="replace")
    entries: list[str] = []
    for line in text.splitlines():
        normalized = _normalize_entry(line)
        if normalized:
            entries.append(normalized)
            if len(entries) >= max_entries:
                break
    return entries, new_offset


def queue_pending_updater_logs(data_dir: Path, enqueue: Any, *, max_entries: int = 200) -> int:
    """Enqueue pending updater log JSON strings; advance offset only after enqueue.

    ``enqueue`` must accept one JSON string (same shape as client log upload).
    Returns how many entries were queued.
    """
    entries, new_offset = collect_pending_updater_logs(data_dir, max_entries=max_entries)
    if not entries:
        if new_offset != read_offset(data_dir):
            write_offset(data_dir, new_offset)
        return 0
    queued = 0
    for entry in entries:
        try:
            enqueue(entry)
            queued += 1
        except Exception:
            break
    if queued:
        # Only advance when we queued everything we planned; otherwise keep offset
        # so the remainder can retry on the next connect.
        if queued == len(entries):
            write_offset(data_dir, new_offset)
    return queued
