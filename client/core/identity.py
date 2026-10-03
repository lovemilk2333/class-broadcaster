"""Ed25519 identity and installation-bound encrypted private key storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from pydantic import BaseModel

from argon2.low_level import Type, hash_secret_raw
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

FORMAT_VERSION = 2
KDF_MEMORY_KIB = 64 * 1024
KDF_ITERATIONS = 3
KDF_PARALLELISM = 2


class Identity(BaseModel):
    private_key: Ed25519PrivateKey
    certificate_pem: bytes

    class Config:
        frozen = True
        arbitrary_types_allowed = True

    def __init__(self, private_key=None, certificate_pem: bytes = b"", **data: object) -> None:
        if private_key is not None:
            data.update(private_key=private_key, certificate_pem=certificate_pem)
        super().__init__(**data)

    @property
    def public_key(self) -> bytes:
        return self.private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key_fingerprint(public_key: bytes) -> bytes:
    key = Ed25519PublicKey.from_public_bytes(public_key) if len(public_key) == 32 else None
    if key is not None:
        public_key = key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    return hashlib.sha256(public_key).digest()


def derive_password(install_timestamp_ms: int, client_id: bytes, cpuid: str, device_id: str) -> bytes:
    if len(client_id) != 32:
        raise ValueError("client_id must be a 32-byte SHA-256 fingerprint")
    def field(value: str) -> bytes:
        return value.strip().lower().encode("utf-8")
    return b"MKCB-KDF-v1\x00" + install_timestamp_ms.to_bytes(8, "big", signed=False) + client_id + field(cpuid) + b"\x00" + field(device_id)


def _key(password: bytes, salt: bytes, memory_kib: int = KDF_MEMORY_KIB, iterations: int = KDF_ITERATIONS, parallelism: int = KDF_PARALLELISM) -> bytes:
    return hash_secret_raw(password, salt, iterations, memory_kib, parallelism, 32, Type.ID)


def encrypt_private_key(private_key: Ed25519PrivateKey, install_timestamp_ms: int, cpuid: str, device_id: str) -> bytes:
    public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    client_id = public_key_fingerprint(public_key)
    salt, nonce = os.urandom(16), os.urandom(12)
    password = derive_password(install_timestamp_ms, client_id, cpuid, device_id)
    key = _key(password, salt)
    plaintext = private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    associated_data = b"MKCB-IDENTITY-v1"
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    document = {
        "format_version": FORMAT_VERSION, "kdf": "argon2id", "memory_kib": KDF_MEMORY_KIB,
        "iterations": KDF_ITERATIONS, "parallelism": KDF_PARALLELISM,
        "install_timestamp_ms": install_timestamp_ms, "client_id": _b64(client_id),
        "salt": _b64(salt), "nonce": _b64(nonce), "ciphertext": _b64(ciphertext),
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def decrypt_private_key(blob: bytes, cpuid: str, device_id: str) -> Ed25519PrivateKey:
    try:
        document = json.loads(blob)
        format_version = int(document["format_version"])
        if format_version not in (1, FORMAT_VERSION) or document["kdf"] != "argon2id":
            raise ValueError("unsupported identity format")
        stored_id = _unb64(document["client_id"])
        salt, nonce, ciphertext = _unb64(document["salt"]), _unb64(document["nonce"]), _unb64(document["ciphertext"])
        # Format 1 stored the raw public key. It is accepted once for upgrade
        # compatibility; newly written identities contain only the SHA-256 ID.
        client_id = stored_id if format_version == 1 else stored_id
        password = derive_password(int(document["install_timestamp_ms"]), client_id, cpuid, device_id)
        key = _key(password, salt, int(document["memory_kib"]), int(document["iterations"]), int(document["parallelism"]))
        raw = AESGCM(key).decrypt(nonce, ciphertext, b"MKCB-IDENTITY-v1")
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
        actual = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if format_version == 1 and actual != stored_id:
            raise ValueError("identity public key mismatch")
        if format_version == FORMAT_VERSION and public_key_fingerprint(actual) != stored_id:
            raise ValueError("identity public key mismatch")
        return private_key
    except Exception as exc:
        raise ValueError("unable to decrypt client identity") from exc


def generate_identity(common_name: str = "MKCB client") -> Identity:
    private_key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(private_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650)).sign(private_key, None)
    return Identity(private_key, certificate.public_bytes(serialization.Encoding.PEM))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)
