"""TLS 1.3 client connection and business handshake."""

from __future__ import annotations

import hashlib
import base64
import os
import socket
import ssl
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel
from loguru import logger

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .protocol.bson import decode, encode
from .protocol.packet import Packet, read_packet
from .store import StateStore


LOGGER = logger.bind(name="mkcb.client.connection")

CONNECT_REQ = 0x0101
CONNECT_RESP = 0x0102
CONFIG_UPDATE = 0x0103
PING = 0x0201
PONG = 0x0202
MESSAGE = 0x1001
MESSAGE_ACK = 0x1002
MESSAGE_WITHDRAW = 0x1003
SERVER_SHUTDOWN = 0x0203
ERROR = 0x7F00


class ClientNotApprovedError(ConnectionError):
    """The server requires management approval before accepting this client."""


class IncomingMessage(BaseModel):
    message_id: str
    queue_seq: int
    priority: int
    content: str
    created_at_ms: Optional[int]
    expires_at_ms: Optional[int]
    display_position: Optional[str] = None
    display_duration_ratio: Optional[float] = None
    as_default: bool = False
    default_display_position: Optional[str] = None
    default_display_duration_ratio: Optional[float] = None
    speech: List[Dict[str, Any]]
    tts_enabled: bool = False

    class Config:
        frozen = True


class IncomingWithdrawal(BaseModel):
    message_id: str
    message: str = "消息已撤回"
    content: str = ""

    class Config:
        frozen = True


class Connection(BaseModel):
    socket: ssl.SSLSocket
    client_public_key: bytes

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, socket=None, client_public_key: bytes = b"", **data: object) -> None:
        if socket is not None:
            data.update(socket=socket, client_public_key=client_public_key)
        super().__init__(**data)

    def send(self, packet: Packet) -> None:
        self.socket.sendall(packet.encode())

    def receive(self) -> Packet:
        return read_packet(self.socket)

    def receive_message(self) -> IncomingMessage:
        packet = self.receive()
        if packet.packet_type == ERROR:
            document = decode(packet.payload)
            raise ConnectionError(str(document.get("code", "server_error")))
        return decode_message(packet)

    def receive_event(self) -> Union[IncomingMessage, IncomingWithdrawal]:
        packet = self.receive()
        if packet.packet_type == ERROR:
            document = decode(packet.payload)
            raise ConnectionError(str(document.get("code", "server_error")))
        if packet.packet_type == MESSAGE_WITHDRAW:
            return decode_withdrawal(packet)
        return decode_message(packet)

    def close(self) -> None:
        self.socket.close()

    def acknowledge(self, message_id: str, status: str, error: Optional[str] = None) -> None:
        """Acknowledge a received message using the same message UUID.

        The server treats this as an idempotent application-level receipt. The
        method is deliberately separate from ``receive`` so a UI/event loop can
        decide when a message reached received/displayed/spoken state.
        """
        payload = {"message_id": message_id, "status": status}
        if error:
            payload["error"] = error
        self.send(Packet(1, 0, MESSAGE_ACK, 0, 0, encode(payload)))


