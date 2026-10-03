"""BSON adapter backed by PyMongo's official BSON implementation."""

from __future__ import annotations

from typing import Any

from bson import BSON
from bson.errors import InvalidBSON, InvalidDocument


class BSONError(ValueError):
    pass


def encode(document: dict[str, Any]) -> bytes:
    try:
        return BSON.encode(document)
    except (InvalidDocument, TypeError, ValueError) as exc:
        raise BSONError(str(exc)) from exc


def decode(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise BSONError("BSON data must be bytes")
    try:
        value = BSON(data).decode()
    except (InvalidBSON, TypeError, ValueError) as exc:
        raise BSONError(str(exc)) from exc
    if not isinstance(value, dict):
        raise BSONError("BSON root must be a document")
    return value
