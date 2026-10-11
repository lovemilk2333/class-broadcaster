import tempfile
import unittest
from pathlib import Path

from client.core.client_log import (
    DEFAULT_LOG_MAX_BYTES,
    MIN_LOG_MAX_BYTES,
    cleanup_legacy_rotation_backups,
    clamp_log_max_bytes,
    trim_client_log_file,
)
from client.core.store import StateStore


def _oversized_lines(count: int, threshold: int = MIN_LOG_MAX_BYTES) -> str:
    """Build ``count`` JSONL-ish lines that together exceed ``threshold`` bytes."""
    # Keep each line short but ensure total size clears the threshold.
    per = max(8, (threshold // count) + 8)
    return "".join(f"{'x' * per}-{index}\n" for index in range(count))


class ClientLogTrimTest(unittest.TestCase):
    def tearDown(self) -> None:
        StateStore.clear_registry()

    def test_clamp_log_max_bytes(self):
        self.assertEqual(clamp_log_max_bytes(None), DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(clamp_log_max_bytes("nope"), DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(clamp_log_max_bytes(1), MIN_LOG_MAX_BYTES)
        self.assertEqual(clamp_log_max_bytes(2 * 1024 * 1024), 2 * 1024 * 1024)

    def test_trim_drops_floor_half_when_over_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.log"
            path.write_text(_oversized_lines(5), encoding="utf-8")
            self.assertGreaterEqual(path.stat().st_size, MIN_LOG_MAX_BYTES)
            dropped = trim_client_log_file(path, max_bytes=MIN_LOG_MAX_BYTES)
            self.assertEqual(dropped, 2)  # floor(5/2)
            kept = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(kept), 3)
            self.assertTrue(kept[0].endswith("-2"))
            self.assertTrue(kept[-1].endswith("-4"))

    def test_trim_even_line_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.log"
            path.write_text(_oversized_lines(4), encoding="utf-8")
            dropped = trim_client_log_file(path, max_bytes=MIN_LOG_MAX_BYTES)
            self.assertEqual(dropped, 2)
            kept = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(kept), 2)
            self.assertTrue(kept[0].endswith("-2"))
            self.assertTrue(kept[1].endswith("-3"))

    def test_trim_noop_when_under_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.log"
            path.write_text("keep\n", encoding="utf-8")
            dropped = trim_client_log_file(path, max_bytes=DEFAULT_LOG_MAX_BYTES)
            self.assertEqual(dropped, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep\n")

    def test_trim_single_oversized_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.log"
            path.write_text(("y" * (MIN_LOG_MAX_BYTES + 10)) + "\n", encoding="utf-8")
            dropped = trim_client_log_file(path, max_bytes=MIN_LOG_MAX_BYTES)
            # floor(1/2)=0 → clear the sole oversized line so size can recover.
            self.assertEqual(dropped, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_cleanup_legacy_rotation_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client.log").write_text("live\n", encoding="utf-8")
            (root / "client.log.1").write_text("old1\n", encoding="utf-8")
            (root / "client.log.2").write_text("old2\n", encoding="utf-8")
            (root / "client.log.bak").write_text("keep\n", encoding="utf-8")
            removed = cleanup_legacy_rotation_backups(root)
            self.assertEqual(removed, 2)
            self.assertTrue((root / "client.log").is_file())
            self.assertTrue((root / "client.log.bak").is_file())
            self.assertFalse((root / "client.log.1").exists())
            self.assertFalse((root / "client.log.2").exists())

    def test_state_store_log_max_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            self.assertEqual(store.log_max_bytes(), DEFAULT_LOG_MAX_BYTES)
            self.assertEqual(store.set_log_max_bytes(1024 * 1024), 1024 * 1024)
            reloaded = StateStore(path)
            self.assertEqual(reloaded.log_max_bytes(), 1024 * 1024)
            self.assertEqual(reloaded.set_log_max_bytes(1), MIN_LOG_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
