"""Offline TTS primitives used by the client playback worker.

Both Windows and Linux use classic ``piper-tts`` with separate
``zh_CN-huayan-medium`` + ``en_US-lessac-medium`` ONNX voices under
``client/tts/`` / ``Lib/tts/``. jieba splits CN/EN segments; each segment uses
the matching voice. Windows SAPI remains the fallback. No piper-plus / Tsukuyomi.
No CLI binary fallback. No network calls are made at playback time.

``speech_rate`` is a speed multiplier (1.0 = normal, higher = faster). Piper uses
``length_scale = 1.0 / speech_rate``; SAPI maps the multiplier onto Rate (-10..10).
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
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


def _package_root_from_tts_module() -> Path:
    """``client/core/tts.py`` → package / install root (parent of ``client/``)."""
    return Path(__file__).resolve().parents[2]


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


# onnxruntime 1.3x typically needs VC++ ≥14.40. Bundled PySide6 often ships 14.29;
# those copies beside onnxruntime.dll shadow a newer System32 CRT and cause AV.
_MIN_MSVC_RUNTIME_VERSION = (14, 40, 0, 0)
_MSVC_CRT_NAMES = (
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "concrt140.dll",
    "vcomp140.dll",
)
VC_REDIST_X64_URL = "https://aka.ms/vc14/vc_redist.x64.exe"

# Set when a piper/onnxruntime probe dies with ACCESS_VIOLATION — UI may prompt once.
_pending_vc_redist_prompt_reason: Optional[str] = None


def _pe_file_version(path: Path) -> tuple[int, int, int, int]:
    """Best-effort PE FileVersion (works without Win32 APIs)."""
    try:
        data = path.read_bytes()
    except OSError:
        return (0, 0, 0, 0)
    marker = "VS_VERSION_INFO".encode("utf-16le")
    idx = data.find(marker)
    if idx < 0:
        return (0, 0, 0, 0)
    sig = b"\xbd\x04\xef\xfe"
    sig_at = data.find(sig, idx, idx + 512)
    if sig_at < 0:
        return (0, 0, 0, 0)
    try:
        import struct

        ms, ls = struct.unpack_from("<II", data, sig_at + 8)
        return ((ms >> 16) & 0xFFFF, ms & 0xFFFF, (ls >> 16) & 0xFFFF, ls & 0xFFFF)
    except Exception:
        return (0, 0, 0, 0)


def quarantine_outdated_onnxruntime_crt(install_dir: Optional[Path] = None) -> list[Path]:
    """Move outdated VC++ DLLs out of ``onnxruntime/capi`` so System32 can win.

    Safe to call repeatedly. Returns paths that were quarantined.
    """
    if platform.system() != "Windows":
        return []
    root = Path(install_dir) if install_dir is not None else None
    if root is None:
        env = (os.environ.get("MKCB_INSTALL_DIR") or "").strip()
        root = Path(env) if env else _package_root_from_tts_module()
    capi = root / "Lib" / "site-packages" / "onnxruntime" / "capi"
    if not capi.is_dir():
        return []
    quarantine = capi / "_mkcb_old_msvc"
    moved: list[Path] = []
    for name in _MSVC_CRT_NAMES:
        path = capi / name
        if not path.is_file():
            continue
        version = _pe_file_version(path)
        if version >= _MIN_MSVC_RUNTIME_VERSION:
            continue
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            dest = quarantine / name
            if dest.exists():
                dest.unlink()
            path.replace(dest)
            moved.append(dest)
            LOGGER.info(
                "quarantined outdated CRT {} v{} from onnxruntime/capi",
                name,
                ".".join(str(part) for part in version),
            )
        except OSError as exc:
            LOGGER.warning("could not quarantine {} error={}", path, exc)
    # Also catch hashed side-by-side copies like msvcp140-*.dll
    for path in capi.glob("msvcp140-*.dll"):
        version = _pe_file_version(path)
        if version >= _MIN_MSVC_RUNTIME_VERSION:
            continue
        try:
            quarantine.mkdir(parents=True, exist_ok=True)
            dest = quarantine / path.name
            if dest.exists():
                dest.unlink()
            path.replace(dest)
            moved.append(dest)
        except OSError:
            pass
    return moved


def take_pending_vc_redist_prompt() -> Optional[str]:
    """Return and clear a pending VC++ redistributable prompt reason, if any."""
    global _pending_vc_redist_prompt_reason
    reason = _pending_vc_redist_prompt_reason
    _pending_vc_redist_prompt_reason = None
    return reason


def note_vc_redist_prompt(reason: str) -> None:
    """Record that the UI should offer the VC++ x64 redistributable download."""
    global _pending_vc_redist_prompt_reason
    if platform.system() != "Windows":
        return
    _pending_vc_redist_prompt_reason = str(reason or "piper-tts native import failed")


def is_vc_redist_prompt_dismissed(data_dir: Path) -> bool:
    """True when the user already answered the one-time VC++ download prompt."""
    from .store import StateStore

    return StateStore.for_data_dir(data_dir).vc_redist_prompt_dismissed()


def mark_vc_redist_prompt_dismissed(data_dir: Path, *, opened_download: bool = False) -> None:
    """Persist that the automatic VC++ prompt must not show again on this machine."""
    from .store import StateStore

    try:
        StateStore.for_data_dir(data_dir).mark_vc_redist_prompt_dismissed(
            opened_download=opened_download
        )
    except OSError as exc:
        LOGGER.warning("could not persist VC++ prompt dismissal error={}", exc)


def diagnose_windows_tts_runtime(
    data_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Probe classic piper-tts / onnxruntime and gather CRT diagnostics (Windows only).

    Safe for a background thread: import probe runs in a child process.
    """
    result: dict[str, Any] = {
        "platform": platform.system(),
        "applicable": platform.system() == "Windows",
        "piper_ok": False,
        "onnxruntime_ok": False,
        "piper_detail": "",
        "onnxruntime_detail": "",
        "needs_vc_redist": False,
        "prompt_dismissed": False,
        "model_found": False,
        "model_path": "",
        "install_dir": "",
        "crt_files": {},
        "summary": "非 Windows：无需 VC++ 运行库检测。",
    }
    if platform.system() != "Windows":
        return result
    root_env = (os.environ.get("MKCB_INSTALL_DIR") or "").strip()
    root = Path(root_env) if root_env else _package_root_from_tts_module()
    result["install_dir"] = str(root)
    if data_dir is not None:
        result["prompt_dismissed"] = is_vc_redist_prompt_dismissed(data_dir)
    ensure_windows_native_dll_dirs(root, include_qt=False)
    capi = root / "Lib" / "site-packages" / "onnxruntime" / "capi"
    runtime = root / "runtime"
    sys32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
    pyside = root / "Lib" / "site-packages" / "PySide6"
    for label, path in (
        ("capi/onnxruntime.dll", capi / "onnxruntime.dll"),
        ("capi/msvcp140.dll", capi / "msvcp140.dll"),
        ("capi/vcruntime140.dll", capi / "vcruntime140.dll"),
        ("runtime/vcruntime140.dll", runtime / "vcruntime140.dll"),
        ("PySide6/msvcp140.dll", pyside / "msvcp140.dll"),
        ("System32/msvcp140.dll", sys32 / "msvcp140.dll"),
        ("System32/vcruntime140.dll", sys32 / "vcruntime140.dll"),
    ):
        if path.is_file():
            ver = _pe_file_version(path)
            is_crt = "msvcp" in label.lower() or "vcruntime" in label.lower()
            result["crt_files"][label] = {
                "present": True,
                "version": ".".join(str(part) for part in ver),
                "outdated": bool(is_crt and ver < _MIN_MSVC_RUNTIME_VERSION),
            }
        else:
            result["crt_files"][label] = {"present": False, "version": "", "outdated": False}

    ort_ok, ort_detail = _subprocess_import_ok("onnxruntime")
    result["onnxruntime_ok"] = ort_ok
    result["onnxruntime_detail"] = ort_detail
    piper_ok, piper_detail = _subprocess_import_ok("piper")
    result["piper_ok"] = piper_ok
    result["piper_detail"] = piper_detail
    model_dirs = resolve_tts_model_dirs(data_dir)
    zh = find_voice_model(ZH_VOICE_NAME, model_dirs)
    en = find_voice_model(EN_VOICE_NAME, model_dirs)
    found = zh or en
    if found is not None:
        result["model_found"] = True
        result["model_path"] = str(found)
    access_violation = ("ACCESS_VIOLATION" in (ort_detail or "")) or (
        "ACCESS_VIOLATION" in (piper_detail or "")
    )
    result["needs_vc_redist"] = bool(
        access_violation
        or (not ort_ok and not piper_ok and "0xC0000005" in f"{ort_detail}{piper_detail}")
    )
    if piper_ok and result["model_found"]:
        result["summary"] = "piper-tts 可用（高质量朗读就绪：huayan/lessac）。"
    elif piper_ok and not result["model_found"]:
        result["summary"] = "piper-tts 可导入，但未找到 zh_CN-huayan / en_US-lessac 语音模型。"
    elif result["needs_vc_redist"]:
        result["summary"] = (
            "piper-tts / onnxruntime 原生加载失败（疑似缺少或过旧的 "
            "VC++ 2015–2022 x64 运行库）。可安装官方运行库后重启客户端。"
        )
    elif not piper_ok:
        result["summary"] = f"piper-tts 不可用：{piper_detail or 'unknown'}"
    else:
        result["summary"] = "运行环境检测完成。"
    return result


