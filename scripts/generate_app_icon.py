#!/usr/bin/env python3
"""Generate the shared MKCB application icon (PNG sizes + multi-resolution ICO)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZES = (16, 24, 32, 48, 64, 128, 256)


def make_image(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = max(1, size // 16)
    radius = max(2, size // 5)
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=radius,
        fill=(56, 189, 248, 255),  # #38bdf8
        outline=(15, 23, 42, 255),  # #0f172a
        width=max(1, size // 32),
    )
    font = None
    for candidate in (
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        path = Path(candidate)
        if path.is_file():
            font = ImageFont.truetype(str(path), size=max(10, int(size * 0.55)))
            break
    if font is None:
        font = ImageFont.load_default()
    letter = "M"
    bbox = draw.textbbox((0, 0), letter, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - text_w) / 2 - bbox[0]
    y = (size - text_h) / 2 - bbox[1]
    draw.text((x, y), letter, fill=(8, 47, 73, 255), font=font)  # #082f49
    return img


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("assets/icons"))
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    png_paths: list[Path] = []
    for size in SIZES:
        path = out / f"mkcb-{size}.png"
        make_image(size).save(path)
        png_paths.append(path)

    ico = out / "mkcb.ico"
    convert = shutil.which("magick") or shutil.which("convert")
    if convert is None:
        raise SystemExit("ImageMagick (magick/convert) is required to build mkcb.ico")
    cmd = [convert, *[str(path) for path in png_paths], str(ico)]
    print("+", subprocess.list2cmdline(cmd), flush=True)
    subprocess.run(cmd, check=True)
    print(f"wrote {ico} ({ico.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
