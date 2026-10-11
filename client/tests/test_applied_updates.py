"""Regression: applied update digests persist across restart (force loop guard)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from client.core.applied_updates import (
    is_update_applied,
    load_applied_digests,
    mark_update_applied,
    mark_update_reported,
    pending_update_reports,
)
from client.core.store import StateStore


class AppliedUpdatesTests(unittest.TestCase):
    def tearDown(self) -> None:
        StateStore.clear_registry()

    def test_mark_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            digest = "a" * 64
            self.assertFalse(is_update_applied(data_dir, digest))
            mark_update_applied(data_dir, digest)
            self.assertTrue(is_update_applied(data_dir, digest))
            self.assertEqual(load_applied_digests(data_dir), {digest})
            StateStore.clear_registry()
            again = load_applied_digests(data_dir)
            self.assertIn(digest, again)
            # Persisted under state.json applied_updates key.
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["applied_updates"]["digests"], [digest])
            self.assertFalse((data_dir / "applied_updates.json").exists())

    def test_rejects_invalid_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            mark_update_applied(data_dir, "not-a-digest")
            self.assertEqual(load_applied_digests(data_dir), set())

    def test_pending_report_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            digest = "b" * 64
            mark_update_applied(data_dir, digest)
            self.assertEqual(pending_update_reports(data_dir), [digest])
            mark_update_reported(data_dir, digest)
            self.assertEqual(pending_update_reports(data_dir), [])
            self.assertTrue(is_update_applied(data_dir, digest))

    def test_legacy_list_format_migrates_into_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            digest = "c" * 64
            path = data_dir / "applied_updates.json"
            path.write_text(f'["{digest}"]', encoding="utf-8")
            self.assertEqual(load_applied_digests(data_dir), {digest})
            self.assertEqual(pending_update_reports(data_dir), [])
            self.assertFalse(path.exists())
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["applied_updates"]["digests"], [digest])

    def test_legacy_sha256_key_migrates_to_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            digest = "d" * 64
            path = data_dir / "applied_updates.json"
            path.write_text(
                json.dumps({"sha256": [digest], "pending_report": [digest]}, separators=(",", ":")),
                encoding="utf-8",
            )
            self.assertEqual(load_applied_digests(data_dir), {digest})
            self.assertEqual(pending_update_reports(data_dir), [digest])
            mark_update_reported(data_dir, digest)
            self.assertFalse(path.exists())
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            self.assertIn("digests", state["applied_updates"])
            self.assertEqual(state["applied_updates"]["digests"], [digest])
            self.assertEqual(state["applied_updates"]["pending_report"], [])


if __name__ == "__main__":
    unittest.main()
