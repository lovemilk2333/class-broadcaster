"""Client release identity loaded from ``version.json``.

Canonical source is the repo-root ``version.json`` with separate
``server`` / ``client`` keys. Packaged builds ship a component-local copy next
to this module (``client/version.json``)::

    { "version": "<client>", "build_date": "..." }

When running from a source checkout, the root file is read and the ``client``
key is selected (legacy single ``version`` still works).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _candidate_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    return [
        here / "version.json",  # packaged / synced: client/version.json
        here.parent / "version.json",  # repo root when running from source
    ]


def _normalize_document(document: dict[str, Any]) -> dict[str, str] | None:
    build_date = str(document.get("build_date") or "").strip()
    # Component-local shape written by sync_version_json.
    if document.get("version"):
        return {
            "version": str(document["version"]).strip(),
            "build_date": build_date,
        }
    # Root shape with separate server/client (or legacy single version).
    if document.get("client"):
        return {
            "version": str(document["client"]).strip(),
            "build_date": build_date,
        }
    return None


def _load_document() -> dict[str, str]:
    for path in _candidate_paths():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(document, dict):
            continue
        normalized = _normalize_document(document)
        if normalized and normalized["version"]:
            return normalized
    return {"version": "0.0.0", "build_date": ""}


_DOC = _load_document()

VERSION_STR = str(_DOC.get("version") or "0.0.0").strip()
BUILD_DATE = str(_DOC.get("build_date") or "").strip()

try:
    VERSION = tuple(int(part) for part in VERSION_STR.split("."))
except ValueError:
    VERSION = (0, 0, 0)


def display_version(include_build_date: bool = True) -> str:
    """Human-readable version line for About / clipboard."""
    if not include_build_date:
        return VERSION_STR
    if BUILD_DATE and BUILD_DATE.lower() != "dev":
        return f"{VERSION_STR} ({BUILD_DATE})"
    return f"{VERSION_STR} (dev)"
