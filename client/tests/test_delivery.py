import unittest
from types import SimpleNamespace

from client.core.delivery import ConfigSnapshot, MessageDeduplicator, PriorityQueue, ReconnectBackoff


class DeliveryTest(unittest.TestCase):
    def test_deduplication_is_process_local_and_server_scoped(self):
        seen = MessageDeduplicator()
        self.assertTrue(seen.observe(b"server-a", "id"))
        self.assertFalse(seen.observe(b"server-a", "id"))
        self.assertTrue(seen.observe(b"server-b", "id"))

    def test_priority_then_fifo_and_withdrawal(self):
        pending = PriorityQueue()
        pending.put(SimpleNamespace(priority=1024, queue_seq=1, message_id="first"))
        pending.put(SimpleNamespace(priority=2, queue_seq=3, message_id="urgent"))
        pending.put(SimpleNamespace(priority=1024, queue_seq=2, message_id="second"))
        pending.withdraw("second")
        self.assertEqual(pending.get().message_id, "urgent")
        self.assertEqual(pending.get().message_id, "first")
        self.assertIsNone(pending.get())

    def test_config_snapshot_converts_go_durations(self):
        snapshot = ConfigSnapshot.from_wire({
            "config_id": "1791000000000.123",
            "issued_at": 1791000000000,
            "heartbeat_interval": 15_000_000_000,
            "heartbeat_timeout": 45_000_000_000,
            "message_ttl": 86_400_000_000_000,
            "max_speech_depth": 8,
            "max_repeat_expansion": 100,
        })
        self.assertEqual(snapshot.heartbeat_interval, 15)
        self.assertEqual(snapshot.message_ttl, 86400)
        with self.assertRaises(ValueError):
            ConfigSnapshot.from_wire({"config_id": "bad"})

    def test_backoff_schedule(self):
        backoff = ReconnectBackoff()
        self.assertEqual([backoff.next_delay() for _ in range(12)], [1, 1, 1, 2, 4, 8, 16, 32, 64, 128, 300, 300])
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 1)


if __name__ == "__main__":
    unittest.main()
