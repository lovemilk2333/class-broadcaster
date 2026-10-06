"""Persist applied update package digests across client restarts.

Force catch-up used to re-offer the same sha after a successful overlay when
``client_version`` in the package matched the already-running build (or when
incremental overlays left ``version.json`` unchanged). Remembering applied
sha256 values lets the restarted client refuse the same package immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


_FILE_NAME = "applied_updates.json"
_MAX_DIGESTS = 64


def applied_updates_path(data_dir: Path) -> Path:
    return Path(data_dir) / _FILE_NAME


def load_applied_digests(data_dir: Path) -> set[str]:
    path = applied_updates_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()
    digests: list[str] = []
    if isinstance(document, dict):
        items = document.get("sha256") or document.get("digests") or []
        if isinstance(items, list):
            digests = [str(item).strip().lower() for item in items]
    elif isinstance(document, list):
        digests = [str(item).strip().lower() for item in document]
    return {d for d in digests if len(d) == 64 and all(ch in "0123456789abcdef" for ch in d)}


def mark_update_applied(data_dir: Path, digest: str, *, max_digests: int = _MAX_DIGESTS) -> None:
    digest = str(digest or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return
    existing = [d for d in load_applied_digests(data_dir) if d != digest]
    ordered = [digest, *existing][: max(1, int(max_digests))]
    path = applied_updates_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"sha256": ordered}, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError:
        pass


def is_update_applied(data_dir: Path, digest: str) -> bool:
    digest = str(digest or "").strip().lower()
    if not digest:
        return False
    return digest in load_applied_digests(data_dir)


def filter_unapplied(data_dir: Path, digests: Iterable[str]) -> list[str]:
    applied = load_applied_digests(data_dir)
    result: list[str] = []
    for item in digests:
        value = str(item or "").strip().lower()
        if value and value not in applied:
            result.append(value)
    return result