def ensure_windows_native_dll_dirs(
    install_dir: Optional[Path] = None,
    *,
    include_qt: bool = True,
) -> list[Path]:
    """Register bundled native DLL directories for onnxruntime / Qt.

    Embeddable CPython on Windows does not always resolve DLLs next to ``.pyd``
    via PATH alone. ``os.add_dll_directory`` (Python 3.8+) is required so
    ``import onnxruntime`` / ``piper`` does not ACCESS_VIOLATION (0xC0000005).
    Safe to call repeatedly; returns the directories that were registered.

    ``include_qt=False`` for TTS probe / synthesize children: PySide6 ships
    msvcp140 14.29 which shadows a newer System32 CRT and causes AV.
    """
    if platform.system() != "Windows":
        return []
    root = Path(install_dir) if install_dir is not None else None
    if root is None:
        env = (os.environ.get("MKCB_INSTALL_DIR") or "").strip()
        root = Path(env) if env else _package_root_from_tts_module()
    # Drop outdated CRT next to onnxruntime.dll before registering search paths.
    quarantine_outdated_onnxruntime_crt(root)
    site = root / "Lib" / "site-packages"
    # Never put PySide6 ahead of System32 for onnxruntime: Qt's msvcp140 is 14.29.
    # Do not add_dll_directory(runtime/) either — embeddable ships vcruntime 14.38
    # which is still below onnxruntime 1.3x's ≥14.40 requirement.
    candidates = [
        site / "onnxruntime" / "capi",
        site / "numpy.libs",
    ]
    if include_qt:
        candidates.extend(
            [
                site / "PySide6",
                site / "shiboken6",
            ]
        )
    registered: list[Path] = []
    path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    path_lower = {part.lower() for part in path_parts}
    prepended = False
    for directory in candidates:
        if not directory.is_dir():
            continue
        key = str(directory)
        if key.lower() not in path_lower:
            path_parts.insert(0, key)
            path_lower.add(key.lower())
            prepended = True
        try:
            os.add_dll_directory(key)
        except (AttributeError, FileNotFoundError, OSError):
            pass
        registered.append(directory)
    if prepended:
        os.environ["PATH"] = os.pathsep.join(path_parts)
    return registered


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


