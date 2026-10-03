"""In-memory delivery primitives shared by the connection and UI layers."""

from __future__ import annotations

import heapq
import re
import time
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    config_id: str
    issued_at: int
    heartbeat_interval: int = Field(gt=0)
    heartbeat_timeout: int = Field(gt=0)
    message_ttl: int = Field(gt=0)
    max_speech_depth: int = Field(gt=0, le=64)
    max_repeat_expansion: int = Field(gt=0, le=10000)
    ack_timeout_seconds: int = Field(default=300, gt=0)
    display_position: str = Field(default="center", alias="default_display_position")
    display_duration_ratio: float = Field(default=0.2, ge=0, le=60, alias="default_display_duration_ratio")

    @field_validator("config_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]{13,}[.][0-9]{1,10}", value):
            raise ValueError("invalid config ID")
        return value

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "ConfigSnapshot":
        """Go time.Duration values are BSON int64 nanoseconds."""
        normalized = dict(value)
        for name in ("heartbeat_interval", "heartbeat_timeout", "message_ttl"):
            raw = normalized.get(name)
            if isinstance(raw, int) and raw > 100000:
                normalized[name] = max(1, raw // 1_000_000_000)
        return cls.model_validate(normalized)


class MessageDeduplicator:
    """Process-local deduplication, scoped by the authenticated server key."""

    def __init__(self) -> None:
        self._seen: set[tuple[bytes, str]] = set()

    def observe(self, server_id: bytes, message_id: str) -> bool:
        key = (bytes(server_id), message_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


class PriorityQueue:
    """Lower numeric priorities play first; equal priorities use queue_seq FIFO."""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, Any]] = []
        self._counter = 0
        self._cancelled: set[str] = set()

    def put(self, message: Any) -> None:
        self._counter += 1
        heapq.heappush(
            self._heap,
            (int(message.priority), int(message.queue_seq), self._counter, message),
        )

    def get(self) -> Optional[Any]:
        while self._heap:
            _, _, _, message = heapq.heappop(self._heap)
            if message.message_id not in self._cancelled:
                return message
        return None

    def withdraw(self, message_id: str) -> bool:
        self._cancelled.add(message_id)
        return any(item[3].message_id == message_id for item in self._heap)

    def __len__(self) -> int:
        return sum(item[3].message_id not in self._cancelled for item in self._heap)


class ReconnectBackoff:
    """The specified retry schedule followed by a five-minute probe interval."""

    delays = (1, 1, 1, 2, 4, 8, 16, 32, 64, 128)

    def __init__(self) -> None:
        self.attempt = 0

    def next_delay(self) -> int:
        index = min(self.attempt, len(self.delays) - 1)
        self.attempt += 1
        if self.attempt > len(self.delays):
            return 300
        return self.delays[index]

    def reset(self) -> None:
        self.attempt = 0
