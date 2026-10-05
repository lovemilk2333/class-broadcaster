#!/usr/bin/env python3
"""Sync / stamp version.json for client and server builds.

Canonical source: <repo>/version.json

Shape::

    {
      "server": "0.1.0",
      "client": "0.1.0",
      "build_date": ""
    }

Legacy single ``"version"`` is still accepted and applied to both components.

Each target receives a component-local document::

    { "version": "<server|client>", "build_date": "..." }

``build_date`` is a local build timestamp
``yyyy-mm-dd HH:MM:SS.xxxx±xx:xx`` (4 fractional digits + ISO offset).
Legacy ``YYYY-MM-DD`` stamps are still accepted when reading old packages.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from build_timestamp import is_valid_build_timestamp  # noqa: E402


def _as_version_string(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SystemExit(f"version.json missing non-empty {label}")
    return text


def load_root(path: Path) -> dict[str, str]:
    """Return ``{server, client, build_date}`` from the canonical root file."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"invalid version.json: {path}")
    build_date = str(document.get("build_date") or "").strip()

    # New shape: separate server / client.
    has_server = "server" in document
    has_client = "client" in document
    if has_server or has_client:
        if not has_server or not has_client:
            raise SystemExit(
                f"version.json must define both 'server' and 'client' when split: {path}"
            )
        return {
            "server": _as_version_string(document.get("server"), label="server"),
            "client": _as_version_string(document.get("client"), label="client"),
            "build_date": build_date,
        }

    # Legacy: single "version" for both.
    if "version" in document:
        shared = _as_version_string(document.get("version"), label="version")
        return {"server": shared, "client": shared, "build_date": build_date}

    raise SystemExit(
        f"invalid version.json (need server+client or legacy version): {path}"
    )


def component_document(root: dict[str, str], component: str, build_date: str) -> dict[str, str]:
    if component not in ("server", "client"):
        raise SystemExit(f"unknown component {component!r}")
    return {
        "version": root[component],
        "build_date": build_date,
    }


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--build-date",
        default="",
        help=(
            "build timestamp to stamp: 'yyyy-mm-dd HH:MM:SS.xxxx±xx:xx' "
            "(legacy YYYY-MM-DD also accepted); empty keeps root build_date (usually blank → dev)"
        ),
    )
    parser.add_argument(
        "--targets",
        nargs="*",
        default=["server", "client-source"],
        choices=("server", "client-source", "client-bundle"),
        help="Where to write the (optionally stamped) component version.json",
    )
    parser.add_argument(
        "--client-bundle-dir",
        type=Path,
        help="Packaged client root (writes <dir>/client/version.json)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    source = root / "version.json"
    versions = load_root(source)
    build_date = str(args.build_date or "").strip() or versions["build_date"]
    if build_date:
        if not is_valid_build_timestamp(build_date, allow_legacy_date=True):
            raise SystemExit(
                f"invalid --build-date {build_date!r}; "
                "expected 'yyyy-mm-dd HH:MM:SS.xxxx±xx:xx' (or legacy YYYY-MM-DD)"
            )

    written: list[tuple[str, Path, dict[str, str]]] = []
    for target in args.targets:
        if target == "server":
            document = component_document(versions, "server", build_date)
            path = root / "server" / "internal" / "version" / "version.json"
            write_json(path, document)
            written.append(("server", path, document))
        elif target == "client-source":
            document = component_document(versions, "client", build_date)
            path = root / "client" / "version.json"
            write_json(path, document)
            written.append(("client", path, document))
        elif target == "client-bundle":
            if args.client_bundle_dir is None:
                raise SystemExit("--client-bundle-dir is required for client-bundle")
            document = component_document(versions, "client", build_date)
            path = args.client_bundle_dir.resolve() / "client" / "version.json"
            write_json(path, document)
            written.append(("client", path, document))
        else:
            raise SystemExit(f"unknown target {target}")

    parts = []
    for component, path, document in written:
        rel = str(path.relative_to(root)) if str(path).startswith(str(root)) else str(path)
        parts.append(
            f"{component}={document['version']} build_date={document['build_date'] or 'dev'} -> {rel}"
        )
    print("; ".join(parts), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
