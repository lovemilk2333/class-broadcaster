"""Unified atomic client persistence in ``data/state.json``.

All durable local prefs live here under distinct keys (servers, TTS, applied
updates, VC++ prompt, log trim threshold, updater log offset, …). Legacy
sidecar files are migrated once on load and then removed.
"""

from __future__ import annotations

import json
import base64
import os
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, Optional, Union
from loguru import logger


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


_REGISTRY_LOCK = RLock()
_REGISTRY: dict[str, "StateStore"] = {}

_MAX_APPLIED_DIGESTS = 64


def _normalize_digest(digest: object) -> str:
    value = str(digest or "").strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        return ""
    return value


def _normalize_digest_list(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        digest = _normalize_digest(item)
        if not digest or digest in seen:
            continue
        seen.add(digest)
        ordered.append(digest)
    return ordered


class StateStore:
    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = Path(path)
        self.data_dir = self.path.parent
        self._lock = RLock()
        self._state: dict[str, Any] = self._empty_state()
        self.load()

    # Kept for callers that still reference historical constants; rendering uses core.fontsize
    # (built-in default is 25vh; these are absolute-px fallbacks / admin px band).
    DEFAULT_MESSAGE_FONT_SIZE = 38
    MIN_MESSAGE_FONT_SIZE = 16
    MAX_MESSAGE_FONT_SIZE = 120

    @classmethod
    def for_data_dir(cls, data_dir: Union[str, os.PathLike]) -> "StateStore":
        """Return the shared store for ``data_dir`` (one instance per absolute path)."""
        root = Path(data_dir).resolve()
        key = str(root)
        with _REGISTRY_LOCK:
            existing = _REGISTRY.get(key)
            if existing is not None:
                return existing
            store = cls(root / "state.json")
            _REGISTRY[key] = store
            return store

    @classmethod
    def clear_registry(cls) -> None:
        """Drop cached instances (tests only)."""
        with _REGISTRY_LOCK:
            _REGISTRY.clear()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        from .client_log import DEFAULT_LOG_MAX_BYTES

        return {
            "version": 1,
            "server": None,
            "servers": [],
            "config_id": None,
            "listener_mode": "auto",
            "listener_port": 39004,
            # False until first launch applies the Windows default Run entry.
            "autostart_default_applied": False,
            # Local client.log trim threshold (bytes); default 2 MiB.
            "log_max_bytes": DEFAULT_LOG_MAX_BYTES,
            # Soft TTS engine preference (24h TTL enforced by EnginePreference).
            "tts": {"engine": None, "saved_at": None},
            # Applied package digests + pending UpdateApplied reports.
            "applied_updates": {"digests": [], "pending_report": []},
            # One-shot Windows VC++ redistributable prompt.
            "vc_redist_prompt": {
                "dismissed": False,
                "dismissed_at": None,
                "opened_download": False,
            },
            # Byte offset into data/updater.log for ClientLog upload.
            "updater_log_offset": 0,
        }

    @classmethod
    def clamp_message_font_size(cls, value: object) -> int:
        from .fontsize import clamp_px

        return clamp_px(value)

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
            from .client_log import DEFAULT_LOG_MAX_BYTES, clamp_log_max_bytes

            self._state["log_max_bytes"] = clamp_log_max_bytes(
                self._state.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES),
                default=DEFAULT_LOG_MAX_BYTES,
            )
            # Font family/size are admin-pushed only; drop any legacy local prefs.
            self._state.pop("message_font_family", None)
            self._state.pop("message_font_size", None)
            self._normalize_unified_keys_locked()
            migrated = self._migrate_legacy_sidecars_locked()
            if migrated:
                self._commit_locked()
            return self.snapshot()

    def _normalize_unified_keys_locked(self) -> None:
        """Ensure nested preference keys exist and have a safe shape."""
        tts = self._state.get("tts")
        if not isinstance(tts, dict):
            tts = {}
        engine = tts.get("engine")
        saved_at = tts.get("saved_at")
        try:
            saved_at_value = float(saved_at) if saved_at is not None else None
        except (TypeError, ValueError):
            saved_at_value = None
        self._state["tts"] = {
            "engine": str(engine) if engine else None,
            "saved_at": saved_at_value,
        }

        applied = self._state.get("applied_updates")
        if not isinstance(applied, dict):
            applied = {}
        digests = _normalize_digest_list(
            applied.get("digests") or applied.get("sha256") or []
        )
        pending = _normalize_digest_list(applied.get("pending_report") or [])
        applied_set = set(digests)
        pending = [item for item in pending if item in applied_set]
        self._state["applied_updates"] = {
            "digests": digests,
            "pending_report": pending,
        }

        prompt = self._state.get("vc_redist_prompt")
        if not isinstance(prompt, dict):
            prompt = {}
        try:
            dismissed_at = (
                float(prompt["dismissed_at"])
                if prompt.get("dismissed_at") is not None
                else None
            )
        except (TypeError, ValueError):
            dismissed_at = None
        self._state["vc_redist_prompt"] = {
            "dismissed": bool(prompt.get("dismissed")),
            "dismissed_at": dismissed_at,
            "opened_download": bool(prompt.get("opened_download")),
        }

        try:
            offset = int(self._state.get("updater_log_offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        self._state["updater_log_offset"] = offset if offset >= 0 else 0

    def _migrate_legacy_sidecars_locked(self) -> bool:
        """Fold legacy JSON/offset files into state.json; delete them on success."""
        changed = False
        data_dir = self.data_dir

        # --- applied_updates.json ---
        applied_path = data_dir / "applied_updates.json"
        if applied_path.is_file():
            try:
                raw = json.loads(applied_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, list):
                digests = _normalize_digest_list(raw)
                pending: list[str] = []
            elif isinstance(raw, dict):
                digests = _normalize_digest_list(
                    raw.get("digests") or raw.get("sha256") or []
                )
                pending = _normalize_digest_list(raw.get("pending_report") or [])
                applied_set = set(digests)
                pending = [item for item in pending if item in applied_set]
            else:
                digests, pending = [], []
            current = self._state.get("applied_updates")
            if not isinstance(current, dict):
                current = {"digests": [], "pending_report": []}
            cur_digests = _normalize_digest_list(current.get("digests"))
            cur_pending = _normalize_digest_list(current.get("pending_report"))
            if not cur_digests and digests:
                self._state["applied_updates"] = {
                    "digests": digests,
                    "pending_report": pending,
                }
                changed = True
            elif digests or pending:
                # Merge: prefer existing state, append missing from sidecar.
                merged = list(cur_digests)
                seen = set(merged)
                for item in digests:
                    if item not in seen:
                        merged.append(item)
                        seen.add(item)
                merged_pending = list(cur_pending)
                pending_seen = set(merged_pending)
                for item in pending:
                    if item in seen and item not in pending_seen:
                        merged_pending.append(item)
                        pending_seen.add(item)
                self._state["applied_updates"] = {
                    "digests": merged[:_MAX_APPLIED_DIGESTS],
                    "pending_report": merged_pending[:_MAX_APPLIED_DIGESTS],
                }
                changed = True
            try:
                applied_path.unlink(missing_ok=True)
                changed = True
            except OSError:
                pass

        # --- tts-engine.json (and historical tts.json) ---
        for name in ("tts-engine.json", "tts.json"):
            tts_path = data_dir / name
            if not tts_path.is_file():
                continue
            try:
                raw = json.loads(tts_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raw = None
            current_tts = self._state.get("tts")
            if not isinstance(current_tts, dict):
                current_tts = {"engine": None, "saved_at": None}
            if (
                isinstance(raw, dict)
                and raw.get("engine")
                and not current_tts.get("engine")
            ):
                try:
                    saved_at = (
                        float(raw["saved_at"])
                        if raw.get("saved_at") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    saved_at = None
                self._state["tts"] = {
                    "engine": str(raw.get("engine")),
                    "saved_at": saved_at,
                }
                changed = True
            try:
                tts_path.unlink(missing_ok=True)
                changed = True
            except OSError:
                pass

        # --- vc-redist-prompt.json ---
        vc_path = data_dir / "vc-redist-prompt.json"
        if vc_path.is_file():
            try:
                raw = json.loads(vc_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raw = None
            current_prompt = self._state.get("vc_redist_prompt")
            if not isinstance(current_prompt, dict):
                current_prompt = {
                    "dismissed": False,
                    "dismissed_at": None,
                    "opened_download": False,
                }
            if isinstance(raw, dict) and raw.get("dismissed") and not current_prompt.get(
                "dismissed"
            ):
                try:
                    dismissed_at = (
                        float(raw["dismissed_at"])
                        if raw.get("dismissed_at") is not None
                        else time.time()
                    )
                except (TypeError, ValueError):
                    dismissed_at = time.time()
                self._state["vc_redist_prompt"] = {
                    "dismissed": True,
                    "dismissed_at": dismissed_at,
                    "opened_download": bool(raw.get("opened_download")),
                }
                changed = True
            try:
                vc_path.unlink(missing_ok=True)
                changed = True
            except OSError:
                pass

        # --- updater.log.offset ---
        offset_path = data_dir / "updater.log.offset"
        if offset_path.is_file():
            try:
                raw_offset = int(offset_path.read_text(encoding="utf-8").strip())
                if raw_offset < 0:
                    raw_offset = 0
            except (OSError, ValueError):
                raw_offset = None
            current_offset = int(self._state.get("updater_log_offset", 0) or 0)
            if raw_offset is not None and current_offset == 0 and raw_offset > 0:
                self._state["updater_log_offset"] = raw_offset
                changed = True
            try:
                offset_path.unlink(missing_ok=True)
                changed = True
            except OSError:
                pass

        return changed

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

    def log_max_bytes(self) -> int:
        from .client_log import DEFAULT_LOG_MAX_BYTES, clamp_log_max_bytes

        with self._lock:
            return clamp_log_max_bytes(
                self._state.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES),
                default=DEFAULT_LOG_MAX_BYTES,
            )

    def set_log_max_bytes(self, value: object) -> int:
        from .client_log import DEFAULT_LOG_MAX_BYTES, clamp_log_max_bytes

        cleaned = clamp_log_max_bytes(value, default=DEFAULT_LOG_MAX_BYTES)
        with self._lock:
            self._state["log_max_bytes"] = cleaned
            self._commit_locked()
            return cleaned

    # --- TTS engine preference ---

    def tts_engine_preference(self) -> tuple[Optional[str], Optional[float]]:
        with self._lock:
            tts = self._state.get("tts")
            if not isinstance(tts, dict):
                return None, None
            engine = tts.get("engine")
            saved_at = tts.get("saved_at")
            try:
                saved_at_value = float(saved_at) if saved_at is not None else None
            except (TypeError, ValueError):
                saved_at_value = None
            return (str(engine) if engine else None), saved_at_value

    def set_tts_engine_preference(self, engine: str, *, saved_at: Optional[float] = None) -> None:
        name = str(engine or "").strip()
        if not name:
            return
        stamp = time.time() if saved_at is None else float(saved_at)
        with self._lock:
            self._state["tts"] = {"engine": name, "saved_at": stamp}
            self._commit_locked()

    def clear_tts_engine_preference(self) -> None:
        with self._lock:
            self._state["tts"] = {"engine": None, "saved_at": None}
            self._commit_locked()

    # --- Applied updates ---

    def applied_update_digests(self) -> set[str]:
        with self._lock:
            applied = self._state.get("applied_updates")
            if not isinstance(applied, dict):
                return set()
            return set(_normalize_digest_list(applied.get("digests")))

    def pending_update_reports(self) -> list[str]:
        with self._lock:
            applied = self._state.get("applied_updates")
            if not isinstance(applied, dict):
                return []
            return list(_normalize_digest_list(applied.get("pending_report")))

    def mark_update_applied(
        self, digest: str, *, max_digests: int = _MAX_APPLIED_DIGESTS
    ) -> None:
        cleaned = _normalize_digest(digest)
        if not cleaned:
            return
        limit = max(1, int(max_digests))
        with self._lock:
            applied = self._state.get("applied_updates")
            if not isinstance(applied, dict):
                applied = {"digests": [], "pending_report": []}
            existing = [
                item
                for item in _normalize_digest_list(applied.get("digests"))
                if item != cleaned
            ]
            ordered = [cleaned, *existing][:limit]
            pending = [
                item
                for item in _normalize_digest_list(applied.get("pending_report"))
                if item != cleaned
            ]
            pending = [cleaned, *pending][:limit]
            self._state["applied_updates"] = {
                "digests": ordered,
                "pending_report": pending,
            }
            self._commit_locked()

    def mark_update_reported(self, digest: str) -> None:
        cleaned = _normalize_digest(digest)
        if not cleaned:
            return
        with self._lock:
            applied = self._state.get("applied_updates")
            if not isinstance(applied, dict):
                return
            digests = _normalize_digest_list(applied.get("digests"))
            pending = [
                item
                for item in _normalize_digest_list(applied.get("pending_report"))
                if item != cleaned
            ]
            self._state["applied_updates"] = {
                "digests": digests,
                "pending_report": pending,
            }
            self._commit_locked()

    # --- VC++ redistributable prompt ---

    def vc_redist_prompt_dismissed(self) -> bool:
        with self._lock:
            prompt = self._state.get("vc_redist_prompt")
            return bool(isinstance(prompt, dict) and prompt.get("dismissed"))

    def mark_vc_redist_prompt_dismissed(self, *, opened_download: bool = False) -> None:
        with self._lock:
            self._state["vc_redist_prompt"] = {
                "dismissed": True,
                "dismissed_at": time.time(),
                "opened_download": bool(opened_download),
            }
            self._commit_locked()

    # --- Updater log upload offset ---

    def updater_log_offset(self) -> int:
        with self._lock:
            try:
                value = int(self._state.get("updater_log_offset", 0) or 0)
            except (TypeError, ValueError):
                return 0
            return value if value >= 0 else 0

    def set_updater_log_offset(self, offset: int) -> None:
        try:
            value = int(offset)
        except (TypeError, ValueError):
            value = 0
        if value < 0:
            value = 0
        with self._lock:
            self._state["updater_log_offset"] = value
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
