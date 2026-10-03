"""MKCB packet framing shared by discovery and the TLS stream."""

from __future__ import annotations

import io
import struct
from typing import Optional
from pydantic import BaseModel

MAGIC = b"MKCB"
HEADER_SIZE = 16
MAX_PAYLOAD_SIZE = 1 << 20
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0


class PacketError(ValueError):
    pass


class Packet(BaseModel):
    version_major: int
    version_minor: int
    packet_type: int
    sequence: int
    flags: int
    payload: bytes = b""

    class Config:
        frozen = True

    def __init__(self, version_major: Optional[int] = None, version_minor: Optional[int] = None,
                 packet_type: Optional[int] = None, sequence: Optional[int] = None,
                 flags: Optional[int] = None, payload: bytes = b"", **data: object) -> None:
        if version_major is not None:
            data.update(version_major=version_major, version_minor=version_minor,
                       packet_type=packet_type, sequence=sequence, flags=flags, payload=payload)
        super().__init__(**data)

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD_SIZE:
            raise PacketError("packet payload exceeds limit")
        length = HEADER_SIZE + len(self.payload)
        return b"".join(
            (
                MAGIC,
                bytes((self.version_major, self.version_minor)),
                struct.pack(">H I H H", self.packet_type, length, self.sequence, self.flags),
                self.payload,
            )
        )

    @classmethod
    def decode(cls, data: bytes) -> "Packet":
        packet, consumed = decode_from(data)
        if consumed != len(data):
            raise PacketError("trailing bytes after packet")
        return packet


def decode_from(data: bytes) -> tuple[Packet, int]:
    if len(data) < HEADER_SIZE:
        raise PacketError("incomplete packet header")
    if data[:4] != MAGIC:
        raise PacketError("invalid MKCB magic")
    version_major, version_minor = data[4], data[5]
    packet_type, length, sequence, flags = struct.unpack(">H I H H", data[6:16])
    if length < HEADER_SIZE:
        raise PacketError("invalid packet length")
    if length > HEADER_SIZE + MAX_PAYLOAD_SIZE:
        raise PacketError("packet payload exceeds limit")
    if len(data) < length:
        raise PacketError("incomplete packet payload")
    return (
        Packet(version_major, version_minor, packet_type, sequence, flags, data[HEADER_SIZE:length]),
        length,
    )


def read_packet(stream: io.BufferedIOBase) -> Packet:
    header = stream.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE:
        raise EOFError("incomplete packet header")
    if header[:4] != MAGIC:
        raise PacketError("invalid MKCB magic")
    packet_type, length, sequence, flags = struct.unpack(">H I H H", header[6:16])
    if length < HEADER_SIZE:
        raise PacketError("invalid packet length")
    if length > HEADER_SIZE + MAX_PAYLOAD_SIZE:
        raise PacketError("packet payload exceeds limit")
    payload = stream.read(length - HEADER_SIZE)
    if len(payload) != length - HEADER_SIZE:
        raise EOFError("incomplete packet payload")
    return Packet(header[4], header[5], packet_type, sequence, flags, payload)
