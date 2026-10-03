import unittest
import json
import base64
import hashlib
from cryptography.hazmat.primitives import serialization

from client.core.identity import decrypt_private_key, encrypt_private_key, generate_identity


class IdentityTest(unittest.TestCase):
    def test_installation_bound_encryption(self):
        identity = generate_identity()
        timestamp = 1_700_000_000_000
        blob = encrypt_private_key(identity.private_key, timestamp, "cpu-abc", "device-1")
        document = json.loads(blob)
        self.assertEqual(document["format_version"], 2)
        stored_id = base64.b64decode(document["client_id"])
        spki = identity.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.assertEqual(stored_id, hashlib.sha256(spki).digest())
        restored = decrypt_private_key(blob, "cpu-abc", "device-1")
        self.assertEqual(restored.private_bytes_raw(), identity.private_key.private_bytes_raw())
        with self.assertRaises(ValueError):
            decrypt_private_key(blob, "other-cpu", "device-1")


if __name__ == "__main__":
    unittest.main()