_WAV_STDOUT_MAGIC = b"MKCBWAV1"


def _wav_duration_seconds(data: bytes) -> float:
    """Best-effort duration from a RIFF/WAVE buffer (fallback 1s)."""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 1.0
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            return max(0.05, float(frames) / float(rate))
    except Exception:
        return 1.0


def _play_wav_bytes(data: bytes, stop: threading.Event) -> None:
    """Play an in-memory WAV without writing a temp file.

    Windows: synchronous ``winsound.PlaySound(..., SND_MEMORY)`` on the caller
    thread (CPython forbids ``SND_MEMORY|SND_ASYNC``). A watcher thread issues
    ``SND_PURGE`` when ``stop`` is set so withdrawal can interrupt.
    Linux: pipe bytes to ``aplay -`` / ``paplay`` / ``ffplay -``.
    """
    if stop.is_set() or not data:
        return
    system = platform.system()
    if system == "Windows":
        import winsound

        # CPython: SND_MEMORY + SND_ASYNC raises RuntimeError ("Cannot play
        # asynchronously from memory"). Play synchronously on this worker thread;
        # purge from a side thread when stop is requested.
        finished = threading.Event()

        def _purge_on_stop() -> None:
            while not finished.is_set():
                if stop.wait(0.05):
                    try:
                        winsound.PlaySound(None, winsound.SND_PURGE)
                    except Exception:
                        pass
                    return

        watcher = threading.Thread(target=_purge_on_stop, name="mkcb-wav-stop", daemon=True)
        watcher.start()
        try:
            # Keep ``data`` alive for the duration of the blocking PlaySound call.
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except RuntimeError as exc:
            raise RuntimeError(f"winsound playback failed: {exc}") from exc
        finally:
            finished.set()
        return

    player = shutil.which("aplay") or shutil.which("paplay") or shutil.which("ffplay")
    if not player:
        raise RuntimeError("no WAV player (aplay/paplay/ffplay) for Piper playback")
    name = Path(player).name
    if name == "ffplay":
        cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"]
    elif name == "paplay":
        cmd = [player]
    else:
        cmd = [player, "-q", "-"]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **_windows_hidden_subprocess_kwargs(),
    )
    try:
        assert process.stdin is not None
        try:
            process.stdin.write(data)
            process.stdin.close()
        except BrokenPipeError:
            pass
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


