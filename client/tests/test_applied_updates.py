"""Regression: applied update digests persist across restart (force loop guard)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from client.core.applied_updates import (
    is_update_applied,
    load_applied_digests,
    mark_update_applied,
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


if __name__ == "__main__":
    unittest.main()
