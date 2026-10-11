#!/usr/bin/env python3
"""Legacy helper: download NLTK corpora formerly needed by piper-plus / g2p-en.

Classic ``piper-tts`` (huayan/lessac) no longer needs these. The script remains
so older Makefile / CI invocations of ``make download-nltk-data`` still succeed.
New builds do not require ``client/nltk_data``.

Usage::

    python3 scripts/download_nltk_data.py
    python3 scripts/download_nltk_data.py --out client/nltk_data --force
"""

from __future__ import annotations

import argparse
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Official NLTK data packages (gh-pages). Kept as zip so ``nltk.data.find('…zip')``
# succeeds without extracting (legacy g2p_en looked for the zip path specifically).
PACKAGES: tuple[tuple[str, str, str], ...] = (
    (
        "taggers/averaged_perceptron_tagger.zip",
        "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/taggers/averaged_perceptron_tagger.zip",
        "averaged_perceptron_tagger",
    ),
    (
        "corpora/cmudict.zip",
        "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/cmudict.zip",
        "cmudict",
    ),
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "client" / "nltk_data"


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    print(f"Downloading {url}", flush=True)
    print(f"  -> {destination}", flush=True)
    context = ssl.create_default_context()
    request = urllib.request.Request(url, headers={"User-Agent": "mkcb-nltk-download/1.0"})
    with urllib.request.urlopen(request, timeout=120, context=context) as response, partial.open(
        "wb"
    ) as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    partial.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory")
    parser.add_argument("--force", action="store_true", help="Re-download even when files exist")
    args = parser.parse_args()
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(
        "NOTE: NLTK data is optional after the classic piper-tts switch; "
        "keeping this script for legacy callers.",
        flush=True,
    )
    for rel, url, label in PACKAGES:
        destination = out_dir / rel
        if destination.is_file() and not args.force:
            size_kib = destination.stat().st_size / 1024
            print(f"Reuse {label} ({size_kib:.1f} KiB)", flush=True)
            continue
        try:
            download(url, destination)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"FAILED {label}: {exc}", file=sys.stderr, flush=True)
            return 1
        print(f"Ready {label}", flush=True)
    print(f"NLTK data ready under {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
