"""Regression: applied update digests persist across restart (force loop guard)."""

from __future__ import annotations

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


class AppliedUpdatesTests(unittest.TestCase):
    def test_mark_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            digest = "a" * 64
            self.assertFalse(is_update_applied(data_dir, digest))
            mark_update_applied(data_dir, digest)
            self.assertTrue(is_update_applied(data_dir, digest))
            self.assertEqual(load_applied_digests(data_dir), {digest})
            # Reload path used by ClientWindow seed.
            again = load_applied_digests(data_dir)
            self.assertIn(digest, again)

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
            # Applied digest remains after the server ack.
            self.assertTrue(is_update_applied(data_dir, digest))

    def test_legacy_list_format_has_empty_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            digest = "c" * 64
            path = data_dir / "applied_updates.json"
            path.write_text(f'["{digest}"]', encoding="utf-8")
            self.assertEqual(load_applied_digests(data_dir), {digest})
            self.assertEqual(pending_update_reports(data_dir), [])

    def test_legacy_sha256_key_migrates_to_digests(self) -> None:
        import json

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
            # Rewrite uses digests, not sha256.
            mark_update_reported(data_dir, digest)
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("digests", rewritten)
            self.assertNotIn("sha256", rewritten)
            self.assertEqual(rewritten["digests"], [digest])
            self.assertEqual(rewritten["pending_report"], [])


if __name__ == "__main__":
    unittest.main()
