"""Persist applied update package digests across client restarts.

Force catch-up used to re-offer the same digest after a successful overlay when
``client_version`` in the package matched the already-running build (or when
incremental overlays left ``version.json`` unchanged). Remembering applied
digests lets the restarted client refuse the same package immediately.

Additionally, digests that were applied locally but not yet acknowledged to the
server stay in ``pending_report`` until ``UpdateApplied`` is flushed after
reconnect.

On-disk JSON shape (current)::

    {"digests": ["…"], "pending_report": ["…"]}

Legacy keys ``sha256`` / list-only files are still readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


_FILE_NAME = "applied_updates.json"
_MAX_DIGESTS = 64


def applied_updates_path(data_dir: Path) -> Path:
    return Path(data_dir) / _FILE_NAME


def _normalize_digest(digest: object) -> str:
    value = str(digest or "").strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        return ""
    return value


def _normalize_digest_list(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        digest = _normalize_digest(item)
        if not digest or digest in seen:
            continue
        seen.add(digest)
        ordered.append(digest)
    return ordered


def _load_document(data_dir: Path) -> dict[str, object]:
    path = applied_updates_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"digests": [], "pending_report": []}
    if isinstance(document, list):
        digests = _normalize_digest_list(document)
        return {"digests": digests, "pending_report": []}
    if not isinstance(document, dict):
        return {"digests": [], "pending_report": []}
    # Prefer digests; accept legacy "sha256" key from older clients.
    digests = _normalize_digest_list(
        document.get("digests") or document.get("sha256") or []
    )
    pending = _normalize_digest_list(document.get("pending_report") or [])
    # Only report digests that are still considered applied locally.
    applied = set(digests)
    pending = [d for d in pending if d in applied]
    return {"digests": digests, "pending_report": pending}


def _write_document(data_dir: Path, digests: list[str], pending: list[str]) -> None:
    path = applied_updates_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"digests": digests, "pending_report": pending},
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def load_applied_digests(data_dir: Path) -> set[str]:
    document = _load_document(data_dir)
    return set(_normalize_digest_list(document.get("digests")))


def mark_update_applied(data_dir: Path, digest: str, *, max_digests: int = _MAX_DIGESTS) -> None:
    digest = _normalize_digest(digest)
    if not digest:
        return
    document = _load_document(data_dir)
    existing = [d for d in _normalize_digest_list(document.get("digests")) if d != digest]
    ordered = [digest, *existing][: max(1, int(max_digests))]
    pending = [d for d in _normalize_digest_list(document.get("pending_report")) if d != digest]
    pending = [digest, *pending][: max(1, int(max_digests))]
    _write_document(data_dir, ordered, pending)


def is_update_applied(data_dir: Path, digest: str) -> bool:
    digest = _normalize_digest(digest)
    if not digest:
        return False
    return digest in load_applied_digests(data_dir)


def pending_update_reports(data_dir: Path) -> list[str]:
    """Return digests applied locally but not yet reported to the server."""
    document = _load_document(data_dir)
    return list(_normalize_digest_list(document.get("pending_report")))


def mark_update_reported(data_dir: Path, digest: str) -> None:
    """Remove a digest from the pending server-report list after a successful send."""
    digest = _normalize_digest(digest)
    if not digest:
        return
    document = _load_document(data_dir)
    digests = _normalize_digest_list(document.get("digests"))
    pending = [d for d in _normalize_digest_list(document.get("pending_report")) if d != digest]
    _write_document(data_dir, digests, pending)


def filter_unapplied(data_dir: Path, digests: Iterable[str]) -> list[str]:
    applied = load_applied_digests(data_dir)
    result: list[str] = []
    for item in digests:
        value = _normalize_digest(item)
        if value and value not in applied:
            result.append(value)
    return result
