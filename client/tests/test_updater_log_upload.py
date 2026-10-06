#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from client.core.updater_log_upload import (
    UPDATER_LOGGER,
    collect_pending_updater_logs,
    queue_pending_updater_logs,
    read_offset,
    write_offset,
)


class UpdaterLogUploadTests(unittest.TestCase):
    def test_collect_json_and_legacy_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            log = data_dir / "updater.log"
            lines = [
                json.dumps(
                    {
                        "time": "2026-10-06T12:00:00+08:00",
                        "level": "INFO",
                        "logger": UPDATER_LOGGER,
                        "msg": "updater started identity=mkcb.updater",
                        "source": "updater",
                    },
                    ensure_ascii=False,
                ),
                "2026-10-06 12:00:01.000 INFO mkcb.updater: legacy plain line",
            ]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            entries, offset = collect_pending_updater_logs(data_dir)
            self.assertEqual(len(entries), 2)
            self.assertGreater(offset, 0)
            first = json.loads(entries[0])
            self.assertEqual(first["logger"], UPDATER_LOGGER)
            self.assertIn("identity=mkcb.updater", first["msg"])
            second = json.loads(entries[1])
            self.assertEqual(second["logger"], UPDATER_LOGGER)
            self.assertIn("legacy plain line", second["msg"])

    def test_offset_advances_only_after_full_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            log = data_dir / "updater.log"
            payload = json.dumps(
                {"time": "t", "level": "INFO", "logger": UPDATER_LOGGER, "msg": "ok", "source": "updater"},
                ensure_ascii=False,
            )
            log.write_text(payload + "\n", encoding="utf-8")
            queued: list[str] = []
            n = queue_pending_updater_logs(data_dir, queued.append)
            self.assertEqual(n, 1)
            self.assertEqual(read_offset(data_dir), log.stat().st_size)
            # Second pass should be empty.
            n2 = queue_pending_updater_logs(data_dir, queued.append)
            self.assertEqual(n2, 0)
            self.assertEqual(len(queued), 1)

    def test_truncated_log_resets_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            log = data_dir / "updater.log"
            log.write_text('{"msg":"a","logger":"mkcb.updater","level":"INFO"}\n', encoding="utf-8")
            write_offset(data_dir, 99999)
            entries, offset = collect_pending_updater_logs(data_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(offset, log.stat().st_size)


if __name__ == "__main__":
    unittest.main()
