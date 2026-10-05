import tempfile
import threading
import unittest
from pathlib import Path

from client.core.store import StateStore
from client.core.delivery import display_duration_ms
from client.core.store import StateStore
from client.core.tts import EnginePreference, compile_speech, estimate_duration_ms, speak_with_fallback
from client.app import SingleInstanceLock


class StoreAndTtsTest(unittest.TestCase):
    def test_display_duration_uses_ratio_and_caps_at_two_minutes(self):
        self.assertEqual(display_duration_ms("ab", 0.4), 1000)
        self.assertEqual(display_duration_ms("中", 1.0), 1500)
        self.assertEqual(display_duration_ms("x" * 10000, 1.0), 120000)
        self.assertEqual(display_duration_ms("x", 0), 0)

    def test_state_contains_no_delivery_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"version":1,"server":null,"config_id":null,"messages":{"old":1}}')
            store = StateStore(path)
            self.assertNotIn("messages", store.snapshot())

    def test_missing_state_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            self.assertEqual(store.servers(), [])
            self.assertIsNone(store.server())

    def test_legacy_admin_suspension_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"version":1,"server":null,"servers":[],"suspended_servers":[{"host":"127.0.0.1","tcp_port":39002}]}')
            self.assertNotIn("suspended_servers", StateStore(path).snapshot())

    def test_multiple_servers_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            first = b"\x01" * 32
            second = b"\x02" * 32
            store.set_server(
                host="10.0.0.1",
                tcp_port=39002,
                http_port=39003,
                fingerprint=first,
                make_current=False,
                auto_connect=True,
            )
            store.set_server(
                host="10.0.0.2",
                tcp_port=39002,
                http_port=39003,
                fingerprint=second,
                make_current=False,
                auto_connect=True,
            )
            hosts = {(item["host"], item["tcp_port"]) for item in store.servers()}
            self.assertEqual(hosts, {("10.0.0.1", 39002), ("10.0.0.2", 39002)})
            # Re-saving the same endpoint with a zero fingerprint must not wipe trust.
            store.set_server(
                host="10.0.0.1",
                tcp_port=39002,
                http_port=39003,
                fingerprint=b"\x00" * 32,
                make_current=False,
            )
            restored = next(item for item in store.servers() if item["host"] == "10.0.0.1")
            import base64
            self.assertEqual(base64.b64decode(restored["fingerprint"]), first)
            self.assertTrue(restored.get("auto_connect"))
            self.assertEqual(len(store.servers()), 2)
            store.set_server_config_id(host="10.0.0.1", tcp_port=39002, config_id="1700000000000.1")
            store.set_server_config_id(host="10.0.0.2", tcp_port=39002, config_id="1700000000000.2")
            by_host = {item["host"]: item for item in store.servers()}
            self.assertEqual(by_host["10.0.0.1"]["config_id"], "1700000000000.1")
            self.assertEqual(by_host["10.0.0.2"]["config_id"], "1700000000000.2")

    def test_speech_repeat_and_pause(self):
        parts = compile_speech([
            {"type": "repeat", "count": 2, "children": [{"type": "text", "value": "你好"}]},
            {"type": "pause", "duration_ms": 500},
        ])
        self.assertEqual([part.kind for part in parts], ["text", "text", "pause"])
        self.assertGreaterEqual(estimate_duration_ms(parts), 1000)

    def test_engine_preference_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            preference = EnginePreference(Path(directory) / "tts.json")
            self.assertIsNone(preference.load())
            preference.save("piper")
            self.assertEqual(preference.load(), "piper")

    def test_single_instance_lock_is_exclusive_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "client.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            self.assertTrue(path.exists())

    def test_unavailable_engines_report_which_engines_were_checked(self):
        class UnavailableEngine:
            name = "test-engine"

            def available(self):
                return False

            def speak(self, text, stop):
                raise AssertionError("unavailable engine must not be started")

        with self.assertRaisesRegex(RuntimeError, "test-engine"):
            speak_with_fallback(
                compile_speech([{"type": "text", "value": "hello"}]),
                [UnavailableEngine()],
                threading.Event(),
            )

    def test_autostart_default_flag_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            self.assertFalse(store.autostart_default_applied())
            store.set_autostart_default_applied(True)
            reloaded = StateStore(Path(directory) / "state.json")
            self.assertTrue(reloaded.autostart_default_applied())


if __name__ == "__main__":
    unittest.main()
