"""生成直接文件格式的 zstd 更新包和签名元数据。

兼容旧的单文件 ``--input`` CLI；生成的 zstd 流内 zip 直接包含
``metadata.json`` 与该文件，不再生成或嵌套 ``payload.zip``。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey



def canonical_metadata(metadata: dict[str, str]) -> bytes:
    signed = {key: value for key, value in metadata.items() if key != "signature"}
    return json.dumps(signed, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def main() -> None:
    parser = argparse.ArgumentParser(description="publish MKCB update package")
    parser.add_argument("--component", choices=("client", "updater"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True, help="raw 32-byte Ed25519 seed")
    parser.add_argument("--zstd", default="zstd")
    args = parser.parse_args()
    if not args.input.is_file():
        raise SystemExit(f"input does not exist: {args.input}")
    data = args.input.read_bytes()
    manifest = [{"path": args.input.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    metadata = {
        "component": args.component,
        "version": args.version,
        "platform": args.platform,
        "sha256": digest,
        "payload_format": "files-v1",
    }
    key = Ed25519PrivateKey.from_private_bytes(args.private_key.read_bytes())
    metadata["signature"] = base64.b64encode(key.sign(canonical_metadata(metadata))).decode("ascii").rstrip("=")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mkcb-publish-") as directory:
        archive_path = Path(directory) / "package.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("metadata.json", json.dumps(metadata, sort_keys=True, ensure_ascii=True) + "\n")
            archive.writestr(args.input.name, data)
        with archive_path.open("rb") as source, args.output.open("wb") as destination:
            result = subprocess.run([args.zstd, "-3", "-q", "-c"], stdin=source, stdout=destination)
            if result.returncode:
                raise SystemExit(result.returncode)
    args.output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
