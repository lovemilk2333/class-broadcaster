"""Applied update digests — thin wrappers over ``StateStore``.

Force catch-up used to re-offer the same digest after a successful overlay when
``client_version`` in the package matched the already-running build (or when
incremental overlays left ``version.json`` unchanged). Remembering applied
digests lets the restarted client refuse the same package immediately.

Additionally, digests that were applied locally but not yet acknowledged to the
server stay in ``pending_report`` until ``UpdateApplied`` is flushed after
reconnect.

Persistence lives in ``data/state.json`` under the ``applied_updates`` key.
Legacy ``data/applied_updates.json`` is migrated once by ``StateStore.load``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .store import StateStore, _normalize_digest

_MAX_DIGESTS = 64


def _store(data_dir: Path) -> StateStore:
    return StateStore.for_data_dir(data_dir)


def load_applied_digests(data_dir: Path) -> set[str]:
    return _store(data_dir).applied_update_digests()


def mark_update_applied(data_dir: Path, digest: str, *, max_digests: int = _MAX_DIGESTS) -> None:
    _store(data_dir).mark_update_applied(digest, max_digests=max_digests)


def is_update_applied(data_dir: Path, digest: str) -> bool:
    cleaned = _normalize_digest(digest)
    if not cleaned:
        return False
    return cleaned in load_applied_digests(data_dir)


def pending_update_reports(data_dir: Path) -> list[str]:
    """Return digests applied locally but not yet reported to the server."""
    return _store(data_dir).pending_update_reports()


def mark_update_reported(data_dir: Path, digest: str) -> None:
    """Remove a digest from the pending server-report list after a successful send."""
    _store(data_dir).mark_update_reported(digest)


def filter_unapplied(data_dir: Path, digests: Iterable[str]) -> list[str]:
    applied = load_applied_digests(data_dir)
    result: list[str] = []
    for item in digests:
        value = _normalize_digest(item)
        if value and value not in applied:
            result.append(value)
    return result
