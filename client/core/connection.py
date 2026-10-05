"""TLS 1.3 client connection and business handshake."""

from __future__ import annotations

import hashlib
import base64
import os
import select
import socket
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel
from loguru import logger

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .protocol.bson import decode, encode
from .protocol.packet import Packet, read_packet
from .store import StateStore
from ..version import VERSION_STR


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
CLIENT_DISCONNECT = 0x0205
UPDATE_AVAILABLE = 0x0207
UPDATE_DOWNLOAD_REQ = 0x0208
UPDATE_DOWNLOAD_RESP = 0x0209
CLIENT_SESSION_END = 0x020A  # client→server intentional goodbye (user_exit / update / …)
ERROR = 0x7F00


class ClientNotApprovedError(ConnectionError):
    """The server requires management approval before accepting this client."""


class ServerFingerprintMismatch(ConnectionError):
    """The endpoint presented a different SPKI fingerprint than the trusted one."""

    def __init__(self, actual_fingerprint: bytes) -> None:
        super().__init__("server certificate fingerprint mismatch")
        self.actual_fingerprint = bytes(actual_fingerprint)


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
    # Multi-server clients route acks back to the connection that delivered this message.
    source_host: Optional[str] = None
    source_tcp_port: Optional[int] = None

    class Config:
        frozen = True


class IncomingWithdrawal(BaseModel):
    message_id: str
    message: str = "消息已撤回"
    content: str = ""
    display_duration_ratio: Optional[float] = None
    source_host: Optional[str] = None
    source_tcp_port: Optional[int] = None

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

    def report_session_end(self, reason: str = "user_exit", detail: str = "") -> None:
        """Tell the server this disconnect is intentional before closing the socket.

        reason: user_exit (developer Exit) | update (hand off to updater) | admin
        """
        payload: dict[str, Any] = {"reason": str(reason or "user_exit").strip() or "user_exit"}
        if detail:
            payload["detail"] = str(detail)[:256]
        self.send(Packet(1, 0, CLIENT_SESSION_END, 0, 0, encode(payload)))


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


def decode_update_available(packet: Packet) -> dict[str, Any]:
    """Validate the server's update notification before exposing it to the UI."""
    if packet.packet_type != UPDATE_AVAILABLE:
        raise ValueError("packet is not an update notification")
    document = decode(packet.payload)
    if not isinstance(document, dict):
        raise ValueError("update notification must be a BSON document")
    result = {key: str(document.get(key, "")) for key in (
        "component", "version", "client_version", "updater_version", "platform", "payload_format", "sha256"
    )}
    raw_components = document.get("components")
    components: list[str] = []
    if isinstance(raw_components, (list, tuple)):
        for item in raw_components:
            name = str(item).strip().lower()
            if name in ("client", "updater") and name not in components:
                components.append(name)
    if not components:
        legacy = result["component"].strip().lower()
        if legacy == "bundle":
            components = ["updater", "client"]
        elif legacy in ("client", "updater"):
            components = [legacy]
    # Canonical install order: updater first, then client.
    rank = {"updater": 0, "client": 1}
    components.sort(key=lambda name: rank.get(name, 99))
    if not components:
        raise ValueError("update notification components is invalid")
    primary = components[0] if len(components) == 1 else "bundle"
    result["component"] = primary
    result["components"] = components
    package = document.get("package", b"")
    if package is None:
        package = b""
    download_token = str(document.get("download_token", "") or "")
    # Legacy HTTP download_url kept only for older servers; prefer download_token + TLS.
    download_url = str(document.get("download_url", "") or "")
    legacy_release_url = str(document.get("release_url", "") or "")
    if download_url:
        if not (download_url.startswith("https://") or download_url.startswith("http://")):
            raise ValueError("update download URL must use HTTP or HTTPS")
    if legacy_release_url and not legacy_release_url.startswith("https://"):
        raise ValueError("legacy update URL must use HTTPS")
    if not result["version"]:
        raise ValueError("update notification version is invalid")
    if result["platform"] != "windows-amd64" or result["payload_format"] != "files-v1":
        raise ValueError("update notification platform or payload format is unsupported")
    if not isinstance(package, (bytes, bytearray)):
        raise ValueError("update notification package field is invalid")
    if len(result["sha256"]) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in result["sha256"]):
        raise ValueError("update notification SHA-256 is invalid")
    if not package and not download_token and not download_url and not legacy_release_url:
        raise ValueError("update notification is missing package bytes and download token")
    seq_value = document.get("seq", 0)
    try:
        result["seq"] = int(seq_value) if seq_value is not None else 0
    except (TypeError, ValueError) as exc:
        raise ValueError("update notification seq is invalid") from exc
    if result["seq"] < 0:
        raise ValueError("update notification seq is invalid")
    expires_value = document.get("expires_at", 0)
    try:
        result["expires_at"] = int(expires_value) if expires_value else 0
    except (TypeError, ValueError):
        result["expires_at"] = 0
    result["force"] = bool(document.get("force", False))
    if download_token:
        result["download_token"] = download_token
    if download_url:
        result["download_url"] = download_url
    if package:
        result["package"] = bytes(package)
    if legacy_release_url:
        result["release_url"] = legacy_release_url
        result["signature"] = str(document.get("signature", ""))
    return result


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
        display_duration_ratio=_optional_float(document.get("display_duration_ratio")),
    )


