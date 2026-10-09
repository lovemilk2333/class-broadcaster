import tempfile
import threading
import unittest
from pathlib import Path

from client.core.store import StateStore
from client.core.delivery import display_duration_ms
from client.core.store import StateStore
from client.core.tts import (
    EnginePreference,
    compile_speech,
    estimate_duration_ms,
    sanitize_speech_text,
    speak_with_fallback,
)
from client.app import SingleInstanceLock


class StoreAndTtsTest(unittest.TestCase):
    def test_display_duration_uses_token_units_and_caps_at_two_minutes(self):
        # Each jieba/fallback token = 1.0 unit; floor 1000ms when ratio > 0.
        self.assertEqual(display_duration_ms("ab", 0.4), 1000)
        self.assertEqual(display_duration_ms("中", 1.0), 1000)
        self.assertEqual(display_duration_ms("你好 世界", 1.0), 2000)
        # Unspaced ASCII is a single token (not per-character).
        self.assertEqual(display_duration_ms("x" * 10000, 1.0), 1000)
        # Many space-separated tokens hit the 120s cap.
        self.assertEqual(display_duration_ms(" ".join(["词"] * 200), 1.0), 120000)
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

    def test_sanitize_speech_text_drops_symbols(self):
        self.assertEqual(sanitize_speech_text("a<=b"), "a b")
        self.assertEqual(sanitize_speech_text("score >= 60"), "score 60")
        self.assertEqual(sanitize_speech_text("你好！OK?"), "你好！OK?")
        self.assertEqual(sanitize_speech_text('say "hi", please.'), 'say "hi", please.')
        self.assertEqual(sanitize_speech_text("path/to/file"), "path/to/file")
        self.assertEqual(sanitize_speech_text("x * y + z"), "x y z")
        self.assertEqual(sanitize_speech_text("【通知】A&B"), "通知 A B")
        # compile_speech applies sanitize so engines never see "<=".
        parts = compile_speech([{"type": "text", "value": "CPU<=80%"}])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].text, "CPU 80")

    def test_speech_repeat_and_pause(self):
        parts = compile_speech([
            {"type": "repeat", "count": 2, "children": [{"type": "text", "value": "你好"}]},
            {"type": "pause", "duration_ms": 500},
        ])
        self.assertEqual([part.kind for part in parts], ["text", "text", "pause"])
        self.assertGreaterEqual(estimate_duration_ms(parts), 1000)
        # Slower speech_rate stretches the text estimate; pauses stay fixed.
        slow = estimate_duration_ms(parts, speech_rate=0.5)
        fast = estimate_duration_ms(parts, speech_rate=2.0)
        self.assertGreater(slow, fast)
        self.assertGreaterEqual(slow, 1000)

    def test_estimate_duration_scales_with_speech_rate(self):
        parts = compile_speech([{"type": "text", "value": "测" * 40}])
        base = estimate_duration_ms(parts, speech_rate=1.0)
        half = estimate_duration_ms(parts, speech_rate=0.5)
        # 40 chars * 250ms = 10000 at rate 1.0; half rate ≈ 20000.
        self.assertEqual(base, 10000)
        self.assertEqual(half, 20000)

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

            def speak(self, text, stop, speech_rate=1.0):
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

    def test_message_font_size_clamp_helpers(self):
        self.assertEqual(StateStore.DEFAULT_MESSAGE_FONT_SIZE, 38)
        self.assertEqual(StateStore.clamp_message_font_size(52), 52)
        self.assertEqual(StateStore.clamp_message_font_size(1), StateStore.MIN_MESSAGE_FONT_SIZE)
        self.assertEqual(StateStore.clamp_message_font_size(999), StateStore.MAX_MESSAGE_FONT_SIZE)
        self.assertEqual(StateStore.clamp_message_font_size("nope"), StateStore.DEFAULT_MESSAGE_FONT_SIZE)

    def test_legacy_local_font_prefs_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"version":1,"servers":[],"message_font_size":52,"message_font_family":"Noto Sans CJK SC"}',
                encoding="utf-8",
            )
            store = StateStore(path)
            snapshot = store.snapshot()
            self.assertNotIn("message_font_family", snapshot)
            self.assertNotIn("message_font_size", snapshot)


if __name__ == "__main__":
    unittest.main()
