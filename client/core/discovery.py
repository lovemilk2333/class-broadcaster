"""UDP discovery for locating and verifying MKCB servers."""

from __future__ import annotations

import hashlib
import os
import random
import socket
import time
from typing import Tuple
from pydantic import BaseModel

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol.bson import BSONError, decode, encode
from .protocol.packet import Packet, PacketError

DISCOVERY_REQ = 0x0001
DISCOVERY_RESP = 0x0002
SIGNATURE_DOMAIN = b"MKCB-DISCOVERY-V1\x00"


class DiscoveryResponse(BaseModel):
    service_name: str
    server_public_key: bytes
    fingerprint: bytes
    tcp_port: int
    http_port: int
    protocol_major: int
    protocol_minor: int
    nonce: bytes
    address: Tuple[str, int]

    class Config:
        frozen = True


def verify_response(document: dict, expected_nonce: bytes) -> DiscoveryResponse:
    try:
        public_key = bytes(document["server_public_key"])
        fingerprint = bytes(document["fingerprint"])
        nonce = bytes(document["nonce"])
        signature = bytes(document["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed discovery response") from exc
    if len(public_key) != 32 or len(fingerprint) != 32 or len(signature) != 64:
        raise ValueError("invalid discovery identity fields")
    if nonce != expected_nonce or not 8 <= len(nonce) <= 64:
        raise ValueError("discovery nonce mismatch")
    key = Ed25519PublicKey.from_public_bytes(public_key)
    spki = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if hashlib.sha256(spki).digest() != fingerprint:
        raise ValueError("discovery fingerprint does not match public key")
    unsigned = {
        "service_name": document.get("service_name", ""),
        "server_public_key": public_key,
        "fingerprint": fingerprint,
        "tcp_port": document.get("tcp_port", 0),
        "http_port": document.get("http_port", 0),
        "protocol_major": document.get("protocol_major", 0),
        "protocol_minor": document.get("protocol_minor", 0),
        "nonce": nonce,
        "signature": None,
    }
    try:
        key.verify(signature, SIGNATURE_DOMAIN + encode(unsigned))
    except Exception as exc:
        raise ValueError("invalid discovery signature") from exc
    return DiscoveryResponse(
        service_name=str(unsigned["service_name"]), server_public_key=public_key,
        fingerprint=fingerprint, tcp_port=int(unsigned["tcp_port"]),
        http_port=int(unsigned["http_port"]), protocol_major=int(unsigned["protocol_major"]),
        protocol_minor=int(unsigned["protocol_minor"]), nonce=nonce, address=("", 0),
    )


def discover(
    destinations: list[tuple[str, int]], timeout: float = 1.5, broadcast: bool = False
) -> list[DiscoveryResponse]:
    nonce = os.urandom(16)
    sequence = random.randrange(0, 65536)
    request = encode({"nonce": nonce, "protocol_major": 1, "protocol_minor": 0})
    wire = Packet(1, 0, DISCOVERY_REQ, sequence, 0, request).encode()
    results: list[DiscoveryResponse] = []
    seen: set[bytes] = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(min(timeout, 0.25))
        for destination in destinations:
            sock.sendto(wire, destination)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, address = sock.recvfrom(1 << 20)
            except socket.timeout:
                continue
            try:
                packet = Packet.decode(data)
                if packet.packet_type != DISCOVERY_RESP:
                    continue
                response = verify_response(decode(packet.payload), nonce)
            except (PacketError, BSONError, ValueError):
                continue
            if response.fingerprint in seen:
                continue
            seen.add(response.fingerprint)
            results.append(response.model_copy(update={"address": address}))
    return results