def _fingerprint_text(value: bytes) -> str:
    return "SHA256:" + base64.b64encode(value).decode("ascii").rstrip("=")


def _fingerprint_bytes(value: Union[bytes, str]) -> bytes:
    """统一解析服务端指纹，兼容 SHA256:Base64 与 64 位十六进制。"""
    if isinstance(value, bytes):
        return value
    encoded = value.strip()
    if encoded.lower().startswith("sha256:"):
        encoded = encoded[len("sha256:"):]
    if len(encoded) == 64:
        try:
            return bytes.fromhex(encoded)
        except ValueError:
            pass
    try:
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("server fingerprint must be SHA-256 Base64 or hex") from exc


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
    listener_mode: str = "pull",
    listener_port: int = 0,
    reconnect_probe: bool = False,
    manual_connect: bool = False,
    update_download: bool = False,
) -> tuple[Connection, dict]:
    if isinstance(expected_server_fingerprint, str):
        expected_server_fingerprint = _fingerprint_bytes(expected_server_fingerprint)
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
    purpose = "update-download" if update_download else "session"
    LOGGER.info("TCP/TLS connection {}:{} purpose={}", host, port, purpose)
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
            raise ServerFingerprintMismatch(actual_server_fingerprint)
        request = {
            "request_id": os.urandom(16),
            "client_public_key": client_public_key,
            "protocol_major": 1,
            "protocol_minor": 0,
            "client_version": VERSION_STR,
            "listener_mode": listener_mode,
            "reconnect_probe": reconnect_probe,
            "manual_connect": manual_connect,
        }
        if update_download:
            request["update_download"] = True
        if listener_port > 0:
            request["listener_port"] = int(listener_port)
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
        LOGGER.info("business handshake succeeded {}:{} purpose={}", host, port, purpose)
        response = decode(packet.payload)
        if isinstance(response, dict):
            response["_server_fingerprint"] = actual_server_fingerprint
            response["_server_public_key"] = raw_public_key
        return Connection(tls_socket, client_public_key), response
    except Exception:
        LOGGER.exception("business handshake failed {}:{}", host, port)
        tls_socket.close()
        raise


