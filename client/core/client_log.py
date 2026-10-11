"""Local client.log size policy: trim oldest half of lines when over threshold."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional, Union

# Default / bounds for data/state.json ``log_max_bytes``.
DEFAULT_LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
MIN_LOG_MAX_BYTES = 64 * 1024  # 64 KiB
MAX_LOG_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB

_ROTATION_BACKUP_RE = re.compile(r"^client\.log\.\d+$")


def clamp_log_max_bytes(value: object, *, default: int = DEFAULT_LOG_MAX_BYTES) -> int:
    """Normalize a configured byte threshold into the allowed band."""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(default)
    if parsed < MIN_LOG_MAX_BYTES:
        return MIN_LOG_MAX_BYTES
    if parsed > MAX_LOG_MAX_BYTES:
        return MAX_LOG_MAX_BYTES
    return parsed


def cleanup_legacy_rotation_backups(data_dir: Union[str, os.PathLike]) -> int:
    """Remove historic ``client.log.1`` / ``.2`` / ``.3`` rotation leftovers.

    Returns the number of files removed.
    """
    root = Path(data_dir)
    removed = 0
    try:
        for path in root.iterdir():
            if not path.is_file():
                continue
            if _ROTATION_BACKUP_RE.match(path.name):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    except OSError:
        return removed
    return removed


def trim_client_log_file(
    path: Union[str, os.PathLike],
    max_bytes: int,
) -> int:
    """If ``path`` is at least ``max_bytes``, drop the oldest ``floor(n/2)`` lines.

    Odd line counts use ``Math.floor(n/2)`` (e.g. 5 → drop 2, keep 3).
    When there is only one line but the file is still over the threshold,
    that line is removed so a single huge entry cannot block further writes.

    Returns the number of lines dropped (0 when no trim ran).
    """
    target = Path(path)
    limit = clamp_log_max_bytes(max_bytes)
    try:
        if not target.is_file() or target.stat().st_size < limit:
            return 0
    except OSError:
        return 0

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    if not text:
        return 0

    lines = text.splitlines()
    count = len(lines)
    if count == 0:
        return 0

    drop = count // 2
    if drop == 0:
        # Single oversized line (or empty-ish): clear so size can recover.
        drop = count

    kept = lines[drop:]
    # JSONL: always terminate kept lines with a newline.
    body = ("\n".join(kept) + "\n") if kept else ""

    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd: Optional[int] = None
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(directory),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = None
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
        tmp_name = ""
    except OSError:
        return 0
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return drop
