"""Atomic storage for the trusted server and current config snapshot ID."""

from __future__ import annotations

import json
import base64
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional, Union
from loguru import logger


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class StateStore:
    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._state: dict[str, Any] = self._empty_state()
        self.load()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": 1,
            "server": None,
            "servers": [],
            "config_id": None,
            "listener_mode": "auto",
            "listener_port": 39004,
            # False until first launch applies the Windows default Run entry.
            "autostart_default_applied": False,
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError("unsupported state version")
                self._state = value
            except FileNotFoundError:
                # First launch: empty state is normal; create a fresh file below.
                self._state = self._empty_state()
                logger.info("state file missing; starting with empty client state path={}", self.path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.error("state file could not be loaded path={} error={}", self.path, exc)
                backup = self.path.with_suffix(self.path.suffix + ".bak")
                try:
                    value = json.loads(backup.read_text(encoding="utf-8"))
                    if not isinstance(value, dict) or value.get("version") != 1:
                        raise ValueError("invalid backup state")
                    self._state = value
                    logger.warning("restored client state from backup path={}", backup)
                except FileNotFoundError:
                    logger.info("client state backup missing; using empty state path={}", backup)
                    self._state = self._empty_state()
                except (OSError, ValueError, json.JSONDecodeError) as backup_exc:
                    logger.error("client state backup unavailable path={} error={}", backup, backup_exc)
                    self._state = self._empty_state()
            # Older development builds persisted message IDs. They are deliberately
            # discarded: delivery deduplication is process-local by design.
            self._state.pop("messages", None)
            # Admin disconnect is runtime policy; do not persist a local pause marker.
            self._state.pop("suspended_servers", None)
            servers = self._state.get("servers")
            if not isinstance(servers, list):
                servers = []
            legacy = self._state.get("server")
            if isinstance(legacy, dict) and legacy not in servers:
                servers.insert(0, legacy)
            self._state["servers"] = [item for item in servers if isinstance(item, dict)]
            mode = self._state.get("listener_mode")
            if mode not in ("auto", "pull", "listen"):
                self._state["listener_mode"] = "auto"
            try:
                port = int(self._state.get("listener_port", 39004))
            except (TypeError, ValueError):
                port = 39004
            self._state["listener_port"] = port if 1 <= port <= 65535 else 39004
            self._state["autostart_default_applied"] = bool(
                self._state.get("autostart_default_applied", False)
            )
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def set_server(
        self,
        *,
        host: str,
        tcp_port: int,
        http_port: int,
        fingerprint: bytes,
        make_current: bool = True,
        auto_connect: Optional[bool] = None,
        config_id: Optional[str] = None,
    ) -> None:
        """Add or update one saved server. Multiple distinct host:tcp_port entries are kept.

        Connecting sets auto_connect=True so startup reconnects that endpoint.
        """
        if not host or not 1 <= int(tcp_port) <= 65535:
            raise ValueError("invalid server endpoint")
        if len(fingerprint) != 32:
            raise ValueError("server fingerprint must be SHA-256")
        host = str(host).strip()
        tcp_port = int(tcp_port)
        http_port = int(http_port)
        with self._lock:
            servers = self._state.setdefault("servers", [])
            if not isinstance(servers, list):
                servers = []
                self._state["servers"] = servers
            existing_index = next(
                (
                    index
                    for index, item in enumerate(servers)
                    if isinstance(item, dict)
                    and str(item.get("host", "")).strip() == host
                    and int(item.get("tcp_port", 0)) == tcp_port
                ),
                None,
            )
            previous = servers[existing_index] if existing_index is not None else None
            # Re-saving the same endpoint must not wipe a real trusted fingerprint
            # with an all-zero placeholder from manual add / incomplete scan.
            fingerprint_b64 = _b64(fingerprint)
            previous_auto = True
            previous_config_id = None
            if previous is not None:
                previous_auto = bool(previous.get("auto_connect", True))
                previous_config_id = previous.get("config_id")
                previous_fp = previous.get("fingerprint")
                if not any(fingerprint) and isinstance(previous_fp, str) and previous_fp:
                    try:
                        if any(base64.b64decode(previous_fp, validate=True)):
                            fingerprint_b64 = previous_fp
                            if int(previous.get("http_port", 0)) > 0:
                                http_port = int(previous.get("http_port", http_port))
                    except (ValueError, TypeError):
                        pass
            resolved_auto = previous_auto if auto_connect is None else bool(auto_connect)
            resolved_config = previous_config_id
            if config_id is not None:
                resolved_config = config_id if config_id else None
            server = {
                "host": host,
                "tcp_port": tcp_port,
                "http_port": http_port,
                "fingerprint": fingerprint_b64,
                "auto_connect": resolved_auto,
            }
            if resolved_config:
                server["config_id"] = resolved_config
            if existing_index is not None:
                servers.pop(existing_index)
            servers.insert(0, server)
            if make_current or self._state.get("server") is None:
                self._state["server"] = server
            elif isinstance(self._state.get("server"), dict):
                current = self._state["server"]
                if (
                    str(current.get("host", "")).strip() == host
                    and int(current.get("tcp_port", 0)) == tcp_port
                ):
                    self._state["server"] = server
            self._commit_locked()

    def set_server_config_id(self, *, host: str, tcp_port: int, config_id: Optional[str]) -> None:
        """Persist the last known config snapshot ID for one saved endpoint."""
        host = str(host).strip()
        tcp_port = int(tcp_port)
        with self._lock:
            servers = self._state.get("servers", [])
            if not isinstance(servers, list):
                return
            for item in servers:
                if not isinstance(item, dict):
                    continue
                if str(item.get("host", "")).strip() == host and int(item.get("tcp_port", 0)) == tcp_port:
                    if config_id:
                        item["config_id"] = str(config_id)
                    else:
                        item.pop("config_id", None)
                    if isinstance(self._state.get("server"), dict):
                        current = self._state["server"]
                        if (
                            str(current.get("host", "")).strip() == host
                            and int(current.get("tcp_port", 0)) == tcp_port
                        ):
                            if config_id:
                                current["config_id"] = str(config_id)
                            else:
                                current.pop("config_id", None)
                    self._commit_locked()
                    return

    def server(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._state.get("server")
            return dict(value) if isinstance(value, dict) else None

    def servers(self) -> list[Dict[str, Any]]:
        with self._lock:
            values = self._state.get("servers", [])
            return [dict(value) for value in values if isinstance(value, dict)]

    def trusts_server_fingerprint(self, fingerprint: bytes) -> bool:
        """判断指定 SPKI SHA-256 是否存在于任一已保存服务端信任项。"""
        if len(fingerprint) != 32:
            return False
        with self._lock:
            for server in self._state.get("servers", []):
                if not isinstance(server, dict):
                    continue
                try:
                    candidate = base64.b64decode(server.get("fingerprint", ""), validate=True)
                except (ValueError, TypeError):
                    continue
                if candidate == fingerprint:
                    return True
        return False

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

    def listener_settings(self) -> tuple[str, int]:
        with self._lock:
            return str(self._state.get("listener_mode", "auto")), int(self._state.get("listener_port", 39004))

    def set_listener_settings(self, mode: str, port: int) -> None:
        if mode not in ("auto", "pull", "listen") or not 1 <= int(port) <= 65535:
            raise ValueError("invalid listener settings")
        with self._lock:
            self._state["listener_mode"] = mode
            self._state["listener_port"] = int(port)
            self._commit_locked()

    def autostart_default_applied(self) -> bool:
        with self._lock:
            return bool(self._state.get("autostart_default_applied", False))

    def set_autostart_default_applied(self, applied: bool = True) -> None:
        with self._lock:
            self._state["autostart_default_applied"] = bool(applied)
            self._commit_locked()

    def _commit_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._state, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                try:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
                except OSError as exc:
                    logger.warning("could not backup client state path={} error={}", self.path, exc)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
