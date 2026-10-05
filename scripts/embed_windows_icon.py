#!/usr/bin/env python3
"""Embed the application ICO into Windows PE builds via rsrc .syso files."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


TARGETS = {
    "launcher": "cmd/client-launcher",
    "updater": "updater",
}


def run(command: list[str], cwd: Path | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def find_rsrc() -> str:
    path = shutil.which("rsrc")
    if path:
        return path
    gopath = subprocess.check_output(["go", "env", "GOPATH"], text=True).strip()
    candidate = Path(gopath) / "bin" / "rsrc"
    if candidate.is_file():
        return str(candidate)
    raise SystemExit("rsrc not found; install with: go install github.com/akavel/rsrc@latest")


def embed(package_dir: Path, icon: Path, arch: str = "amd64") -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    # go build picks up *_windows_amd64.syso automatically for GOOS=windows GOARCH=amd64.
    output = package_dir / f"rsrc_windows_{arch}.syso"
    # Remove stale resources so a missing icon cannot leave an old embedding behind.
    for stale in package_dir.glob("rsrc*.syso"):
        stale.unlink()
    run([find_rsrc(), "-arch", arch, "-ico", str(icon), "-o", str(output)])
    if not output.is_file() or output.stat().st_size < 100:
        raise SystemExit(f"rsrc produced an invalid object: {output}")
    print(f"embedded windows icon resource: {output} ({output.stat().st_size} bytes)", flush=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--icon", type=Path)
    parser.add_argument(
        "--target",
        action="append",
        choices=("launcher", "updater", "all"),
        default=None,
    )
    args = parser.parse_args()
    root = args.root.resolve()
    icon = (args.icon or root / "assets/icons/mkcb.ico").resolve()
    if not icon.is_file():
        raise SystemExit(f"icon not found: {icon}")

    selected = args.target or ["all"]
    if "all" in selected:
        selected = list(TARGETS)

    for name in selected:
        package = TARGETS[name]
        embed(root / package, icon)


if __name__ == "__main__":
    main()
