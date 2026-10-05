import json
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from client.core.updater import UpdateError, canonical_metadata, stage_update, verify_metadata


def _write_tar_package(path: Path, metadata: dict, files: list[tuple[str, bytes]]) -> None:
    """Build a plain tar package (zstd optional; client accepts tar after decompress)."""
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        metadata_bytes = (json.dumps(metadata, sort_keys=True, ensure_ascii=True) + "\n").encode()
        info = tarfile.TarInfo("metadata.json")
        info.size = len(metadata_bytes)
        archive.addfile(info, io.BytesIO(metadata_bytes))
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


class UpdaterTest(unittest.TestCase):
    def test_files_v1_stages_direct_files_without_metadata(self):
        files = [("client.exe", b"client"), ("lib/helper.dll", b"helper")]
        manifest = [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in files
        ]
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        metadata = {
            "components": ["client"],
            "component": "client",
            "version": "2.0.0",
            "platform": "windows-x64",
            "payload_format": "files-v1",
            "sha256": digest,
        }
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package.tar"
            _write_tar_package(package, metadata, files)
            staged = stage_update(package, metadata, Path(directory) / "staged")
            self.assertEqual((staged / "client.exe").read_bytes(), b"client")
            self.assertEqual((staged / "lib/helper.dll").read_bytes(), b"helper")
            self.assertFalse((staged / "metadata.json").exists())

    def test_metadata_signature_and_archive_staging(self):
        private = Ed25519PrivateKey.generate()
        payload = b"fixture"
        metadata = {
            "components": ["client"],
            "component": "client",
            "version": "1.2.3",
            "platform": "windows-x64",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        metadata["signature"] = __import__("base64").b64encode(private.sign(canonical_metadata(metadata))).decode("ascii").rstrip("=")
        verify_metadata(metadata, private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package.tar"
            _write_tar_package(package, metadata, [("client.exe", payload)])
            staged = stage_update(package, metadata, Path(directory) / "staged")
            self.assertEqual((staged / "client.exe").read_bytes(), b"fixture")

    def test_archive_path_traversal_is_rejected(self):
        metadata = {
            "components": ["client"],
            "component": "client",
            "version": "1",
            "platform": "windows-x64",
            "sha256": hashlib.sha256(b"bad").hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package.tar"
            _write_tar_package(package, metadata, [("../escape", b"bad")])
            with self.assertRaises(UpdateError):
                stage_update(package, metadata, Path(directory) / "staged")

    def test_files_v1_path_traversal_is_rejected(self):
        data = b"bad"
        manifest = [{"path": "../escape", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
        metadata = {
            "components": ["client"],
            "component": "client",
            "version": "1",
            "platform": "windows-x64",
            "payload_format": "files-v1",
            "sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "package.tar"
            _write_tar_package(package, metadata, [("../escape", data)])
            with self.assertRaises(UpdateError):
                stage_update(package, metadata, Path(directory) / "staged")

    def test_bundle_components_normalize(self):
        metadata = {
            "components": ["client", "updater"],
            "component": "bundle",
            "version": "1",
            "platform": "windows-x64",
            "sha256": "a" * 64,
        }
        from client.core.updater import normalize_components

        self.assertEqual(normalize_components(metadata), ["updater", "client"])


if __name__ == "__main__":
    unittest.main()
