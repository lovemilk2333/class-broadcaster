import tempfile
import threading
import unittest
from pathlib import Path

from client.core.store import StateStore
from client.core.tts import EnginePreference, compile_speech, estimate_duration_ms, speak_with_fallback


class StoreAndTtsTest(unittest.TestCase):
    def test_state_contains_no_delivery_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"version":1,"server":null,"config_id":null,"messages":{"old":1}}')
            store = StateStore(path)
            self.assertNotIn("messages", store.snapshot())

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


if __name__ == "__main__":
    unittest.main()
