#!/usr/bin/env python3
"""Unit tests for incremental files-v1 packaging."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import package_update as pkg  # noqa: E402
from client.core.update_progress import (  # noqa: E402
    format_speed_mib_s,
    next_progress_milestone,
)


class IncrementalPackageTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, data: bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_select_changed_and_new_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"
            new = Path(temp) / "new"
            self._write(base, "a.txt", b"aaa")
            self._write(base, "keep.bin", b"same")
            self._write(new, "a.txt", b"bbb")
            self._write(new, "keep.bin", b"same")
            self._write(new, "extra.py", b"print(1)\n")
            files, incremental, stats = pkg.select_incremental_files(new, base)
            self.assertTrue(incremental)
            self.assertEqual(stats["changed"], 2)
            relatives = sorted(path.relative_to(new).as_posix() for path in files)
            self.assertEqual(relatives, ["a.txt", "extra.py"])

    def test_deleted_paths_force_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "base"
            new = Path(temp) / "new"
            self._write(base, "gone.txt", b"x")
            self._write(base, "keep.txt", b"y")
            self._write(new, "keep.txt", b"y")
            files, incremental, stats = pkg.select_incremental_files(new, base)
            self.assertFalse(incremental)
            self.assertEqual(stats.get("reason"), "base_has_deleted_paths")
            self.assertEqual(len(files), 1)

    def test_base_zip_extract_and_package(self) -> None:
        zstd = shutil.which("zstd")
        if not zstd:
            self.skipTest("zstd not installed")
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            base_tree = temp_path / "base-tree"
            new_tree = temp_path / "new-tree"
            self._write(base_tree, "client/app.pyc", b"old")
            self._write(base_tree, "runtime/python.exe", b"pe")
            self._write(new_tree, "client/app.pyc", b"new")
            self._write(new_tree, "runtime/python.exe", b"pe")
            zip_path = temp_path / "prev.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                for path in base_tree.rglob("*"):
                    if path.is_file():
                        archive.write(path, arcname=f"lovemilk-class-broadcaster/{path.relative_to(base_tree).as_posix()}")
            out = temp_path / "out.tar.zst"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "package_update.py"),
                    "--components",
                    "client",
                    "--version",
                    "0.0.2",
                    "--platform",
                    "windows-amd64",
                    "--input-dir",
                    str(new_tree),
                    "--output",
                    str(out),
                    "--client-version",
                    "0.0.2",
                    "--base-zip",
                    str(zip_path),
                    "--zstd",
                    zstd,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(out.is_file())
            meta = json.loads((temp_path / "out.metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(meta.get("incremental"))
            self.assertEqual(meta["payload_format"], "files-v1")
            self.assertEqual(meta["file_count"], 1)
            # Manifest digest must match the single changed file only.
            changed = new_tree / "client" / "app.pyc"
            entries = [{
                "path": "client/app.pyc",
                "size": changed.stat().st_size,
                "sha256": hashlib.sha256(changed.read_bytes()).hexdigest(),
            }]
            expected = hashlib.sha256(
                json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            ).hexdigest()
            self.assertEqual(meta["sha256"], expected)


class ProgressMilestoneTests(unittest.TestCase):
    def test_milestones_order(self) -> None:
        logged: set[float] = set()
        self.assertIsNone(next_progress_milestone(0.0, logged))
        self.assertEqual(next_progress_milestone(0.1, logged), 0.0)
        logged.add(0.0)
        self.assertEqual(next_progress_milestone(25.0, logged), 25.0)
        logged.add(25.0)
        self.assertEqual(next_progress_milestone(50.0, logged), 50.0)
        logged.add(50.0)
        self.assertEqual(next_progress_milestone(75.0, logged), 75.0)
        logged.add(75.0)
        self.assertIsNone(next_progress_milestone(99.0, logged))
        self.assertEqual(next_progress_milestone(99.1, logged), 99.0)

    def test_speed_format(self) -> None:
        self.assertEqual(format_speed_mib_s(1024 * 1024), "1.00MiB/s")


if __name__ == "__main__":
    unittest.main()
