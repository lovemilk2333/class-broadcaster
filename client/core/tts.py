"""Offline TTS primitives used by the client playback worker.

Platform split:
- Windows (win32): prefer ``piper-plus`` with the multilingual Tsukuyomi-chan
  ONNX voice under ``client/tts/`` / ``Lib/tts/``. CN/EN segments are synthesized
  separately with ``language_id`` (ja=0, en=1, zh=2).
- Linux: keep ``piper-tts`` with separate zh_CN-huayan-medium + en_US-lessac-medium
  models (same layout).

Windows SAPI remains the fallback when Piper models/library are unavailable.
No CLI binary fallback. No network calls are made at playback time.

``speech_rate`` is a speed multiplier (1.0 = normal, higher = faster). Piper uses
``length_scale = 1.0 / speech_rate``; SAPI maps the multiplier onto Rate (-10..10).
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, List, Optional, Protocol, Union

from loguru import logger
from pydantic import BaseModel

from .tokenize import split_language_segments

LOGGER = logger.bind(name="mkcb.client.tts")

# Default voice filenames expected under the TTS model directory.
ZH_VOICE_NAME = "zh_CN-huayan-medium"
EN_VOICE_NAME = "en_US-lessac-medium"
# piper-plus multilingual model (Windows). HF filenames from ayousanz/piper-plus-tsukuyomi-chan.
TSUKUYOMI_ONNX_NAME = "tsukuyomi-chan-6lang-fp16.onnx"
TSUKUYOMI_CONFIG_NAME = "config.json"

# piper-plus language_id: ja=0, en=1, zh=2, es=3, fr=4, pt=5
PIPER_PLUS_LANG_ID = {"ja": 0, "en": 1, "zh": 2}

DEFAULT_SPEECH_RATE = 0.75
MIN_SPEECH_RATE = 0.25
MAX_SPEECH_RATE = 2.0

# Punctuation kept for natural TTS pacing; everything else symbolic is dropped.
# Includes ASCII and common CJK sentence marks / quotes. "/" kept per product choice.
_TTS_KEEP_PUNCT = frozenset(
    ".,?!;:\"'/"
    "。，？！；："
    "“”‘’„‟‹›«»「」『』〝〞＂＇"
)
# Letters / digits / CJK ideographs & related blocks spoken as-is; whitespace kept.
_TTS_KEEP_CHAR_RE = re.compile(
    r"[\sA-Za-z0-9"
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK
    r"\u3040-\u30ff"  # Hiragana / Katakana (harmless if present)
    r"]"
)


def sanitize_speech_text(text: str) -> str:
    """Strip symbols TTS should not read (``<=``, ``*``, brackets, …).

    Keeps letters, digits, CJK, whitespace, and ``. , ? ! : ; " ' /``
    (plus common Chinese equivalents). Collapses leftover whitespace.

    Only the speech / ``compile_speech`` path uses this. The on-screen popup
    always shows the original ``message.content`` (including ``=>`` / ``<=``).
    """
    raw = str(text or "")
    if not raw:
        return ""
    kept: list[str] = []
    for ch in raw:
        if ch in _TTS_KEEP_PUNCT or _TTS_KEEP_CHAR_RE.fullmatch(ch):
            kept.append(ch)
        else:
            # Replace dropped symbols with a space so "a<=b" → "a b", not "ab".
            if kept and not kept[-1].isspace():
                kept.append(" ")
    cleaned = "".join(kept)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


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
                value = sanitize_speech_text(str(node.get("value", "")))
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


def normalize_speech_rate(speech_rate: float | None, *, default: float = DEFAULT_SPEECH_RATE) -> float:
    """Clamp a TTS speed multiplier into the supported range."""
    try:
        value = float(speech_rate) if speech_rate is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)
    if value <= 0:
        value = float(default)
    return max(MIN_SPEECH_RATE, min(MAX_SPEECH_RATE, value))


def length_scale_for_rate(speech_rate: float) -> float:
    """Piper length_scale: larger = slower. Invert the public speech_rate multiplier."""
    rate = normalize_speech_rate(speech_rate, default=1.0)
    return 1.0 / rate


def sapi_rate_for_speech_rate(speech_rate: float) -> int:
    """Map speech_rate (0.25..2.0) onto SAPI Rate (-10..10), with 1.0 → 0."""
    rate = normalize_speech_rate(speech_rate, default=1.0)
    # Linear around 1.0: 0.5 → -5, 1.5 → +5, clamped.
    mapped = int(round((rate - 1.0) * 10.0))
    return max(-10, min(10, mapped))


def estimate_duration_ms(parts: list[SpeechPart], speech_rate: float = 1.0) -> int:
    """Estimate spoken duration; slower speech_rate yields a longer estimate.

    Pauses are kept as-is (not scaled). Base text estimate uses ~240 Chinese
    characters/minute (250 ms/char) at speech_rate=1.0.
    """
    text_length = sum(len(part.text) for part in parts if part.kind == "text")
    pause_ms = sum(part.duration_ms for part in parts)
    rate = normalize_speech_rate(speech_rate, default=1.0)
    text_ms = int(text_length * 250 / rate) if text_length else 0
    return max(1000, text_ms + pause_ms)


class Engine(Protocol):
    name: str

    def available(self) -> bool: ...

    def speak(self, text: str, stop: threading.Event, speech_rate: float = 1.0) -> None: ...


def resolve_tts_model_dirs(data_dir: Optional[Path] = None) -> list[Path]:
    """Candidate directories for Piper ONNX voices (first hit wins).

    Order:
    1. ``data/tts`` next to runtime state (operator drop-in / override)
    2. Packaged ``Lib/tts`` beside the embeddable runtime
    3. Source checkout ``client/tts``
    """
    candidates: list[Path] = []
    if data_dir is not None:
        candidates.append(Path(data_dir) / "tts")
    here = Path(__file__).resolve()
    # .../Lib/site-packages is not used; packaged voices live at Lib/tts.
    # From client/core/tts.py → client/ → package root → Lib/tts
    client_root = here.parents[1]
    package_root = client_root.parent
    candidates.append(package_root / "Lib" / "tts")
    candidates.append(client_root / "tts")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def find_voice_model(stem: str, search_dirs: list[Path]) -> Optional[Path]:
    """Return ``stem.onnx`` if both the model and ``stem.onnx.json`` exist."""
    for directory in search_dirs:
        model = directory / f"{stem}.onnx"
        config = directory / f"{stem}.onnx.json"
        if model.is_file() and config.is_file():
            return model
        # Also accept nested voice folders (download layout).
        nested = directory / stem / f"{stem}.onnx"
        nested_config = directory / stem / f"{stem}.onnx.json"
        if nested.is_file() and nested_config.is_file():
            return nested
    return None


def find_tsukuyomi_model(search_dirs: list[Path]) -> Optional[tuple[Path, Path]]:
    """Locate piper-plus Tsukuyomi-chan ONNX + config under search dirs."""
    names = (
        (TSUKUYOMI_ONNX_NAME, TSUKUYOMI_CONFIG_NAME),
        # Alternate names operators / piper-plus --download-model may produce.
        ("tsukuyomi.onnx", "config.json"),
        ("tsukuyomi.onnx", "tsukuyomi.onnx.json"),
        ("ja_JP-tsukuyomi-chan-medium.onnx", "ja_JP-tsukuyomi-chan-medium.onnx.json"),
        ("ja_JP-tsukuyomi-chan-medium.onnx", "config.json"),
    )
    for directory in search_dirs:
        for onnx_name, config_name in names:
            model = directory / onnx_name
            config = directory / config_name
            if model.is_file() and config.is_file():
                return model, config
            nested = directory / "tsukuyomi" / onnx_name
            nested_config = directory / "tsukuyomi" / config_name
            if nested.is_file() and nested_config.is_file():
                return nested, nested_config
            nested2 = directory / "tsukuyomi-chan" / onnx_name
            nested2_config = directory / "tsukuyomi-chan" / config_name
            if nested2.is_file() and nested2_config.is_file():
                return nested2, nested2_config
    return None


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


def _play_wav(path: Path, stop: threading.Event) -> None:
    system = platform.system()
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    if system == "Windows":
        # PowerShell Media.SoundPlayer is available without extra deps.
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            f"$p = New-Object System.Media.SoundPlayer '{str(path).replace(chr(39), chr(39)+chr(39))}'; "
            "$p.PlaySync()"
        )
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("PowerShell unavailable for Piper WAV playback")
        cmd = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    else:
        # Linux classroom: aplay (ALSA) is the usual path; paplay as fallback.
        player = shutil.which("aplay") or shutil.which("paplay") or shutil.which("ffplay")
        if not player:
            raise RuntimeError("no WAV player (aplay/paplay/ffplay) for Piper playback")
        if Path(player).name == "ffplay":
            cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
        else:
            cmd = [player, str(path)]
    process = subprocess.Popen(
        cmd,
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
            raise RuntimeError(detail or "Piper WAV playback failed")
    finally:
        if process.poll() is None:
            process.kill()


def _unlink_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except TypeError:
        try:
            path.unlink()
        except OSError:
            pass


class SAPIEngine:
    """Windows SAPI5 TTS fallback."""

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
        return bool(powershell)

    def available(self) -> bool:
        if platform.system() != "Windows":
            return False
        if self._com_available():
            return True
        return self._powershell_available()

    def speak(self, text: str, stop: threading.Event, speech_rate: float = 1.0) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("SAPI is only available on Windows")
        if stop.is_set():
            return
        rate = sapi_rate_for_speech_rate(speech_rate)
        if self._com_available():
            self._speak_com(text, stop, rate)
            return
        self._speak_powershell(text, stop, rate)

    def _speak_com(self, text: str, stop: threading.Event, rate: int) -> None:
        import win32com.client  # type: ignore[import-not-found]

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        token = self._select_chinese_voice(voice)
        if token is None:
            raise RuntimeError("SAPI voice is unavailable")
        voice.Voice = token
        try:
            voice.Rate = int(rate)
        except Exception as exc:
            LOGGER.warning("SAPI Rate set failed rate={} error={}", rate, exc)
        voice.Speak(text, 1)
        while not stop.wait(0.05):
            status = voice.Status
            if getattr(status, "RunningState", 1) == 1:
                return
        voice.Speak("", 2)

    def _speak_powershell(self, text: str, stop: threading.Event, rate: int) -> None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise RuntimeError("PowerShell is unavailable for SAPI playback")
        safe = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$preferred = $s.GetInstalledVoices() | Where-Object { "
            "$_.VoiceInfo.Culture.Name -like 'zh*' -or $_.VoiceInfo.Name -match "
            "'Chinese|Huihui|Yaoyao|Kangkang|Lili|Xiaoxiao|Yunxi|Yunyang' }; "
            "if ($preferred) { $s.SelectVoice($preferred[0].VoiceInfo.Name) }; "
            f"$s.Rate = {int(rate)}; "
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


class PiperLibraryEngine:
    """Preferred TTS: piper-plus on Windows, piper-tts on Linux.

    Engine name stays ``piper`` so existing ``tts-engine.json`` preferences keep
    working across the Windows library swap.
    """

    name = "piper"

    def __init__(
        self,
        model_dirs: Optional[list[Path]] = None,
        *,
        zh_stem: str = ZH_VOICE_NAME,
        en_stem: str = EN_VOICE_NAME,
    ) -> None:
        self.model_dirs = list(model_dirs or [])
        self.zh_stem = zh_stem
        self.en_stem = en_stem
        self._backend: Optional[str] = None  # "piper_plus" | "piper"
        self._plus_voice: Any = None
        self._plus_paths: Optional[tuple[Path, Path]] = None
        self._zh_voice: Any = None
        self._en_voice: Any = None
        self._zh_path: Optional[Path] = None
        self._en_path: Optional[Path] = None
        self._load_error: Optional[str] = None

    def available(self) -> bool:
        system = platform.system()
        # Windows: prefer piper-plus + Tsukuyomi multilingual model.
        if system == "Windows":
            if self._probe_piper_plus():
                return True
            # Soft fallback if someone dropped classic piper models on Windows.
            if self._probe_classic_piper():
                return True
            return False
        # Linux (and other): classic piper-tts with zh/en models.
        return self._probe_classic_piper()

    def _probe_piper_plus(self) -> bool:
        try:
            import piper_plus  # noqa: F401  # type: ignore[import-not-found]
        except Exception as exc:
            self._load_error = f"piper-plus import failed: {exc}"
            return False
        found = find_tsukuyomi_model(self.model_dirs)
        if found is None:
            self._load_error = "no Tsukuyomi piper-plus model found under {}".format(
                ", ".join(str(path) for path in self.model_dirs) or "(no search dirs)"
            )
            return False
        self._plus_paths = found
        self._backend = "piper_plus"
        self._load_error = None
        return True

    def _probe_classic_piper(self) -> bool:
        try:
            import piper  # noqa: F401  # type: ignore[import-not-found]
        except Exception as exc:
            self._load_error = f"piper-tts import failed: {exc}"
            return False
        zh = find_voice_model(self.zh_stem, self.model_dirs)
        en = find_voice_model(self.en_stem, self.model_dirs)
        if zh is None and en is None:
            self._load_error = "no zh-CN/en-US Piper models found under {}".format(
                ", ".join(str(path) for path in self.model_dirs) or "(no search dirs)"
            )
            return False
        self._zh_path = zh
        self._en_path = en
        self._backend = "piper"
        self._load_error = None
        return True

    def _load_plus_voice(self) -> Any:
        if self._plus_voice is not None:
            return self._plus_voice
        if self._plus_paths is None:
            raise RuntimeError("piper-plus model path missing")
        from piper_plus import PiperVoice  # type: ignore[import-not-found]

        onnx_path, config_path = self._plus_paths
        self._plus_voice = PiperVoice.load(onnx_path, config_path=config_path)
        return self._plus_voice

    def _load_classic_voice(self, path: Path) -> Any:
        from piper import PiperVoice  # type: ignore[import-not-found]

        return PiperVoice.load(path)

    def _classic_voice_for(self, lang: str) -> Any:
        if lang == "en":
            if self._en_voice is None:
                if self._en_path is None:
                    raise RuntimeError("en-US Piper model missing")
                self._en_voice = self._load_classic_voice(self._en_path)
            return self._en_voice
        if self._zh_voice is None:
            if self._zh_path is None:
                # Fall back to English voice when only en is packaged.
                if self._en_path is not None:
                    return self._classic_voice_for("en")
                raise RuntimeError("zh-CN Piper model missing")
            self._zh_voice = self._load_classic_voice(self._zh_path)
        return self._zh_voice

    def speak(self, text: str, stop: threading.Event, speech_rate: float = 1.0) -> None:
        if stop.is_set():
            return
        if not self.available():
            raise RuntimeError(self._load_error or "piper is not configured")
        segments = split_language_segments(text)
        if not segments:
            return
        length_scale = length_scale_for_rate(speech_rate)
        if self._backend == "piper_plus":
            voice = self._load_plus_voice()
            for lang, segment in segments:
                if stop.is_set():
                    return
                language_id = PIPER_PLUS_LANG_ID.get("en" if lang == "en" else "zh", 2)
                self._speak_plus_segment(voice, segment, stop, length_scale, language_id)
            return
        for lang, segment in segments:
            if stop.is_set():
                return
            voice = self._classic_voice_for("en" if lang == "en" else "zh")
            self._speak_classic_segment(voice, segment, stop, length_scale)

    def _speak_plus_segment(
        self,
        voice: Any,
        text: str,
        stop: threading.Event,
        length_scale: float,
        language_id: int,
    ) -> None:
        if not text.strip() or stop.is_set():
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            wav_path = Path(handle.name)
        try:
            with wave.open(str(wav_path), "wb") as wav_file:
                voice.synthesize(
                    text,
                    wav_file,
                    length_scale=length_scale,
                    language_id=language_id,
                )
            if stop.is_set():
                return
            _play_wav(wav_path, stop)
        finally:
            _unlink_temp(wav_path)

    def _speak_classic_segment(
        self,
        voice: Any,
        text: str,
        stop: threading.Event,
        length_scale: float,
    ) -> None:
        if not text.strip() or stop.is_set():
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            wav_path = Path(handle.name)
        try:
            with wave.open(str(wav_path), "wb") as wav_file:
                self._classic_synthesize(voice, text, wav_file, length_scale)
            if stop.is_set():
                return
            _play_wav(wav_path, stop)
        finally:
            _unlink_temp(wav_path)

    @staticmethod
    def _classic_synthesize(voice: Any, text: str, wav_file: Any, length_scale: float) -> None:
        """Call piper-tts synthesize with length_scale across API variants."""
        # OHF-Voice / piper1-gpl: synthesize_wav(..., syn_config=SynthesisConfig(...))
        if hasattr(voice, "synthesize_wav"):
            try:
                from piper import SynthesisConfig  # type: ignore[import-not-found]

                voice.synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=length_scale))
                return
            except Exception:
                pass
            try:
                voice.synthesize_wav(text, wav_file, length_scale=length_scale)
                return
            except TypeError:
                voice.synthesize_wav(text, wav_file)
                return
        if hasattr(voice, "synthesize"):
            try:
                voice.synthesize(text, wav_file, length_scale=length_scale)
            except TypeError:
                voice.synthesize(text, wav_file)
            return
        raise RuntimeError("PiperVoice has neither synthesize_wav nor synthesize")


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
    speech_rate: float = 1.0,
) -> None:
    """Play compiled parts, allowing a high-priority message to interrupt."""
    rate = normalize_speech_rate(speech_rate, default=1.0)
    for part in parts:
        if stop.is_set():
            return
        if part.kind == "pause":
            stop.wait(part.duration_ms / 1000)
        elif part.text:
            if on_text is not None:
                on_text(part.text)
            engine.speak(part.text, stop, speech_rate=rate)


def speak_with_fallback(
    parts: List[SpeechPart],
    engines: List[Engine],
    stop: threading.Event,
    preference: Optional[EnginePreference] = None,
    on_text: Any = None,
    speech_rate: float = 1.0,
) -> str:
    """Try the remembered engine first, then fall back from the start."""
    preferred = preference.load() if preference else None
    if preferred == "piper-cli":
        # Older builds remembered the CLI engine; ignore so we use the package.
        preferred = None
    rate = normalize_speech_rate(speech_rate, default=1.0)

    # Always prefer the Python Piper library over SAPI when available,
    # unless the operator pinned another working engine.
    def sort_key(engine: Engine) -> tuple[int, int]:
        if preferred and engine.name == preferred:
            return (0, 0)
        if engine.name == "piper":
            return (1, 0)
        if engine.name == "sapi5-zh":
            return (1, 1)
        return (1, 2)

    ordered = sorted(engines, key=sort_key)
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
        LOGGER.info("TTS engine selected engine={} speech_rate={}", engine.name, rate)
        try:
            speak_parts(parts, engine, stop, on_text, speech_rate=rate)
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
