import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from client.core.store import StateStore
from client.core.delivery import display_duration_ms
from client.core.store import StateStore
from client.core.tts import (
    EnginePreference,
    SpeechPart,
    _WAV_STDOUT_MAGIC,
    _synthesize_wav_bytes,
    _wav_duration_seconds,
    compile_speech,
    estimate_duration_ms,
    play_cached_speech_plan,
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

    def test_in_memory_wav_helpers(self):
        import io
        import struct
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(22050)
            handle.writeframes(b"\x00\x00" * 2205)  # 0.1s silence
        wav = buffer.getvalue()
        self.assertGreaterEqual(wav[:4], b"RIFF")
        self.assertAlmostEqual(_wav_duration_seconds(wav), 0.1, delta=0.02)

        def _write(wav_file):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 1600)

        produced = _synthesize_wav_bytes(_write)
        self.assertTrue(produced.startswith(b"RIFF"))
        self.assertAlmostEqual(_wav_duration_seconds(produced), 0.1, delta=0.02)
        # Subprocess framing used by Windows classic piper-tts path.
        framed = _WAV_STDOUT_MAGIC + struct.pack(">I", len(wav)) + wav
        self.assertEqual(framed[8:12], struct.pack(">I", len(wav)))
        self.assertEqual(framed[12:], wav)

    def test_play_cached_speech_plan_honours_stop_and_pauses(self):
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(22050)
            handle.writeframes(b"\x00\x00" * 220)  # ~10ms
        wav = buffer.getvalue()
        stop = threading.Event()
        stop.set()
        # Already cancelled: must return without raising.
        play_cached_speech_plan([("pause", 50), ("wav", wav)], stop)
        self.assertTrue(stop.is_set())
        # Empty / truncated wav entries are skipped.
        stop.clear()
        play_cached_speech_plan([("wav", b"nope"), ("pause", 1)], stop)

    def test_synthesize_subprocess_pumps_large_stdout(self):
        """Parent must read stdout while the child writes or large WAVs deadlock.

        Regression for classroom hang: poll()-only wait left the child blocked on
        a full pipe (~64KiB) with no audio until withdraw.
        """
        import struct
        import subprocess
        import sys

        from client.core.tts import _WAV_STDOUT_MAGIC, _windows_hidden_subprocess_kwargs

        payload_size = 200 * 1024
        # Child builds a large framed payload itself (avoid ARG_MAX) and writes
        # slowly so a non-pumping parent would stall on a full pipe.
        magic_repr = repr(_WAV_STDOUT_MAGIC)
        child = (
            "import struct, sys, time\n"
            f"magic = {magic_repr}\n"
            f"n = {payload_size}\n"
            "payload = b'R' * n\n"
            "header = magic + struct.pack('>I', n)\n"
            "out = sys.stdout.buffer\n"
            "out.write(header); out.flush()\n"
            "for i in range(0, n, 4096):\n"
            "  out.write(payload[i:i+4096]); out.flush(); time.sleep(0.001)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", child],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windows_hidden_subprocess_kwargs(),
        )
        chunks: list[bytes] = []

        def _pump() -> None:
            assert process.stdout is not None
            while True:
                block = process.stdout.read(65536)
                if not block:
                    break
                chunks.append(block)

        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        try:
            process.wait(timeout=60)
            pump.join(timeout=5)
            self.assertEqual(process.returncode, 0)
            got = b"".join(chunks)
            self.assertTrue(got.startswith(_WAV_STDOUT_MAGIC))
            size = int.from_bytes(got[8:12], "big")
            self.assertEqual(size, payload_size)
            self.assertEqual(got[12 : 12 + size], b"R" * payload_size)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_tts_cache_entry_ready_gate(self):
        from client.app import TtsCacheEntry

        entry = TtsCacheEntry("mid", [SpeechPart("text", text="hi")], 0.75)
        self.assertFalse(entry.ready.is_set())
        entry.plan = [("pause", 10)]
        entry.ready.set()
        self.assertTrue(entry.ready.wait(0.01))

    def test_engine_preference_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            preference = EnginePreference(Path(directory) / "tts.json")
            self.assertIsNone(preference.load())
            preference.save("piper")
            self.assertEqual(preference.load(), "piper")
            # saved_at must be present for TTL tracking.
            raw = json.loads((Path(directory) / "tts.json").read_text(encoding="utf-8"))
            self.assertIn("saved_at", raw)
            self.assertIsInstance(raw["saved_at"], (int, float))

    def test_engine_preference_expires_after_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tts-engine.json"
            preference = EnginePreference(path, ttl_seconds=24 * 60 * 60)
            preference.save("piper")
            self.assertEqual(preference.load(), "piper")
            # Age the stamp past 24h.
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["saved_at"] = time.time() - (24 * 60 * 60 + 1)
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertIsNone(preference.load())
            # Legacy file without saved_at also expires (force re-probe).
            path.write_text(json.dumps({"engine": "sapi5-zh"}), encoding="utf-8")
            self.assertIsNone(preference.load())

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

    def test_sapi_preference_does_not_block_piper(self):
        """Saved sapi5-zh must not permanently pin over a working Piper engine."""

        class FakePiper:
            name = "piper"

            def available(self):
                return True

            def speak(self, text, stop, speech_rate=1.0):
                self.spoken = text

        class FakeSapi:
            name = "sapi5-zh"

            def available(self):
                return True

            def speak(self, text, stop, speech_rate=1.0):
                raise AssertionError("SAPI must not win when Piper is available")

        with tempfile.TemporaryDirectory() as directory:
            preference = EnginePreference(Path(directory) / "tts-engine.json")
            preference.save("sapi5-zh")
            piper = FakePiper()
            chosen = speak_with_fallback(
                compile_speech([{"type": "text", "value": "你好"}]),
                [piper, FakeSapi()],
                threading.Event(),
                preference,
            )
            self.assertEqual(chosen, "piper")
            self.assertEqual(preference.load(), "piper")
            self.assertEqual(piper.spoken, "你好")

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