def _probe_python_executable() -> Path:
    """Prefer ``python.exe`` over ``pythonw.exe`` so ``-c`` stdout is capturable.

    Always pair with :func:`_windows_hidden_subprocess_kwargs` so the console
    host does not flash a CMD window under the tray GUI.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.is_file():
            return sibling
    return exe


def _windows_hidden_subprocess_kwargs() -> dict[str, Any]:
    """Kwargs so Windows child processes do not flash a console window."""
    if platform.system() != "Windows":
        return {}
    kwargs: dict[str, Any] = {}
    # CREATE_NO_WINDOW (0x08000000) is the primary fix for python.exe / powershell.
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    # Belt-and-suspenders for hosts that still allocate a console.
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    except (AttributeError, TypeError):
        pass
    return kwargs


def _tts_child_environ() -> dict[str, str]:
    """Environment for piper/onnxruntime child processes.

    Strip PySide6 / shiboken6 / embeddable ``runtime/`` from PATH so LoadLibrary
    cannot pick Qt's msvcp140 14.29 or runtime's vcruntime 14.38 ahead of System32.
    """
    env = os.environ.copy()
    root_env = (env.get("MKCB_INSTALL_DIR") or "").strip()
    root = Path(root_env) if root_env else _package_root_from_tts_module()
    env["MKCB_INSTALL_DIR"] = str(root)
    # Optional legacy NLTK_DATA (piper-plus/g2p_en removed; harmless if present).
    nltk_data = root / "Lib" / "nltk_data"
    if nltk_data.is_dir():
        env["NLTK_DATA"] = str(nltk_data)
    site = root / "Lib" / "site-packages"
    block = {
        str(site / "PySide6").lower(),
        str(site / "shiboken6").lower(),
        str(root / "runtime").lower(),
    }
    path_parts = [
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part and part.lower() not in block
    ]
    # Prefer TTS native dirs (without Qt / outdated runtime CRT).
    for directory in (
        site / "onnxruntime" / "capi",
        site / "numpy.libs",
    ):
        if directory.is_dir():
            key = str(directory)
            if key.lower() not in {p.lower() for p in path_parts}:
                path_parts.insert(0, key)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def _format_access_violation_detail(module_name: str, env: Optional[dict[str, str]] = None) -> str:
    """Human-readable ACCESS_VIOLATION annotation with CRT path diagnostics."""
    root = Path(
        ((env or {}).get("MKCB_INSTALL_DIR") or os.environ.get("MKCB_INSTALL_DIR") or "").strip()
        or str(_package_root_from_tts_module())
    )
    capi = root / "Lib" / "site-packages" / "onnxruntime" / "capi"
    pyside = root / "Lib" / "site-packages" / "PySide6"
    sys32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
    checks = [
        ("ort.dll", (capi / "onnxruntime.dll").is_file()),
        ("sys32/msvcp140", (sys32 / "msvcp140.dll").is_file()),
        ("sys32/vcruntime140", (sys32 / "vcruntime140.dll").is_file()),
        ("capi/msvcp140", (capi / "msvcp140.dll").is_file()),
        ("PySide6/msvcp140", (pyside / "msvcp140.dll").is_file()),
    ]
    versions: list[str] = []
    for label, path in (
        ("sys32/msvcp140", sys32 / "msvcp140.dll"),
        ("sys32/vcruntime140", sys32 / "vcruntime140.dll"),
        ("PySide6/msvcp140", pyside / "msvcp140.dll"),
        ("runtime/vcruntime140", root / "runtime" / "vcruntime140.dll"),
    ):
        if path.is_file():
            ver = _pe_file_version(path)
            versions.append(f"{label}=v{'.'.join(str(p) for p in ver)}")
    hint = ", ".join(f"{name}={'yes' if ok else 'NO'}" for name, ok in checks)
    if versions:
        hint = f"{hint}; {'; '.join(versions)}"
    return (
        f"native ACCESS_VIOLATION (0xC0000005) while importing {module_name}; "
        f"check VC++ 2015-2022 x64 runtime and bundled onnxruntime DLLs ({hint})"
    )


def _windows_dll_bootstrap_source() -> str:
    """Python snippet for child ``-c`` scripts: register onnxruntime DLL dirs.

    Child interpreters inherit PATH from the tray process, but Windows LoadLibrary
    for ``.pyd`` dependencies often still needs ``os.add_dll_directory`` *inside*
    that process. Without this preamble, ``import piper`` / onnxruntime dies
    with ACCESS_VIOLATION (0xC0000005).

    Critical: do **not** ``add_dll_directory(PySide6)`` or preload ``runtime/`` CRT.
    PySide6 ships msvcp140 14.29 and embeddable runtime ships vcruntime 14.38; both
    are below onnxruntime 1.3x (≥14.40) and shadow a newer System32 CRT when listed
    ahead of it. Preload System32 CRT by absolute path first, then onnxruntime.dll.
    """
    return (
        "import ctypes, os, sys\n"
        "from pathlib import Path\n"
        "exe = Path(sys.executable).resolve()\n"
        "root = Path(os.environ.get('MKCB_INSTALL_DIR') or '')\n"
        "if not root.is_dir():\n"
        "  # runtime/python.exe → install root; bare python → parent\n"
        "  root = exe.parent.parent if exe.parent.name.lower() == 'runtime' else exe.parent\n"
        "site = root / 'Lib' / 'site-packages'\n"
        "capi = site / 'onnxruntime' / 'capi'\n"
        "# Quarantine outdated CRT next to onnxruntime.dll (PySide6 often ships 14.29).\n"
        "import struct as _struct\n"
        "def _ver(p):\n"
        "  try:\n"
        "    data=p.read_bytes()\n"
        "  except OSError:\n"
        "    return (0,0,0,0)\n"
        "  i=data.find('VS_VERSION_INFO'.encode('utf-16le'))\n"
        "  if i<0: return (0,0,0,0)\n"
        "  s=data.find(b'\\xbd\\x04\\xef\\xfe', i, i+512)\n"
        "  if s<0: return (0,0,0,0)\n"
        "  try:\n"
        "    ms,ls=_struct.unpack_from('<II', data, s+8)\n"
        "    return ((ms>>16)&0xffff, ms&0xffff, (ls>>16)&0xffff, ls&0xffff)\n"
        "  except Exception:\n"
        "    return (0,0,0,0)\n"
        "min_ver=(14,40,0,0)\n"
        "qdir=capi/'_mkcb_old_msvc'\n"
        "for name in ('vcruntime140.dll','vcruntime140_1.dll','msvcp140.dll','msvcp140_1.dll','msvcp140_2.dll','concrt140.dll','vcomp140.dll'):\n"
        "  p=capi/name\n"
        "  if p.is_file() and _ver(p)<min_ver:\n"
        "    try:\n"
        "      qdir.mkdir(parents=True, exist_ok=True)\n"
        "      dest=qdir/name\n"
        "      if dest.exists(): dest.unlink()\n"
        "      p.replace(dest)\n"
        "    except OSError:\n"
        "      pass\n"
        "for p in list(capi.glob('msvcp140-*.dll')):\n"
        "  if _ver(p)<min_ver:\n"
        "    try:\n"
        "      qdir.mkdir(parents=True, exist_ok=True)\n"
        "      dest=qdir/p.name\n"
        "      if dest.exists(): dest.unlink()\n"
        "      p.replace(dest)\n"
        "    except OSError:\n"
        "      pass\n"
        "# TTS-only dirs: never add PySide6/shiboken6 (old msvcp140) or runtime/ (vcruntime 14.38).\n"
        "dirs = [\n"
        "  capi,\n"
        "  site / 'numpy.libs',\n"
        "  site / 'pyopenjtalk_plus.libs',\n"
        "]\n"
        "# Drop Qt / outdated CRT dirs from PATH so accidental LoadLibrary PATH search cannot shadow System32.\n"
        "block = {str(site / 'PySide6').lower(), str(site / 'shiboken6').lower(), str(root / 'runtime').lower()}\n"
        "path_parts = [p for p in os.environ.get('PATH', '').split(os.pathsep) if p and p.lower() not in block]\n"
        "path_lower = {p.lower() for p in path_parts}\n"
        "changed = True  # PATH may have been filtered\n"
        "for d in dirs:\n"
        "  if not d.is_dir():\n"
        "    continue\n"
        "  key = str(d)\n"
        "  if key.lower() not in path_lower:\n"
        "    path_parts.insert(0, key)\n"
        "    path_lower.add(key.lower())\n"
        "    changed = True\n"
        "  try:\n"
        "    os.add_dll_directory(key)\n"
        "  except (AttributeError, FileNotFoundError, OSError):\n"
        "    pass\n"
        "if changed:\n"
        "  os.environ['PATH'] = os.pathsep.join(path_parts)\n"
        "# Bind System32 CRT first (≥14.40 after vc_redist), then onnxruntime — never preload runtime/ 14.38.\n"
        "sys32 = Path(os.environ.get('SystemRoot') or r'C:\\Windows') / 'System32'\n"
        "preload = [\n"
        "  sys32 / 'vcruntime140.dll',\n"
        "  sys32 / 'vcruntime140_1.dll',\n"
        "  sys32 / 'msvcp140.dll',\n"
        "  sys32 / 'msvcp140_1.dll',\n"
        "  sys32 / 'msvcp140_2.dll',\n"
        "]\n"
        "# Only preload capi CRT copies that are new enough (quarantine should have removed old ones).\n"
        "for name in ('vcruntime140.dll','vcruntime140_1.dll','msvcp140.dll','msvcp140_1.dll','msvcp140_2.dll'):\n"
        "  p = capi / name\n"
        "  if p.is_file() and _ver(p) >= min_ver:\n"
        "    preload.append(p)\n"
        "preload.extend([\n"
        "  capi / 'onnxruntime.dll',\n"
        "  capi / 'onnxruntime_providers_shared.dll',\n"
        "])\n"
        "for dll in preload:\n"
        "  if not dll.is_file():\n"
        "    continue\n"
        "  try:\n"
        "    ctypes.WinDLL(str(dll))\n"
        "  except OSError:\n"
        "    pass\n"
    )


def _synthesize_wav_bytes(synthesize) -> bytes:
    """Run ``synthesize(wav_file)`` into an in-memory WAV and return the bytes."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        synthesize(wav_file)
    return buffer.getvalue()


