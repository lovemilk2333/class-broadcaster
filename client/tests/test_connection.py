import base64
import unittest

from client.core.connection import UPDATE_AVAILABLE, _fingerprint_bytes, decode_update_available
from client.core.protocol.bson import encode
from client.core.protocol.packet import Packet


class ConnectionFingerprintTest(unittest.TestCase):
    def test_accepts_hex_and_ssh_style_base64_fingerprints(self):
        digest = bytes(range(32))
        encoded = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
        self.assertEqual(_fingerprint_bytes(encoded), digest)
        self.assertEqual(_fingerprint_bytes(digest.hex()), digest)

    def test_rejects_invalid_fingerprint(self):
        with self.assertRaises(ValueError):
            _fingerprint_bytes("SHA256:not-a-digest")


class UpdateNotificationTest(unittest.TestCase):
    def test_decodes_valid_update_notification_with_download_token(self):
        metadata = {
            "component": "bundle",
            "components": ["client", "updater"],
            "version": "0.1.1",
            "platform": "windows-amd64",
            "payload_format": "files-v1",
            "sha256": "a" * 64,
            "seq": 7,
            "force": True,
            "download_token": "payload.signature",
            "expires_at": 1_700_000_000,
        }
        packet = Packet(1, 0, UPDATE_AVAILABLE, 0, 0, encode(metadata))
        decoded = decode_update_available(packet)
        self.assertEqual(decoded["component"], "bundle")
        self.assertEqual(decoded["components"], ["updater", "client"])
        self.assertEqual(decoded["version"], "0.1.1")
        self.assertEqual(decoded["sha256"], "a" * 64)
        self.assertEqual(decoded["seq"], 7)
        self.assertTrue(decoded["force"])
        self.assertEqual(decoded["download_token"], "payload.signature")
        self.assertEqual(decoded["expires_at"], 1_700_000_000)
        self.assertNotIn("package", decoded)
        self.assertNotIn("download_url", decoded)

    def test_decodes_legacy_http_download_url(self):
        metadata = {
            "component": "client",
            "version": "0.1.1",
            "platform": "windows-amd64",
            "payload_format": "files-v1",
            "sha256": "a" * 64,
            "download_url": "http://10.22.33.2:39003/api/v1/updates/packages/" + ("a" * 64) + "?token=x",
        }
        packet = Packet(1, 0, UPDATE_AVAILABLE, 0, 0, encode(metadata))
        decoded = decode_update_available(packet)
        self.assertEqual(decoded["download_url"], metadata["download_url"])

    def test_decodes_legacy_https_release_url(self):
        metadata = {
            "component": "client",
            "version": "0.1.1",
            "platform": "windows-amd64",
            "payload_format": "files-v1",
            "sha256": "a" * 64,
            "signature": "A" * 86,
            "release_url": "https://updates.example/client.zst",
        }
        packet = Packet(1, 0, UPDATE_AVAILABLE, 0, 0, encode(metadata))
        decoded = decode_update_available(packet)
        self.assertEqual(decoded["release_url"], metadata["release_url"])
        self.assertEqual(decoded["signature"], metadata["signature"])

    def test_rejects_non_https_legacy_release_url(self):
        metadata = {
            "component": "client",
            "version": "0.1.1",
            "platform": "windows-amd64",
            "payload_format": "files-v1",
            "sha256": "a" * 64,
            "signature": "A" * 86,
            "release_url": "http://updates.example/client.zst",
        }
        packet = Packet(1, 0, UPDATE_AVAILABLE, 0, 0, encode(metadata))
        with self.assertRaises(ValueError):
            decode_update_available(packet)


if __name__ == "__main__":
    unittest.main()
