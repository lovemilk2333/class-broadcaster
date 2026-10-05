"""Offline TTS primitives used by the client playback worker.

SAPI is preferred on Windows 10. Piper is an optional bundled executable and
model used when SAPI or its Chinese voice is unavailable. No network calls are
made by this module.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, List, Optional, Protocol, Union
from pydantic import BaseModel
from loguru import logger


LOGGER = logger.bind(name="mkcb.client.tts")


class SpeechPart(BaseModel):
    kind: str
    text: str = ""
    duration_ms: int = 0

    class Config:
        frozen = True

    def __init__(self, kind: str = "", text: str = "", duration_ms: int = 0, **data: object) -> None:
        data.update(kind=kind, text=text, duration_ms=duration_ms)
        super().__init__(**data)


def compile_speech(nodes: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, max_repeat: int = 100) -> list[SpeechPart]:
    """Expand the safe repeat/pause node format into playback parts."""
    if max_repeat < 1:
        raise ValueError("max_repeat must be positive")
    output: list[SpeechPart] = []

    def visit(items: list[dict[str, Any]] | tuple[dict[str, Any], ...], depth: int) -> None:
        if depth > 64:
            raise ValueError("speech nesting is too deep")
        for node in items:
            if not isinstance(node, dict):
                raise ValueError("speech node must be an object")
            kind = str(node.get("type", "text"))
            if kind == "text":
                value = str(node.get("value", ""))
                if value:
                    output.append(SpeechPart("text", text=value))
            elif kind == "pause":
                duration = int(node.get("duration_ms", 0))
                if duration < 0 or duration > 60_000:
                    raise ValueError("pause must be between 0 and 60000ms")
                output.append(SpeechPart("pause", duration_ms=duration))
            elif kind == "repeat":
                count = int(node.get("count", 1))
                if count < 1 or count > max_repeat:
                    raise ValueError("repeat count exceeds limit")
                children = node.get("children", [])
                if not isinstance(children, list):
                    raise ValueError("repeat children must be a list")
                for _ in range(count):
                    visit(children, depth + 1)
            else:
                raise ValueError(f"unsupported speech node: {kind}")

    visit(nodes, 0)
    return output


def estimate_duration_ms(parts: list[SpeechPart]) -> int:
    text_length = sum(len(part.text) for part in parts if part.kind == "text")
    # A conservative 240 Chinese characters/minute estimate keeps the window
    # visible for at least max(total / 4, 1) seconds while speech is running.
    return max(1000, int(text_length * 250) + sum(part.duration_ms for part in parts))


class Engine(Protocol):
    name: str

    def available(self) -> bool: ...

    def speak(self, text: str, stop: threading.Event) -> None: ...


def _looks_chinese_voice(name: str, language: str = "") -> bool:
    lowered = f"{name} {language}".lower()
    markers = (
        "zh-cn",
        "zh_cn",
        "zh-hans",
        "chinese",
        "huihui",
        "yaoyao",
        "kangkang",
        "lili",
        "xiaoxiao",
        "xiaoyi",
        "yunxi",
        "yunyang",
        "0x804",
        "0804",
        "1004",
    )
    return any(marker in lowered for marker in markers)


class SAPIEngine:
    """Windows SAPI5 TTS.

    Prefer pywin32 COM when present; otherwise drive SAPI via PowerShell so the
    embedded runtime still works without a separate pywin32 install.
    """

    name = "sapi5-zh"

    @staticmethod
    def _select_chinese_voice(voice: Any) -> Any:
        voices = voice.GetVoices()
        for index in range(voices.Count):
            token = voices.Item(index)
            try:
                languages = str(token.GetAttribute("Language") or "").split(";")
                if any(int(language, 16) in (0x0804, 0x1004) for language in languages if language):
                    return token
            except (ValueError, TypeError, AttributeError):
                pass
            try:
                desc = str(token.GetDescription() or "")
            except Exception:
                desc = ""
            if _looks_chinese_voice(desc):
                return token
        # Fall back to the system default voice rather than failing hard when a
        # Chinese pack is missing; classrooms still get an audible cue.
        try:
            if voices.Count > 0:
                return voices.Item(0)
        except Exception:
            return None
        return None

    def _com_available(self) -> bool:
        try:
            import win32com.client  # type: ignore[import-not-found]
        except ImportError:
            return False
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            return self._select_chinese_voice(voice) is not None
        except Exception as exc:
            LOGGER.warning("SAPI COM probe failed error={}", exc)
            return False

    def _powershell_available(self) -> bool:
        if platform.system() != "Windows":
            return False
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return False
        # SpVoice exists on every supported Windows install; skip a slow probe.
        return True

    def available(self) -> bool:
        if platform.system() != "Windows":
            return False
        if self._com_available():
            return True
        return self._powershell_available()

    def speak(self, text: str, stop: threading.Event) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("SAPI is only available on Windows")
        if stop.is_set():
            return
        if self._com_available():
            self._speak_com(text, stop)
            return
        self._speak_powershell(text, stop)

    def _speak_com(self, text: str, stop: threading.Event) -> None:
        import win32com.client  # type: ignore[import-not-found]

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        token = self._select_chinese_voice(voice)
        if token is None:
            raise RuntimeError("SAPI voice is unavailable")
        voice.Voice = token
        # SVSFlagsAsync lets the worker stop an ongoing utterance promptly.
        voice.Speak(text, 1)
        while not stop.wait(0.05):
            status = voice.Status
            if getattr(status, "RunningState", 1) == 1:
                return
        voice.Speak("", 2)

    def _speak_powershell(self, text: str, stop: threading.Event) -> None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("PowerShell is unavailable for SAPI playback")
        # Escape for a single-quoted PowerShell string.
        safe = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$preferred = $s.GetInstalledVoices() | Where-Object { "
            "$_.VoiceInfo.Culture.Name -like 'zh*' -or $_.VoiceInfo.Name -match "
            "'Chinese|Huihui|Yaoyao|Kangkang|Lili|Xiaoxiao|Yunxi|Yunyang' }; "
            "if ($preferred) { $s.SelectVoice($preferred[0].VoiceInfo.Name) }; "
            f"$s.Speak('{safe}')"
        )
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        process = subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            while process.poll() is None:
                if stop.wait(0.05):
                    process.terminate()
                    break
            if process.returncode not in (0, None) and not stop.is_set():
                err = b""
                if process.stderr is not None:
                    err = process.stderr.read() or b""
                detail = err.decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or "SAPI PowerShell playback failed")
        finally:
            if process.poll() is None:
                process.kill()


class PiperEngine:
    name = "piper"

    def __init__(self, executable: Optional[str] = None, model: Optional[str] = None) -> None:
        self.executable = executable or shutil.which("piper")
        self.model = model

    def available(self) -> bool:
        return bool(self.executable and self.model and Path(self.model).is_file())

    def speak(self, text: str, stop: threading.Event) -> None:
        if not self.available():
            raise RuntimeError("piper is not configured")
        process = subprocess.Popen(
            [self.executable or "piper", "--model", self.model or ""],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            if process.stdin is not None:
                process.stdin.write((text + "\n").encode("utf-8"))
                process.stdin.close()
            while process.poll() is None:
                if stop.wait(0.05):
                    process.terminate()
                    break
            if process.returncode not in (0, None) and not stop.is_set():
                raise RuntimeError("piper playback failed")
        finally:
            if process.poll() is None:
                process.kill()


class EnginePreference:
    """Remember the last working engine while retaining automatic fallback."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def load(self) -> Optional[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return str(value["engine"]) if isinstance(value, dict) and value.get("engine") else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, engine: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"engine": engine}, separators=(",", ":")), encoding="utf-8")


