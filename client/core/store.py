"""Atomic storage for the trusted server and current config snapshot ID."""

from __future__ import annotations

import json
import base64
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional, Union


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class StateStore:
    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._state: dict[str, Any] = {
            "version": 1,
            "server": None,
            "servers": [],
            "config_id": None,
        }
        self.load()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError("unsupported state version")
                self._state = value
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                self._state = {"version": 1, "server": None, "servers": [], "config_id": None}
            # Older development builds persisted message IDs. They are deliberately
            # discarded: delivery deduplication is process-local by design.
            self._state.pop("messages", None)
            servers = self._state.get("servers")
            if not isinstance(servers, list):
                servers = []
            legacy = self._state.get("server")
            if isinstance(legacy, dict) and legacy not in servers:
                servers.insert(0, legacy)
            self._state["servers"] = [item for item in servers if isinstance(item, dict)]
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def set_server(self, *, host: str, tcp_port: int, http_port: int, fingerprint: bytes) -> None:
        if not host or not 1 <= int(tcp_port) <= 65535:
            raise ValueError("invalid server endpoint")
        if len(fingerprint) != 32:
            raise ValueError("server fingerprint must be SHA-256")
        with self._lock:
            server = {
                "host": host,
                "tcp_port": int(tcp_port),
                "http_port": int(http_port),
                "fingerprint": _b64(fingerprint),
            }
            servers = self._state.setdefault("servers", [])
            servers[:] = [item for item in servers if not (
                isinstance(item, dict)
                and item.get("host") == host
                and int(item.get("tcp_port", 0)) == int(tcp_port)
            )]
            servers.insert(0, server)
            self._state["server"] = server
            self._commit_locked()

    def server(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._state.get("server")
            return dict(value) if isinstance(value, dict) else None

    def servers(self) -> list[Dict[str, Any]]:
        with self._lock:
            values = self._state.get("servers", [])
            return [dict(value) for value in values if isinstance(value, dict)]

    def remove_server(self, *, host: str, tcp_port: int) -> None:
        with self._lock:
            values = self._state.get("servers", [])
            self._state["servers"] = [item for item in values if not (
                isinstance(item, dict)
                and item.get("host") == host
                and int(item.get("tcp_port", 0)) == int(tcp_port)
            )]
            self._state["server"] = self._state["servers"][0] if self._state["servers"] else None
            self._commit_locked()

    def set_config_id(self, config_id: Optional[str]) -> None:
        if config_id is not None and (not isinstance(config_id, str) or not config_id):
            raise ValueError("config ID cannot be empty")
        with self._lock:
            self._state["config_id"] = config_id
            self._commit_locked()

    def config_id(self) -> Optional[str]:
        with self._lock:
            value = self._state.get("config_id")
            return value if isinstance(value, str) and value else None

    def _commit_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._state, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