def download_update_package_tls(
    host: str,
    port: int,
    certfile: str,
    keyfile: str,
    expected_server_fingerprint: Union[bytes, str],
    sha256_hex: str,
    download_token: str,
    destination: "os.PathLike[str] | str",
    timeout: float = 30.0,
    *,
    idle_warn_seconds: float = 5.0,
    idle_fail_seconds: float = 120.0,
    progress_log_bytes: int = 1 << 20,
) -> None:
    """Open a short-lived TLS session and stream an update package to disk.

    Uses ConnectReq.update_download=true so the server does not register a hub session.
    Chunks arrive as UpdateDownloadResp BSON documents (offset/total_size/data/done).

    Read deadline is short and polled so stalled transfers emit WARN every
    ``idle_warn_seconds`` without data (default 5s) instead of hanging silently.
    """
    from pathlib import Path

    digest = str(sha256_hex).strip().lower()
    token = str(download_token or "").strip()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("update download sha256 is invalid")
    if not token:
        raise ValueError("update download token is required")
    dest = Path(destination)
    name = dest.name.lower()
    if not (name.endswith(".tar.zst") or name.endswith(".tar.zstd")):
        stem = dest.name
        for suffix in (".zst", ".zstd", ".zip", ".tar"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        dest = dest.with_name(stem + ".tar.zst")
    temporary = dest.with_name(dest.name + ".partial")
    idle_warn = max(1.0, float(idle_warn_seconds))
    idle_fail = max(idle_warn, float(idle_fail_seconds))
    progress_every = max(64 << 10, int(progress_log_bytes))
    # Full-packet read window: large enough for one UpdateDownloadResp (~256 KiB BSON).
    packet_read_timeout = max(float(timeout), 60.0)

    LOGGER.info(
        "update download begin host={} port={} sha256={} dest={}",
        host,
        port,
        digest,
        dest,
    )
    connection, _response = connect(
        host,
        port,
        certfile,
        keyfile,
        expected_server_fingerprint,
        timeout=timeout,
        update_download=True,
        manual_connect=True,
    )
    received = 0
    total_size = -1
    chunks = 0
    started_at = time.monotonic()
    last_data_at = started_at
    last_idle_warn_at = started_at
    last_progress_log_at = started_at
    last_progress_bytes = 0
    try:
        # Idle is detected with select() between complete packets. Socket timeout only
        # applies while reading one full frame so a mid-payload timeout cannot desync MKCB.
        connection.socket.settimeout(packet_read_timeout)
        req_payload = encode({"sha256": digest, "token": token})
        connection.send(Packet(1, 0, UPDATE_DOWNLOAD_REQ, 1, 0, req_payload))
        LOGGER.info(
            "update download request sent host={} port={} sha256={} waiting_for_first_chunk",
            host,
            port,
            digest,
        )
        with temporary.open("wb") as out:
            while True:
                now = time.monotonic()
                idle_for = now - last_data_at
                if idle_for >= idle_fail:
                    raise TimeoutError(
                        "update download stalled: no data for {:.1f}s "
                        "(received={} total={} chunks={})".format(
                            idle_for,
                            received,
                            total_size if total_size >= 0 else "unknown",
                            chunks,
                        )
                    )
                if idle_for >= idle_warn and (now - last_idle_warn_at) >= idle_warn:
                    last_idle_warn_at = now
                    LOGGER.warning(
                        "update download idle no data for {:.1f}s host={} port={} "
                        "received={} total={} chunks={} sha256={} partial={}",
                        idle_for,
                        host,
                        port,
                        received,
                        total_size if total_size >= 0 else "unknown",
                        chunks,
                        digest,
                        temporary,
                    )
                # Wait for the next complete packet without starting a mid-frame read.
                # TLS may already hold decrypted bytes (pending) while the OS FD is idle.
                wait_budget = min(idle_warn, max(0.1, idle_fail - idle_for))
                pending = 0
                try:
                    pending = int(connection.socket.pending())  # type: ignore[attr-defined]
                except (AttributeError, NotImplementedError, OSError, ValueError):
                    pending = 0
                if pending <= 0:
                    try:
                        readable, _, _ = select.select([connection.socket], [], [], wait_budget)
                    except (TypeError, ValueError, OSError):
                        # Fall through to a blocking receive when select is unsupported.
                        readable = [connection.socket]
                    if not readable:
                        continue
                try:
                    packet = connection.receive()
                except (socket.timeout, TimeoutError) as exc:
                    # select/pending said data may be ready, but a full MKCB frame did not arrive.
                    LOGGER.warning(
                        "update download packet read timeout host={} port={} received={} "
                        "total={} chunks={} sha256={} error={}",
                        host,
                        port,
                        received,
                        total_size if total_size >= 0 else "unknown",
                        chunks,
                        digest,
                        exc,
                    )
                    raise TimeoutError(
                        "update download packet read timed out after {:.1f}s idle "
                        "(received={} total={} chunks={})".format(
                            time.monotonic() - last_data_at,
                            received,
                            total_size if total_size >= 0 else "unknown",
                            chunks,
                        )
                    ) from exc
                except (OSError, EOFError, ssl.SSLError) as exc:
                    LOGGER.warning(
                        "update download socket error host={} port={} received={} total={} "
                        "chunks={} sha256={} error={}",
                        host,
                        port,
                        received,
                        total_size if total_size >= 0 else "unknown",
                        chunks,
                        digest,
                        exc,
                    )
                    raise
                if packet.packet_type == ERROR:
                    document = decode(packet.payload)
                    message = str(document.get("message") or document.get("code") or "server_error")
                    LOGGER.warning(
                        "update download server error host={} port={} sha256={} message={}",
                        host,
                        port,
                        digest,
                        message,
                    )
                    raise RuntimeError(message)
                if packet.packet_type != UPDATE_DOWNLOAD_RESP:
                    LOGGER.warning(
                        "update download unexpected packet type=0x{:04x} host={} port={} sha256={}",
                        packet.packet_type,
                        host,
                        port,
                        digest,
                    )
                    raise RuntimeError(f"unexpected update download packet 0x{packet.packet_type:04x}")
                document = decode(packet.payload)
                if not isinstance(document, dict):
                    raise RuntimeError("malformed update download response")
                error = str(document.get("error") or "").strip()
                if error:
                    LOGGER.warning(
                        "update download response error host={} port={} sha256={} error={}",
                        host,
                        port,
                        digest,
                        error,
                    )
                    raise RuntimeError(error)
                resp_sha = str(document.get("sha256") or "").lower()
                if resp_sha and resp_sha != digest:
                    raise RuntimeError("update download sha256 mismatch")
                try:
                    offset = int(document.get("offset") or 0)
                    chunk_total = int(document.get("total_size") or 0)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("malformed update download offsets") from exc
                if total_size < 0 and chunk_total > 0:
                    total_size = chunk_total
                    LOGGER.info(
                        "update download first chunk host={} port={} total_size={} sha256={}",
                        host,
                        port,
                        total_size,
                        digest,
                    )
                data = document.get("data") or b""
                if data is None:
                    data = b""
                if not isinstance(data, (bytes, bytearray)):
                    raise RuntimeError("update download chunk is not bytes")
                if data:
                    if offset != received:
                        raise RuntimeError(f"update download offset mismatch got={offset} want={received}")
                    out.write(bytes(data))
                    # Flush so a stalled transfer leaves an observable .partial on disk.
                    out.flush()
                    received += len(data)
                    chunks += 1
                    last_data_at = time.monotonic()
                    last_idle_warn_at = last_data_at
                    # Progress: every progress_every bytes, or at least every idle_warn while moving.
                    if (
                        received - last_progress_bytes >= progress_every
                        or (last_data_at - last_progress_log_at) >= idle_warn
                        or chunks == 1
                    ):
                        elapsed = max(0.001, last_data_at - started_at)
                        rate = received / elapsed
                        pct = (100.0 * received / total_size) if total_size > 0 else -1.0
                        LOGGER.info(
                            "update download progress host={} port={} received={} total={} "
                            "pct={:.1f} rate_kib_s={:.1f} chunks={} sha256={}",
                            host,
                            port,
                            received,
                            total_size if total_size >= 0 else "unknown",
                            pct,
                            rate / 1024.0,
                            chunks,
                            digest,
                        )
                        last_progress_bytes = received
                        last_progress_log_at = last_data_at
                if bool(document.get("done")):
                    break
        if total_size >= 0 and received != total_size:
            raise RuntimeError(f"update download size mismatch got={received} want={total_size}")
        with temporary.open("rb") as probe:
            magic = probe.read(4)
        if magic != b"\x28\xb5\x2f\xfd":
            raise RuntimeError("downloaded update package is not zstd-compressed (.tar.zst expected)")
        temporary.replace(dest)
        elapsed = max(0.001, time.monotonic() - started_at)
        LOGGER.info(
            "update package downloaded via TLS host={} port={} path={} bytes={} "
            "chunks={} elapsed_s={:.2f} rate_kib_s={:.1f} sha256={}",
            host,
            port,
            dest,
            received,
            chunks,
            elapsed,
            received / elapsed / 1024.0,
            digest,
        )
    except Exception as exc:
        partial_size = None
        try:
            if temporary.is_file():
                partial_size = temporary.stat().st_size
        except OSError:
            partial_size = None
        LOGGER.warning(
            "update download failed host={} port={} received={} total={} chunks={} "
            "partial={} partial_bytes={} sha256={} error={}",
            host,
            port,
            received,
            total_size if total_size >= 0 else "unknown",
            chunks,
            temporary if temporary.exists() else None,
            partial_size,
            digest,
            exc,
        )
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        try:
            connection.close()
        except OSError:
            pass


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