def choose_engine(engines: List[Engine], preference: Optional[EnginePreference] = None) -> Engine:
    preferred = preference.load() if preference else None
    ordered = sorted(engines, key=lambda engine: 0 if engine.name == preferred else 1)
    for engine in ordered:
        if engine.available():
            if preference:
                preference.save(engine.name)
            return engine
    raise RuntimeError("no offline TTS engine is available")


def speak_parts(
    parts: List[SpeechPart],
    engine: Engine,
    stop: threading.Event,
    on_text: Any = None,
) -> None:
    """Play compiled parts, allowing a high-priority message to interrupt."""
    for part in parts:
        if stop.is_set():
            return
        if part.kind == "pause":
            stop.wait(part.duration_ms / 1000)
        elif part.text:
            if on_text is not None:
                on_text(part.text)
            engine.speak(part.text, stop)


def speak_with_fallback(
    parts: List[SpeechPart],
    engines: List[Engine],
    stop: threading.Event,
    preference: Optional[EnginePreference] = None,
    on_text: Any = None,
) -> str:
    """Try the remembered engine first, then fall back from the start."""
    preferred = preference.load() if preference else None
    ordered = sorted(engines, key=lambda engine: 0 if engine.name == preferred else 1)
    last_error: Optional[Exception] = None
    unavailable: list[str] = []
    for engine in ordered:
        try:
            available = engine.available()
        except Exception as exc:
            last_error = exc
            LOGGER.warning("TTS engine probe failed engine={} error={}", engine.name, exc)
            continue
        if not available:
            unavailable.append(engine.name)
            LOGGER.info("TTS engine unavailable engine={}", engine.name)
            continue
        LOGGER.info("TTS engine selected engine={}", engine.name)
        try:
            speak_parts(parts, engine, stop, on_text)
            if preference:
                preference.save(engine.name)
            return engine.name
        except Exception as exc:  # runtime engine failures trigger fallback
            last_error = exc
            LOGGER.warning("TTS playback failed engine={} error={}", engine.name, exc)
            if stop.is_set():
                return engine.name
    if last_error is None:
        detail = ", ".join(unavailable) if unavailable else "no engines configured"
        raise RuntimeError("no offline TTS engine is available; checked={}".format(detail))
    raise RuntimeError("offline TTS playback failed: {}".format(last_error)) from last_error
