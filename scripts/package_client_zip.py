#!/usr/bin/env python3
"""Create a green Windows client install zip (first-time install only).

This is NOT an update package. Server publish only accepts ``.tar.zst`` updates
built by ``package_update.py``. The zip is a plain archive of the install tree
so admins can unzip and run ``lovemilk-class-broadcaster.exe``.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Zip a Windows client install tree for green install")
    parser.add_argument("--input-dir", type=Path, required=True, help="built client directory")
    parser.add_argument("--output", type=Path, required=True, help="output .zip path")
    parser.add_argument(
        "--top-level",
        default="lovemilk-class-broadcaster",
        help="directory name inside the zip (default: lovemilk-class-broadcaster)",
    )
    args = parser.parse_args()
    root = args.input_dir.resolve()
    if not root.is_dir():
        raise SystemExit(f"input directory does not exist: {root}")
    output = args.output
    if not str(output).lower().endswith(".zip"):
        output = output.with_suffix(output.suffix + ".zip") if output.suffix else output.with_suffix(".zip")
    top = str(args.top_level).strip().strip("/\\") or "lovemilk-class-broadcaster"
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not files:
        raise SystemExit(f"input directory is empty: {root}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source in files:
            relative = source.relative_to(root).as_posix()
            archive.write(source, arcname=f"{top}/{relative}")
    print(f"wrote green install zip {output} files={len(files)}", flush=True)


if __name__ == "__main__":
    main()
