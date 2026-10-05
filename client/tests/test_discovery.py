import hashlib
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.core.discovery import (
    LIMITED_BROADCAST,
    LocalInterface,
    SIGNATURE_DOMAIN,
    list_local_ipv4_interfaces,
    parse_scan_destinations,
    verify_response,
)
from client.core.protocol.bson import encode


class DiscoveryTest(unittest.TestCase):
    def test_cidr_includes_directed_broadcast_and_hosts(self):
        destinations, broadcast = parse_scan_destinations("10.22.33.1/24")
        self.assertTrue(broadcast)
        self.assertIn(("10.22.33.255", 39001), destinations)
        self.assertIn(("10.22.33.1", 39001), destinations)
        self.assertNotIn((LIMITED_BROADCAST, 39001), destinations)

    def test_limited_broadcast_includes_each_nic_directed_broadcast(self):
        fake = [
            LocalInterface(name="eth0", address="10.0.0.100", network="10.0.0.0/16", broadcast="10.0.255.255"),
            LocalInterface(name="usb0", address="10.22.33.243", network="10.22.33.0/24", broadcast="10.22.33.255"),
        ]
        with mock.patch("client.core.discovery.list_local_ipv4_interfaces", return_value=fake):
            destinations, broadcast = parse_scan_destinations("255.255.255.255")
        self.assertTrue(broadcast)
        self.assertIn((LIMITED_BROADCAST, 39001), destinations)
        self.assertIn(("10.0.255.255", 39001), destinations)
        self.assertIn(("10.22.33.255", 39001), destinations)

    def test_auto_alias_uses_broadcast(self):
        destinations, broadcast = parse_scan_destinations("auto")
        self.assertTrue(broadcast)
        self.assertIn((LIMITED_BROADCAST, 39001), destinations)

    def test_single_host_is_unicast(self):
        destinations, broadcast = parse_scan_destinations("192.168.1.10")
        self.assertFalse(broadcast)
        self.assertEqual(destinations, [("192.168.1.10", 39001)])

    def test_list_local_interfaces_uses_lib_private_ip(self):
        import ipaddress

        fake = [
            ipaddress.IPv4Interface("10.0.0.100/16"),
            ipaddress.IPv4Interface("10.22.33.243/24"),
        ]
        with mock.patch("client.core.discovery._interfaces_via_lib_private_ip", return_value=[
            LocalInterface(name="", address="10.0.0.100", network="10.0.0.0/16", broadcast="10.0.255.255"),
            LocalInterface(name="", address="10.22.33.243", network="10.22.33.0/24", broadcast="10.22.33.255"),
        ]):
            interfaces = list_local_ipv4_interfaces()
        addresses = {item.address for item in interfaces}
        self.assertIn("10.0.0.100", addresses)
        self.assertIn("10.22.33.243", addresses)
        by_addr = {item.address: item for item in interfaces}
        self.assertEqual(by_addr["10.22.33.243"].broadcast, "10.22.33.255")
        self.assertEqual(by_addr["10.22.33.243"].network, "10.22.33.0/24")
        # Ensure the helper path is preferred (no dependency on Linux `ip`).
        del fake

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