def _synthesize_classic_subprocess_wav(
    onnx_path: Path,
    text: str,
    stop: threading.Event,
    length_scale: float,
) -> Optional[bytes]:
    """Synthesize one classic piper-tts segment in a child process; return WAV bytes.

    Isolates onnxruntime native crashes from the tray UI process on Windows.
    Child stdout: ``MKCBWAV1`` + big-endian u32 length + WAV bytes (no temp file).
    Returns ``None`` when cancelled via ``stop``.
    """
    if not text.strip() or stop.is_set():
        return None
    # Child mirrors PiperLibraryEngine._classic_synthesize API variants.
    code = (
        _windows_dll_bootstrap_source()
        + "import io, struct, sys, wave\n"
        "from piper import PiperVoice\n"
        f"onnx={str(onnx_path)!r}\n"
        f"text={text!r}\n"
        f"length_scale={float(length_scale)!r}\n"
        "voice = PiperVoice.load(onnx)\n"
        "buf = io.BytesIO()\n"
        "with wave.open(buf, 'wb') as wav_file:\n"
        "    done = False\n"
        "    if hasattr(voice, 'synthesize_wav'):\n"
        "        try:\n"
        "            from piper import SynthesisConfig\n"
        "            voice.synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=length_scale))\n"
        "            done = True\n"
        "        except Exception:\n"
        "            pass\n"
        "        if not done:\n"
        "            try:\n"
        "                voice.synthesize_wav(text, wav_file, length_scale=length_scale)\n"
        "                done = True\n"
        "            except TypeError:\n"
        "                voice.synthesize_wav(text, wav_file)\n"
        "                done = True\n"
        "    if not done and hasattr(voice, 'synthesize'):\n"
        "        try:\n"
        "            voice.synthesize(text, wav_file, length_scale=length_scale)\n"
        "        except TypeError:\n"
        "            voice.synthesize(text, wav_file)\n"
        "        done = True\n"
        "    if not done:\n"
        "        raise RuntimeError('PiperVoice has neither synthesize_wav nor synthesize')\n"
        "payload = buf.getvalue()\n"
        "sys.stdout.buffer.write(b'MKCBWAV1' + struct.pack('>I', len(payload)) + payload)\n"
        "sys.stdout.buffer.flush()\n"
    )
    ensure_windows_native_dll_dirs(include_qt=False)
    env = _tts_child_environ()
    process = subprocess.Popen(
        [str(_probe_python_executable()), "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **_windows_hidden_subprocess_kwargs(),
    )
    # Pump stdout/stderr while the child runs. Waiting on poll() without reading
    # deadlocks once the WAV exceeds the OS pipe buffer (~64KiB).
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _pump(stream: Any, sink: list[bytes]) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                sink.append(chunk)
        except Exception:
            pass

    assert process.stdout is not None and process.stderr is not None
    out_thread = threading.Thread(
        target=_pump, args=(process.stdout, stdout_chunks), name="mkcb-piper-stdout", daemon=True
    )
    err_thread = threading.Thread(
        target=_pump, args=(process.stderr, stderr_chunks), name="mkcb-piper-stderr", daemon=True
    )
    out_thread.start()
    err_thread.start()
    try:
        while process.poll() is None:
            if stop.wait(0.05):
                process.terminate()
                break
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        out_thread.join(timeout=10)
        err_thread.join(timeout=2)
        if stop.is_set():
            return None
        stdout_b = b"".join(stdout_chunks)
        stderr_b = b"".join(stderr_chunks)
        returncode = process.returncode
        if returncode != 0:
            detail = stderr_b.decode("utf-8", errors="replace").strip()
            if returncode in (3221225477, -1073741819) or (
                returncode and hex(returncode & 0xFFFFFFFF) == "0xc0000005"
            ):
                detail = (
                    "native ACCESS_VIOLATION (0xC0000005) in piper-tts synthesize subprocess; "
                    "install VC++ 2015-2022 x64 runtime or check onnxruntime DLLs"
                )
            raise RuntimeError(detail or f"piper-tts synthesize failed exit={returncode}")
        if not stdout_b.startswith(_WAV_STDOUT_MAGIC) or len(stdout_b) < 12:
            raise RuntimeError("piper-tts synthesize produced no WAV payload")
        size = int.from_bytes(stdout_b[8:12], "big")
        wav_bytes = stdout_b[12 : 12 + size]
        if size <= 0 or len(wav_bytes) != size or size < 44:
            raise RuntimeError("piper-tts synthesize produced empty/truncated WAV")
        return wav_bytes
    finally:
        if process.poll() is None:
            process.kill()


def _synthesize_and_play_classic_subprocess(
    onnx_path: Path,
    text: str,
    stop: threading.Event,
    length_scale: float,
) -> None:
    """Synthesize one classic piper-tts segment in a child process, then play from memory."""
    LOGGER.info(
        "piper-tts synthesize start chars={} length_scale={:.3f} model={}",
        len(text),
        float(length_scale),
        onnx_path.name,
    )
    wav_bytes = _synthesize_classic_subprocess_wav(onnx_path, text, stop, length_scale)
    if wav_bytes is None or stop.is_set():
        LOGGER.info("piper-tts synthesize cancelled or empty")
        return
    LOGGER.info(
        "piper-tts synthesize done bytes={} duration_s={:.2f}; playing",
        len(wav_bytes),
        _wav_duration_seconds(wav_bytes),
    )
    _play_wav_bytes(wav_bytes, stop)


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
        process = subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **_windows_hidden_subprocess_kwargs(),
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


_SUBPROCESS_IMPORT_CACHE: dict[str, tuple[bool, str]] = {}


def clear_subprocess_import_cache(*module_names: str) -> None:
    """Drop cached child-process import results (e.g. before a manual re-probe)."""
    if not module_names:
        _SUBPROCESS_IMPORT_CACHE.clear()
        return
    for name in module_names:
        _SUBPROCESS_IMPORT_CACHE.pop(str(name), None)


def _subprocess_import_ok(module_name: str, *, timeout: float = 45.0) -> tuple[bool, str]:
    """Import ``module_name`` in a child process; survive native ACCESS_VIOLATION.

    Returns ``(ok, detail)``. On Windows a failed onnxruntime/piper load often
    kills the interpreter with ``0xC0000005`` before Python can raise — probing
    in a subprocess keeps the tray UI alive and yields a usable error string.
    """
    cached = _SUBPROCESS_IMPORT_CACHE.get(module_name)
    if cached is not None:
        return cached
    # Non-Windows: in-process import is enough (and cheaper).
    if platform.system() != "Windows":
        try:
            __import__(module_name)
        except Exception as exc:
            result = (False, str(exc) or exc.__class__.__name__)
        else:
            result = (True, "ok")
        _SUBPROCESS_IMPORT_CACHE[module_name] = result
        return result

    # Parent may keep Qt on PATH; TTS children must not inherit PySide6 CRT dirs.
    ensure_windows_native_dll_dirs(include_qt=False)
    # Re-exec the embeddable runtime so ._pth / site-packages stay consistent.
    # Bootstrap add_dll_directory inside the child before importing onnx-backed modules.
    # For piper, probe onnxruntime first so a VC++/DLL failure is attributed correctly.
    ort_preflight = ""
    if module_name == "piper":
        ort_preflight = (
            "try:\n"
            "  import onnxruntime  # noqa: F401\n"
            "except Exception as exc:\n"
            "  print('ERR', 'onnxruntime: ' + type(exc).__name__ + ': ' + str(exc), flush=True)\n"
            "  sys.exit(3)\n"
        )
    code = (
        _windows_dll_bootstrap_source()
        + "import sys\n"
        + ort_preflight
        + f"mod={module_name!r}\n"
        "try:\n"
        "  __import__(mod)\n"
        "  print('OK', flush=True)\n"
        "except Exception as exc:\n"
        "  print('ERR', type(exc).__name__ + ': ' + str(exc), flush=True)\n"
        "  sys.exit(2)\n"
    )
    env = _tts_child_environ()
    try:
        completed = subprocess.run(
            [str(_probe_python_executable()), "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
            **_windows_hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        result = (False, f"import {module_name} timed out after {timeout:.0f}s")
        _SUBPROCESS_IMPORT_CACHE[module_name] = result
        return result
    except OSError as exc:
        result = (False, f"could not spawn probe process: {exc}")
        _SUBPROCESS_IMPORT_CACHE[module_name] = result
        return result

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode == 0 and stdout.startswith("OK"):
        result = (True, "ok")
    else:
        # Native crash: returncode often 3221225477 (0xC0000005) with empty stdout.
        detail = stdout or stderr or f"exit={completed.returncode}"
        if completed.returncode in (3221225477, -1073741819) or (
            completed.returncode and hex(completed.returncode & 0xFFFFFFFF) == "0xc0000005"
        ):
            detail = _format_access_violation_detail(module_name, env)
            note_vc_redist_prompt(detail)
        elif detail.startswith("ERR "):
            detail = detail[4:]
        result = (False, detail)
    _SUBPROCESS_IMPORT_CACHE[module_name] = result
    return result


class PiperLibraryEngine:
    """Preferred TTS: classic ``piper-tts`` with zh_CN-huayan + en_US-lessac.

    Engine name stays ``piper`` so existing ``state.json`` ``tts.engine`` preferences keep
    working. On Windows, synthesize runs in a child process to isolate
    onnxruntime ACCESS_VIOLATION from the tray UI.
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
        self._backend: Optional[str] = None  # "piper"
        self._zh_voice: Any = None
        self._en_voice: Any = None
        self._zh_path: Optional[Path] = None
        self._en_path: Optional[Path] = None
        self._load_error: Optional[str] = None

    def available(self) -> bool:
        ensure_windows_native_dll_dirs(include_qt=False)
        return self._probe_classic_piper()

    def _probe_classic_piper(self) -> bool:
        # Native ACCESS_VIOLATION cannot be caught in-process on Windows; probe
        # imports in a child interpreter. Never import piper / onnxruntime into
        # the tray process on Windows — speak() uses a synthesize subprocess.
        ok, detail = _subprocess_import_ok("piper")
        if not ok:
            self._load_error = f"piper-tts import failed: {detail}"
            return False
        if platform.system() != "Windows":
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

    def _load_classic_voice(self, path: Path) -> Any:
        ensure_windows_native_dll_dirs(include_qt=False)
        from piper import PiperVoice  # type: ignore[import-not-found]

        return PiperVoice.load(path)

    def _classic_path_for(self, lang: str) -> Path:
        if lang == "en":
            if self._en_path is None:
                raise RuntimeError("en-US Piper model missing")
            return self._en_path
        if self._zh_path is None:
            if self._en_path is not None:
                return self._en_path
            raise RuntimeError("zh-CN Piper model missing")
        return self._zh_path

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
        # Windows: keep onnxruntime / piper out of the tray process. A native
        # ACCESS_VIOLATION (0xC0000005) would otherwise kill the whole client with
        # no Python traceback; synthesize + play in a child interpreter instead.
        if platform.system() == "Windows":
            for lang, segment in segments:
                if stop.is_set():
                    return
                onnx_path = self._classic_path_for("en" if lang == "en" else "zh")
                _synthesize_and_play_classic_subprocess(
                    onnx_path,
                    segment,
                    stop,
                    length_scale,
                )
            return
        for lang, segment in segments:
            if stop.is_set():
                return
            voice = self._classic_voice_for("en" if lang == "en" else "zh")
            self._speak_classic_segment(voice, segment, stop, length_scale)

    def _speak_classic_segment(
        self,
        voice: Any,
        text: str,
        stop: threading.Event,
        length_scale: float,
    ) -> None:
        if not text.strip() or stop.is_set():
            return
        wav_bytes = _synthesize_wav_bytes(
            lambda wav_file: self._classic_synthesize(voice, text, wav_file, length_scale)
        )
        if stop.is_set():
            return
        _play_wav_bytes(wav_bytes, stop)

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


# Soft TTL for remembered engine: after expiry, re-probe from priority order
# (piper first). Prevents a temporary SAPI fallback from sticking forever when
# Piper models/wheels land later via update.
ENGINE_PREFERENCE_TTL_SECONDS = 24 * 60 * 60


class EnginePreference:
    """Remember the last working engine for up to 24h, then re-probe.

    Persistence is ``state.json`` ``tts`` key via ``StateStore``. The constructor
    accepts either a ``StateStore``, a ``data_dir``, or a legacy file path
    (``…/tts-engine.json`` / ``…/tts.json``) whose parent is treated as data_dir.
    """

    def __init__(
        self,
        store_or_path: Union[str, Path, Any],
        *,
        ttl_seconds: int = ENGINE_PREFERENCE_TTL_SECONDS,
    ) -> None:
        from .store import StateStore

        self.ttl_seconds = max(0, int(ttl_seconds))
        if isinstance(store_or_path, StateStore):
            self._store = store_or_path
        else:
            path = Path(store_or_path)
            # Legacy call sites passed …/tts-engine.json; treat parent as data_dir.
            if path.name in ("tts-engine.json", "tts.json", "state.json"):
                data_dir = path.parent
            else:
                data_dir = path
            self._store = StateStore.for_data_dir(data_dir)
        # Kept for tests that still inspect .path (points at unified state.json).
        self.path = self._store.path

    def load(self) -> Optional[str]:
        engine, saved_at = self._store.tts_engine_preference()
        if not engine:
            return None
        # Legacy / missing saved_at: treat as expired so we re-probe.
        if saved_at is None:
            return None
        try:
            age = time.time() - float(saved_at)
        except (TypeError, ValueError):
            return None
        if self.ttl_seconds > 0 and age > self.ttl_seconds:
            return None
        return engine

    def save(self, engine: str) -> None:
        self._store.set_tts_engine_preference(engine)


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


def play_cached_speech_plan(
    plan: list[tuple[str, Any]],
    stop: threading.Event,
    on_text: Any = None,
) -> None:
    """Play a prefetch plan: ``(\"wav\", bytes)`` or ``(\"pause\", ms)`` entries."""
    for kind, payload in plan:
        if stop.is_set():
            return
        if kind == "pause":
            stop.wait(float(payload) / 1000.0)
        elif kind == "wav":
            wav = payload
            if not isinstance(wav, (bytes, bytearray)) or len(wav) < 44:
                continue
            if on_text is not None:
                on_text("")
            _play_wav_bytes(bytes(wav), stop)


def synthesize_parts_to_plan(
    parts: List[SpeechPart],
    *,
    data_dir: Path,
    speech_rate: float = 1.0,
    stop: Optional[threading.Event] = None,
) -> list[tuple[str, Any]]:
    """Prefetch Piper WAV for each text part; return a playable plan.

    Plan entries: ``(\"wav\", wav_bytes)`` or ``(\"pause\", duration_ms)``.
    Raises if Piper is unavailable or synthesis fails. Honours ``stop``.
    SAPI cannot produce cacheable WAV — callers should fall back to live speak.
    """
    cancel = stop or threading.Event()
    rate = normalize_speech_rate(speech_rate, default=1.0)
    length_scale = length_scale_for_rate(rate)
    model_dirs = resolve_tts_model_dirs(data_dir)
    engine = PiperLibraryEngine(model_dirs)
    if not engine.available():
        raise RuntimeError(engine._load_error or "piper is not configured")
    plan: list[tuple[str, Any]] = []
    for part in parts:
        if cancel.is_set():
            raise RuntimeError("cancelled")
        if part.kind == "pause":
            plan.append(("pause", int(part.duration_ms)))
            continue
        if not part.text:
            continue
        segments = split_language_segments(part.text)
        if not segments:
            continue
        # Windows: synthesize in a child process (onnxruntime AV isolation).
        if platform.system() == "Windows":
            for lang, segment in segments:
                if cancel.is_set():
                    raise RuntimeError("cancelled")
                onnx_path = engine._classic_path_for("en" if lang == "en" else "zh")
                wav = _synthesize_classic_subprocess_wav(
                    onnx_path,
                    segment,
                    cancel,
                    length_scale,
                )
                if cancel.is_set():
                    raise RuntimeError("cancelled")
                if not wav:
                    raise RuntimeError("piper-tts synthesize returned empty WAV")
                plan.append(("wav", wav))
            continue
        # Linux / in-process classic piper-tts.
        for lang, segment in segments:
            if cancel.is_set():
                raise RuntimeError("cancelled")
            voice = engine._classic_voice_for("en" if lang == "en" else "zh")
            wav_bytes = _synthesize_wav_bytes(
                lambda wav_file, _seg=segment, _voice=voice: PiperLibraryEngine._classic_synthesize(
                    _voice, _seg, wav_file, length_scale
                )
            )
            if cancel.is_set():
                raise RuntimeError("cancelled")
            plan.append(("wav", wav_bytes))
    return plan


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
    if preferred in ("piper-cli", "sapi5-zh"):
        # Older builds may have remembered CLI or SAPI after a failed Piper probe.
        # SAPI is fallback-only — never permanently pin it over piper-tts.
        preferred = None
    rate = normalize_speech_rate(speech_rate, default=1.0)

    # Always prefer the Python Piper library over SAPI when available.
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
            detail = getattr(engine, "_load_error", None) or "not available"
            LOGGER.info("TTS engine unavailable engine={} reason={}", engine.name, detail)
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