def decode_message(packet: Packet) -> IncomingMessage:
    if packet.packet_type != MESSAGE:
        raise ValueError("packet is not a message")
    document = decode(packet.payload)
    try:
        message_id = str(document["message_id"])
        content = str(document["content"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed message payload") from exc
    return IncomingMessage(
        message_id=message_id,
        queue_seq=int(document.get("queue_seq", 0)),
        priority=int(document.get("priority", 1024)),
        content=content,
        created_at_ms=_optional_int(document.get("created_at")),
        expires_at_ms=_optional_int(document.get("expires_at")),
        display_position=(str(document["display_position"]) if document.get("display_position") else None),
        display_duration_ratio=_optional_float(document.get("display_duration_ratio")),
        as_default=bool(document.get("as_default", False)),
        default_display_position=(str(document["default_display_position"]) if document.get("default_display_position") else None),
        default_display_duration_ratio=_optional_float(document.get("default_display_duration_ratio")),
        speech=list(document.get("speech", [])),
        tts_enabled=bool(document.get("tts_enabled", bool(document.get("speech")))),
    )


def _optional_int(value: object) -> Optional[int]:
    return int(value) if isinstance(value, int) else None


def _optional_float(value: object) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def decode_withdrawal(packet: Packet) -> IncomingWithdrawal:
    if packet.packet_type != MESSAGE_WITHDRAW:
        raise ValueError("packet is not a message withdrawal")
    document = decode(packet.payload)
    return IncomingWithdrawal(
        message_id=str(document["message_id"]),
        message=str(document.get("message", "消息已撤回")),
        content=str(document.get("content", "")),
    )


def _fingerprint_text(value: bytes) -> str:
    return "SHA256:" + base64.b64encode(value).decode("ascii").rstrip("=")


def probe_server_fingerprint(host: str, port: int, timeout: float = 3.0) -> bytes:
    """Read a server certificate fingerprint before the user approves trust."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=None) as tls_socket:
            certificate = x509.load_der_x509_certificate(tls_socket.getpeercert(binary_form=True))
            spki = certificate.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            fingerprint = hashlib.sha256(spki).digest()
            LOGGER.debug("probed server certificate fingerprint {}:{} {}", host, port, _fingerprint_text(fingerprint))
            return fingerprint


def connect(
    host: str,
    port: int,
    certfile: str,
    keyfile: str,
    expected_server_fingerprint: Union[bytes, str],
    config_id: Optional[str] = None,
    timeout: float = 10.0,
) -> tuple[Connection, dict]:
    if isinstance(expected_server_fingerprint, str):
        encoded = expected_server_fingerprint.strip()
        if encoded.lower().startswith("sha256:"):
            encoded = encoded[len("SHA256:"):]
        expected_server_fingerprint = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
    if expected_server_fingerprint and len(expected_server_fingerprint) != hashlib.sha256().digest_size:
        raise ValueError("server fingerprint must be 32 bytes")
    private_key = _load_private_key(keyfile)
    client_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    LOGGER.info("TCP/TLS connection {}:{}", host, port)
    raw = socket.create_connection((host, port), timeout=timeout)
    tls_socket = context.wrap_socket(raw, server_hostname=None)
    try:
        cert_der = tls_socket.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(cert_der)
        raw_public_key = cert.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        spki = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        actual_server_fingerprint = hashlib.sha256(spki).digest()
        public_key_hash = hashlib.sha256(raw_public_key).digest()
        LOGGER.debug(
            "received server certificate public_key_sha256={} spki_sha256={} certificate_sha256={}",
            _fingerprint_text(public_key_hash),
            _fingerprint_text(actual_server_fingerprint),
            _fingerprint_text(hashlib.sha256(cert_der).digest()),
        )
        if expected_server_fingerprint and actual_server_fingerprint != expected_server_fingerprint:
            LOGGER.error(
                "server certificate fingerprint mismatch {}:{} expected={} actual={}",
                host,
                port,
                _fingerprint_text(expected_server_fingerprint),
                _fingerprint_text(actual_server_fingerprint),
            )
            raise ssl.SSLError("server certificate fingerprint mismatch")
        request = {
            "request_id": os.urandom(16),
            "client_public_key": client_public_key,
            "protocol_major": 1,
            "protocol_minor": 0,
        }
        if config_id is not None:
            request["config_id"] = config_id
        tls_socket.sendall(Packet(1, 0, CONNECT_REQ, 1, 0, encode(request)).encode())
        packet = read_packet(tls_socket)
        if packet.packet_type == ERROR:
            try:
                document = decode(packet.payload)
                code = str(document.get("code", "server_error"))
                message = str(document.get("message", code))
                retryable = bool(document.get("retryable", False))
                if code == "client_not_approved":
                    raise ClientNotApprovedError(message)
                raise ssl.SSLError(
                    "{}: {}{}".format(code, message, " (retryable)" if retryable else "")
                )
            except (ClientNotApprovedError, ssl.SSLError):
                raise
            except Exception as exc:
                raise ssl.SSLError("server_error: malformed error response") from exc
        if packet.packet_type != CONNECT_RESP:
            LOGGER.error("unexpected handshake packet type=0x{:04x}", packet.packet_type)
            raise ssl.SSLError(f"unexpected handshake packet 0x{packet.packet_type:04x}")
        LOGGER.info("business handshake succeeded {}:{}", host, port)
        response = decode(packet.payload)
        if isinstance(response, dict):
            response["_server_fingerprint"] = actual_server_fingerprint
        return Connection(tls_socket, client_public_key), response
    except Exception:
        LOGGER.exception("business handshake failed {}:{}", host, port)
        tls_socket.close()
        raise


def connect_with_store(
    host: str,
    port: int,
    certfile: str,
    keyfile: str,
    expected_server_fingerprint: bytes,
    state_store: StateStore,
    timeout: float = 10.0,
) -> tuple[Connection, dict]:
    """Connect using the persisted config snapshot and update it atomically."""
    connection, response = connect(
        host,
        port,
        certfile,
        keyfile,
        expected_server_fingerprint,
        config_id=state_store.config_id(),
        timeout=timeout,
    )
    config = response.get("config")
    if isinstance(config, dict):
        config_id = config.get("config_id")
        if isinstance(config_id, str) and config_id:
            state_store.set_config_id(config_id)
    return connection, response


def _load_private_key(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as stream:
        key = serialization.load_pem_private_key(stream.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("client key is not Ed25519")
    return key
