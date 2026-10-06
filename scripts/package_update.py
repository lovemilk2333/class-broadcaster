#!/usr/bin/env python3
"""Create a multi-file update package as ``*.tar.zst``.

The zstd stream contains a tar archive directly. Top-level entries are
``metadata.json`` and the files to install (no nested zip).

Full packages include every file under ``--input-dir``. Incremental packages
(``--base-dir`` / ``--base-zip``) include only new or changed files relative to
the previous install tree or green ``.zip``; the Go updater already overlays
file-by-file, so a sparse files-v1 package is enough. When the new tree deletes
paths that existed in the base, packaging falls back to a full package (overlay
cannot remove files).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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


def list_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def file_index(root: Path) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in list_files(root)}


def extract_base_zip(zip_path: Path, destination: Path) -> Path:
    """Extract a green install zip; return the install-tree root inside it."""
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(destination)
    children = [path for path in destination.iterdir() if path.is_dir()]
    # Green zip uses a single top-level folder (lovemilk-class-broadcaster/).
    if len(children) == 1 and not any(path.is_file() for path in destination.iterdir()):
        return children[0]
    return destination


def select_incremental_files(
    input_dir: Path,
    base_dir: Path,
) -> tuple[list[Path], bool, dict[str, int]]:
    """Return (files_to_pack, is_incremental, stats).

    Falls back to a full file list when the new tree deletes base paths (overlay
    cannot remove files) or when every file changed.
    """
    new_index = file_index(input_dir)
    base_index = file_index(base_dir)
    deleted = sorted(set(base_index) - set(new_index))
    if deleted:
        return (
            list_files(input_dir),
            False,
            {
                "new_files": len(new_index),
                "base_files": len(base_index),
                "changed": len(new_index),
                "deleted": len(deleted),
                "reason": "base_has_deleted_paths",
            },
        )

    changed: list[Path] = []
    unchanged = 0
    for relative, path in new_index.items():
        base_path = base_index.get(relative)
        if base_path is None:
            changed.append(path)
            continue
        try:
            if file_sha256(path) != file_sha256(base_path):
                changed.append(path)
            else:
                unchanged += 1
        except OSError:
            changed.append(path)

    stats = {
        "new_files": len(new_index),
        "base_files": len(base_index),
        "changed": len(changed),
        "unchanged": unchanged,
        "deleted": 0,
    }
    if not changed:
        # Nothing differs — still emit a tiny package with metadata only? Prefer
        # packing zero payload files is invalid for verify; fall back to full.
        return list_files(input_dir), False, {**stats, "reason": "no_changes"}
    if len(changed) >= len(new_index):
        return list_files(input_dir), False, {**stats, "reason": "all_files_changed"}
    return changed, True, stats


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
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="previous install tree; pack only new/changed files (incremental)",
    )
    parser.add_argument(
        "--base-zip",
        type=Path,
        default=None,
        help="previous green install .zip used as the incremental base",
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="ignore --base-dir/--base-zip and always pack the full tree",
    )
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        raise SystemExit(f"input directory does not exist: {args.input_dir}")
    if args.base_dir and args.base_zip:
        raise SystemExit("pass only one of --base-dir or --base-zip")
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
        base_dir: Path | None = None
        cleanup_base = False
        if not args.force_full:
            if args.base_dir is not None:
                if not args.base_dir.is_dir():
                    raise SystemExit(f"base directory does not exist: {args.base_dir}")
                base_dir = args.base_dir
            elif args.base_zip is not None:
                if not args.base_zip.is_file():
                    raise SystemExit(f"base zip does not exist: {args.base_zip}")
                extract_root = temp_path / "base-zip"
                base_dir = extract_base_zip(args.base_zip, extract_root)
                cleanup_base = True

        incremental = False
        stats: dict[str, object] = {}
        if base_dir is not None:
            files, incremental, stats = select_incremental_files(args.input_dir, base_dir)
            mode = "incremental" if incremental else "full"
            print(
                f"package base comparison mode={mode} "
                f"new={stats.get('new_files')} base={stats.get('base_files')} "
                f"changed={stats.get('changed')} deleted={stats.get('deleted')} "
                f"reason={stats.get('reason', '')}".rstrip(),
                flush=True,
            )
        else:
            files = list_files(args.input_dir)

        if not files:
            raise SystemExit(f"input directory is empty: {args.input_dir}")

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
        if incremental:
            metadata["incremental"] = True
            metadata["file_count"] = len(files)

        tar_path = temp_path / "package.tar"
        write_tar(tar_path, metadata, files, args.input_dir)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tar_path.open("rb") as source, output.open("wb") as destination:
            result = subprocess.run([args.zstd, "-3", "-q", "-c"], stdin=source, stdout=destination)
            if result.returncode:
                raise SystemExit(result.returncode)
        meta_path = Path(str(output)[: -len(".tar.zst")] + ".metadata.json")
        meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        kind = "incremental" if incremental else "full"
        print(
            f"wrote {output} kind={kind} files={len(files)} sha256={digest} components={components}",
            flush=True,
        )
        if cleanup_base:
            shutil.rmtree(temp_path / "base-zip", ignore_errors=True)


if __name__ == "__main__":
    main()
