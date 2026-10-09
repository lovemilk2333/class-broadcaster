#!/usr/bin/env python3
"""Download offline TTS ONNX voices into ``client/tts/``.

Downloads both platform families so a single cache serves Windows packaging
and Linux development:

1. **piper-plus (Windows)**: Tsukuyomi-chan 6-language FP16 model
   (``tsukuyomi-chan-6lang-fp16.onnx`` + ``config.json``) from
   Hugging Face ``ayousanz/piper-plus-tsukuyomi-chan``. Alias / catalog name:
   ``tsukuyomi`` / ``ja_JP-tsukuyomi-chan-medium``.

2. **piper-tts (Linux)**: rhasspy zh-CN huayan + en-US lessac medium voices.

Models are large and gitignored. ``make clean`` must NOT delete this cache.
Packaged Windows builds copy ``client/tts`` → ``Lib/tts``.

Usage:
  python3 scripts/download_tts_models.py
  python3 scripts/download_tts_models.py --force
  python3 scripts/download_tts_models.py --skip-piper-plus
  python3 scripts/download_tts_models.py --skip-rhasspy
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "client" / "tts"

# Hugging Face rhasspy/piper-voices (MIT). Medium quality is a good classroom default.
# Used by Linux ``piper-tts``.
RHASSYP_VOICES = (
    {
        "stem": "zh_CN-huayan-medium",
        "onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx",
        "json": "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json",
    },
    {
        "stem": "en_US-lessac-medium",
        "onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "json": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
    },
)

# piper-plus Tsukuyomi-chan (Windows). Saved under the HF filenames the client looks for.
TSUKUYOMI = {
    "onnx_name": "tsukuyomi-chan-6lang-fp16.onnx",
    "config_name": "config.json",
    "onnx": "https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/tsukuyomi-chan-6lang-fp16.onnx",
    "config": "https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/config.json",
    "alias": "tsukuyomi",
}


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    print(f"Downloading {url}", flush=True)
    print(f"  -> {destination}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "mkcb-tts-download/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    partial.replace(destination)


def ensure_rhasspy_voice(voice: dict[str, str], out_dir: Path, force: bool) -> None:
    stem = voice["stem"]
    onnx_path = out_dir / f"{stem}.onnx"
    json_path = out_dir / f"{stem}.onnx.json"
    if onnx_path.is_file() and json_path.is_file() and not force:
        size_mib = onnx_path.stat().st_size / (1024 * 1024)
        print(f"Reuse {stem} ({size_mib:.1f} MiB)", flush=True)
        return
    download(voice["onnx"], onnx_path)
    download(voice["json"], json_path)
    digest = hashlib.sha256(onnx_path.read_bytes()).hexdigest()[:16]
    print(f"Ready {stem} sha256_prefix={digest}", flush=True)


def _try_piper_plus_cli_download(out_dir: Path) -> bool:
    """Shell out to ``python -m piper_plus --download-model tsukuyomi`` when installed."""
    try:
        import piper_plus  # noqa: F401  # type: ignore[import-not-found]
    except Exception:
        return False
    print(
        f"Trying piper-plus CLI download alias={TSUKUYOMI['alias']} dir={out_dir}",
        flush=True,
    )
    # Newer CLIs accept --download-dir / --model-dir; try both.
    attempts = (
        [
            sys.executable,
            "-m",
            "piper_plus",
            "--download-model",
            TSUKUYOMI["alias"],
            "--download-dir",
            str(out_dir),
        ],
        [
            sys.executable,
            "-m",
            "piper_plus",
            "--download-model",
            TSUKUYOMI["alias"],
            "--model-dir",
            str(out_dir),
        ],
        [
            sys.executable,
            "-m",
            "piper_plus",
            "--download-model",
            "ja_JP-tsukuyomi-chan-medium",
            "--download-dir",
            str(out_dir),
        ],
    )
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"piper-plus CLI attempt failed: {exc}", flush=True)
            continue
        if result.returncode == 0:
            print("piper-plus CLI download succeeded", flush=True)
            if result.stdout.strip():
                print(result.stdout.strip(), flush=True)
            return True
        detail = (result.stderr or result.stdout or "").strip()
        print(f"piper-plus CLI exit={result.returncode} detail={detail[:400]}", flush=True)
    return False


def _normalize_tsukuyomi_layout(out_dir: Path) -> bool:
    """Ensure canonical filenames exist after a CLI download that used other names."""
    target_onnx = out_dir / TSUKUYOMI["onnx_name"]
    target_config = out_dir / TSUKUYOMI["config_name"]
    if target_onnx.is_file() and target_config.is_file():
        return True
    # Search common CLI / nested layouts and copy into the flat cache names.
    candidates_onnx = [
        out_dir / "tsukuyomi.onnx",
        out_dir / "ja_JP-tsukuyomi-chan-medium.onnx",
        out_dir / "tsukuyomi" / TSUKUYOMI["onnx_name"],
        out_dir / "tsukuyomi" / "tsukuyomi.onnx",
        out_dir / "tsukuyomi-chan" / TSUKUYOMI["onnx_name"],
    ]
    candidates_config = [
        out_dir / "tsukuyomi.onnx.json",
        out_dir / "ja_JP-tsukuyomi-chan-medium.onnx.json",
        out_dir / "tsukuyomi" / "config.json",
        out_dir / "tsukuyomi" / "tsukuyomi.onnx.json",
        out_dir / "tsukuyomi-chan" / "config.json",
        out_dir / TSUKUYOMI["config_name"],
    ]
    src_onnx = next((path for path in candidates_onnx if path.is_file()), None)
    src_config = next((path for path in candidates_config if path.is_file()), None)
    if src_onnx is None or src_config is None:
        return False
    if not target_onnx.is_file():
        shutil.copy2(src_onnx, target_onnx)
        print(f"Normalized {src_onnx.name} -> {target_onnx.name}", flush=True)
    if not target_config.is_file():
        shutil.copy2(src_config, target_config)
        print(f"Normalized {src_config.name} -> {target_config.name}", flush=True)
    return target_onnx.is_file() and target_config.is_file()


def ensure_tsukuyomi(out_dir: Path, force: bool) -> None:
    onnx_path = out_dir / TSUKUYOMI["onnx_name"]
    config_path = out_dir / TSUKUYOMI["config_name"]
    if onnx_path.is_file() and config_path.is_file() and not force:
        size_mib = onnx_path.stat().st_size / (1024 * 1024)
        print(f"Reuse tsukuyomi ({size_mib:.1f} MiB)", flush=True)
        return
    # Prefer piper-plus catalog when the package is installed (Windows build host).
    if not force and _try_piper_plus_cli_download(out_dir) and _normalize_tsukuyomi_layout(out_dir):
        digest = hashlib.sha256(onnx_path.read_bytes()).hexdigest()[:16]
        print(f"Ready tsukuyomi sha256_prefix={digest}", flush=True)
        return
    # Direct Hugging Face URLs (works without piper-plus installed, e.g. Linux CI).
    download(TSUKUYOMI["onnx"], onnx_path)
    download(TSUKUYOMI["config"], config_path)
    digest = hashlib.sha256(onnx_path.read_bytes()).hexdigest()[:16]
    print(f"Ready tsukuyomi sha256_prefix={digest}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory (default: client/tts)")
    parser.add_argument("--force", action="store_true", help="Re-download even when files exist")
    parser.add_argument(
        "--skip-piper-plus",
        action="store_true",
        help="Skip Tsukuyomi piper-plus model (Windows)",
    )
    parser.add_argument(
        "--skip-rhasspy",
        action="store_true",
        help="Skip rhasspy zh/en piper-tts models (Linux)",
    )
    args = parser.parse_args()
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_piper_plus:
        print("=== piper-plus Tsukuyomi-chan (Windows) ===", flush=True)
        ensure_tsukuyomi(out_dir, args.force)
    if not args.skip_rhasspy:
        print("=== piper-tts rhasspy zh/en (Linux) ===", flush=True)
        for voice in RHASSYP_VOICES:
            ensure_rhasspy_voice(voice, out_dir, args.force)

    print(f"TTS models ready under {out_dir}", flush=True)
    print(
        "Layout: tsukuyomi-chan-6lang-fp16.onnx + config.json (win32 piper-plus); "
        "zh_CN-huayan-medium.onnx(+.json) + en_US-lessac-medium.onnx(+.json) (linux piper-tts).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
