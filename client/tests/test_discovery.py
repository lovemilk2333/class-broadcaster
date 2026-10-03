import hashlib
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.core.discovery import SIGNATURE_DOMAIN, verify_response
from client.core.protocol.bson import encode


class DiscoveryTest(unittest.TestCase):
    def test_verifies_signed_response_and_fingerprint(self):
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        spki = private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        nonce = b"12345678"
        response = {
            "service_name": "test",
            "server_public_key": public,
            "fingerprint": hashlib.sha256(spki).digest(),
            "tcp_port": 39002,
            "http_port": 39003,
            "protocol_major": 1,
            "protocol_minor": 0,
            "nonce": nonce,
            "signature": None,
        }
        response["signature"] = private.sign(SIGNATURE_DOMAIN + encode(response))
        result = verify_response(response, nonce)
        self.assertEqual(result.server_public_key, public)

    def test_rejects_fingerprint_tampering(self):
        with self.assertRaises(ValueError):
            verify_response({"server_public_key": b"x"}, b"12345678")


if __name__ == "__main__":
    unittest.main()
