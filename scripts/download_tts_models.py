#!/usr/bin/env python3
"""Download offline TTS ONNX voices into ``client/tts/``.

Classic ``piper-tts`` voices for both Windows and Linux packaging:

* ``zh_CN-huayan-medium`` (Chinese)
* ``en_US-lessac-medium`` (English)

Models are large and gitignored. ``make clean`` must NOT delete this cache.
Packaged Windows builds copy ``client/tts`` → ``Lib/tts``.

Usage:
  python3 scripts/download_tts_models.py
  python3 scripts/download_tts_models.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "client" / "tts"

# Hugging Face rhasspy/piper-voices (MIT). Medium quality is a good classroom default.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory (default: client/tts)")
    parser.add_argument("--force", action="store_true", help="Re-download even when files exist")
    # Kept for older Makefile / CI invocations; Tsukuyomi is no longer downloaded.
    parser.add_argument(
        "--skip-piper-plus",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skip-rhasspy",
        action="store_true",
        help="Skip rhasspy zh/en piper-tts models",
    )
    args = parser.parse_args()
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_rhasspy:
        print("=== piper-tts rhasspy zh/en (Windows + Linux) ===", flush=True)
        for voice in RHASSYP_VOICES:
            ensure_rhasspy_voice(voice, out_dir, args.force)

    print(f"TTS models ready under {out_dir}", flush=True)
    print(
        "Layout: zh_CN-huayan-medium.onnx(+.json) + en_US-lessac-medium.onnx(+.json).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
