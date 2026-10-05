#!/usr/bin/env python3
"""Create a multi-file update package as ``*.tar.zst``.

The zstd stream contains a tar archive directly. Top-level entries are
``metadata.json`` and the files to install (no nested zip).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path


def file_manifest(files: list[Path], root: Path) -> bytes:
    """Build the canonical files-v1 manifest bytes.

    Entries must be ordered by the POSIX path *string* (not pathlib.Path order).
    """
    entries = []
    for path in files:
        data = path.read_bytes()
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    entries.sort(key=lambda item: item["path"])
    return json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def write_tar(path: Path, metadata: dict[str, object], files: list[Path], root: Path) -> None:
    """写入确定性的直接 tar 容器。"""
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        metadata_bytes = (json.dumps(metadata, sort_keys=True, ensure_ascii=True) + "\n").encode()
        info = tarfile.TarInfo("metadata.json")
        info.size = len(metadata_bytes)
        info.mode = 0o600
        info.mtime = 0
        archive.addfile(info, io.BytesIO(metadata_bytes))
        for source in files:
            relative = source.relative_to(root).as_posix()
            info = archive.gettarinfo(str(source), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with source.open("rb") as stream:
                archive.addfile(info, stream)


def normalize_components(raw: list[str]) -> list[str]:
    """Canonical order: updater first, then client (matches install priority)."""
    allowed = {"client", "updater"}
    seen: set[str] = set()
    ordered: list[str] = []
    for name in raw:
        item = str(name).strip().lower()
        if item not in allowed:
            raise SystemExit(f"unsupported component {name!r}; expected client and/or updater")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    if not ordered:
        raise SystemExit("at least one component is required")
    rank = {"updater": 0, "client": 1}
    ordered.sort(key=lambda item: rank[item])
    return ordered


def normalize_output_path(path: Path) -> Path:
    """Force the canonical ``*.tar.zst`` suffix."""
    name = path.name
    lower = name.lower()
    if lower.endswith(".tar.zst") or lower.endswith(".tar.zstd"):
        if lower.endswith(".tar.zstd"):
            return path.with_name(name[: -len(".tar.zstd")] + ".tar.zst")
        return path
    stem = name
    for suffix in (".zst", ".zstd", ".zip", ".tar"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return path.with_name(stem + ".tar.zst")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a files-v1 .tar.zst update package")
    parser.add_argument(
        "--component",
        action="append",
        dest="components",
        choices=("client", "updater"),
        help="component to include; repeat for multi-component packages",
    )
    parser.add_argument(
        "--components",
        nargs="+",
        dest="components_list",
        choices=("client", "updater"),
        help="space-separated components (alias of repeated --component)",
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="output path (forced to *.tar.zst)")
    parser.add_argument("--client-version", default="")
    parser.add_argument("--updater-version", default="")
    parser.add_argument("--zstd", default="zstd")
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        raise SystemExit(f"input directory does not exist: {args.input_dir}")
    output = normalize_output_path(args.output)
    raw_components: list[str] = []
    if args.components:
        raw_components.extend(args.components)
    if args.components_list:
        raw_components.extend(args.components_list)
    if not raw_components:
        raise SystemExit("pass --component and/or --components")
    components = normalize_components(raw_components)
    with tempfile.TemporaryDirectory(prefix="mkcb-package-") as temp:
        temp_path = Path(temp)
        files = sorted(
            (path for path in args.input_dir.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(args.input_dir).as_posix(),
        )
        digest = hashlib.sha256(file_manifest(files, args.input_dir)).hexdigest()
        primary = components[0] if len(components) == 1 else "bundle"
        metadata: dict[str, object] = {
            "components": components,
            "component": primary,
            "version": args.version,
            "platform": args.platform,
            "sha256": digest,
            "payload_format": "files-v1",
            "client_version": args.client_version if "client" in components else "",
            "updater_version": args.updater_version if "updater" in components else "",
        }
        tar_path = temp_path / "package.tar"
        write_tar(tar_path, metadata, files, args.input_dir)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tar_path.open("rb") as source, output.open("wb") as destination:
            result = subprocess.run([args.zstd, "-3", "-q", "-c"], stdin=source, stdout=destination)
            if result.returncode:
                raise SystemExit(result.returncode)
        meta_path = Path(str(output)[: -len(".tar.zst")] + ".metadata.json")
        meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(f"wrote {output} sha256={digest} components={components}", flush=True)


if __name__ == "__main__":
    main()
