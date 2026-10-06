"""PySide6 tray application for configuring and connecting the Windows client."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import queue
import platform
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from loguru import logger
from cryptography.exceptions import InvalidSignature

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "client"

from PySide6.QtCore import QThread, QTimer, Qt, Signal, QPoint
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QDialog,
    QStackedWidget,
    QSystemTrayIcon,
    QMenu,
    QVBoxLayout,
    QWidget,
)
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from .version import VERSION_STR, BUILD_DATE, display_version
from .core.connection import (
    CONFIG_UPDATE,
    CLIENT_DISCONNECT,
    MESSAGE,
    PING,
    PONG,
    SERVER_SHUTDOWN,
    UPDATE_AVAILABLE,
    Connection,
    ClientNotApprovedError,
    ServerFingerprintMismatch,
    IncomingMessage,
    IncomingWithdrawal,
    decode_message,
    decode_update_available,
    decode_withdrawal,
    connect,
    download_update_package_tls,
    probe_server_fingerprint,
)
from .core.delivery import ConfigSnapshot, MessageDeduplicator, PriorityQueue, ReconnectBackoff, display_duration_ms
from .core.discovery import DiscoveryResponse, discover, parse_scan_destinations
from .core.identity import decrypt_private_key, encrypt_private_key, generate_identity, public_key_fingerprint
from .core.protocol.bson import decode, encode
from .core.protocol.packet import Packet, read_packet
from .core.store import StateStore
from .core.tts import (
    EnginePreference,
    PiperEngine,
    SAPIEngine,
    SpeechPart,
    compile_speech,
    estimate_duration_ms,
    speak_with_fallback,
)
from .core.updater import (
    UpdateError,
    launch_updater,
    normalize_components,
    prepare_apply_workspace,
    prepare_detached_updater,
    resolve_install_dir,
    resolve_updater_executable,
    stage_package_for_apply,
    verify_metadata,
    verify_package_files,
)
from .core.updater_log_upload import queue_pending_updater_logs
from .core.applied_updates import is_update_applied, load_applied_digests, mark_update_applied


LOGGER = logger.bind(name="mkcb.client")
CLIENT_LOG_UPLOAD = 0x0206
_CLIENT_LOG_QUEUE: queue.Queue = queue.Queue(maxsize=1000)


def _parse_version_parts(value: str) -> list[object]:
    parts: list[object] = []
    for part in str(value or "").strip().split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(part)
    return parts


def _client_version_behind(reported: str, target: str) -> bool:
    """True when reported is strictly older than target (numeric dotted versions)."""
    reported = str(reported or "").strip()
    target = str(target or "").strip()
    if not reported or not target or reported == target:
        return False
    left = _parse_version_parts(reported)
    right = _parse_version_parts(target)
    width = max(len(left), len(right))
    left.extend([0] * (width - len(left)))
    right.extend([0] * (width - len(right)))
    return left < right


def _enqueue_client_log_entry(entry: str) -> None:
    """Push a pre-serialized ClientLog JSON string onto the upload queue."""
    if not entry:
        return
    if _CLIENT_LOG_QUEUE.full():
        try:
            _CLIENT_LOG_QUEUE.get_nowait()
        except queue.Empty:
            pass
    try:
        _CLIENT_LOG_QUEUE.put_nowait(entry)
    except queue.Full:
        pass


def _queue_client_log(message: Any) -> None:
    record = message.record
    log_message = record["message"]
    extra = record.get("extra") or {}
    logger_name = str(extra.get("name") or record.get("name") or "mkcb.client")
    entry = ""
    for _ in range(2):
        entry = json.dumps(
            {
                "time": record["time"].isoformat(),
                "level": record["level"].name,
                "msg": log_message,
                "logger": logger_name,
                "source": "client",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(entry.encode("utf-8")) <= 16 * 1024:
            break
        log_message = log_message[: max(100, len(log_message) // 2)]
    if len(entry.encode("utf-8")) > 16 * 1024:
        entry = json.dumps(
            {
                "time": record["time"].isoformat(),
                "level": record["level"].name,
                "msg": log_message[:3000],
                "logger": logger_name,
                "source": "client",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    _enqueue_client_log_entry(entry)


def _data_dir() -> Path:
    # Packaged builds keep state beside the installed executable. In a source
    # checkout this resolves to client/data, matching the install-directory policy.
    install_dir = os.environ.get("MKCB_INSTALL_DIR")
    base = Path(install_dir).resolve() if install_dir else Path(__file__).resolve().parent
    path = base / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


class SingleInstanceLock:
    """使用操作系统文件锁保证同一安装目录只有一个客户端进程。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Optional[Any] = None

    def acquire(self) -> bool:
        """尝试非阻塞加锁；锁文件本身保留，状态由操作系统维护。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        """释放文件锁并关闭句柄，不删除锁文件。"""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            handle.close()


def _application_icon() -> QIcon:
    """绘制不依赖系统主题的托盘和窗口图标，确保 Windows 托盘始终可见。"""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#0f172a"))
    painter.setBrush(QColor("#38bdf8"))
    painter.drawRoundedRect(2, 2, 28, 28, 7, 7)
    painter.setPen(QColor("#082f49"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "M")
    painter.end()
    return QIcon(pixmap)


def _make_client_log_file_sink(log_path: Path):
    """Return a loguru sink that appends one JSON object per line.

    Must be a sink (not ``format=``): loguru still runs ``str.format_map`` on
    format templates, so a JSON body with ``{...}`` would raise KeyError.
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = 2 * 1024 * 1024
    retain = 3

    def _rotate_if_needed() -> None:
        try:
            if not path.is_file() or path.stat().st_size < max_bytes:
                return
        except OSError:
            return
        # Simple size rotation: client.log -> client.log.1 ... keep ``retain`` files.
        try:
            oldest = path.with_name(f"{path.name}.{retain}")
            if oldest.exists():
                oldest.unlink(missing_ok=True)
            for index in range(retain - 1, 0, -1):
                src = path.with_name(f"{path.name}.{index}")
                dst = path.with_name(f"{path.name}.{index + 1}")
                if src.exists():
                    src.replace(dst)
            path.replace(path.with_name(f"{path.name}.1"))
        except OSError:
            pass

    def sink(message: Any) -> None:
        record = message.record
        extra = record.get("extra") or {}
        logger_name = str(extra.get("name") or record.get("name") or "mkcb.client")
        payload = {
            "time": record["time"].isoformat(),
            "level": record["level"].name,
            "msg": record["message"],
            "logger": logger_name,
            "source": "client",
        }
        try:
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        except (TypeError, ValueError):
            payload["msg"] = str(record.get("message", ""))[:4000]
            payload["logger"] = "mkcb.client"
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        _rotate_if_needed()
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            pass

    return sink


def _configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(extra={"name": "mkcb.client"})
    # Disk + upload share the same JSON schema the server already requires (json.Valid).
    # Use a sink callable — do NOT put JSON in format= (braces break str.format_map).
    logger.add(
        _make_client_log_file_sink(data_dir / "client.log"),
        level="DEBUG",
        backtrace=False,
        diagnose=False,
        enqueue=True,
        catch=True,
    )
    logger.add(_queue_client_log, level="DEBUG", enqueue=True, catch=True)


def _read_client_log(data_dir: Path, max_bytes: int = 512 * 1024) -> str:
    path = data_dir / "client.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            data = stream.read()
        text = data.decode("utf-8", errors="replace")
        if size > max_bytes:
            text = "[日志已截取最后 {} KB]\n".format(max_bytes // 1024) + text
        return text
    except OSError as exc:
        return "读取客户端日志失败：{}".format(exc)


def _public_key_fingerprint(public_key: bytes) -> str:
    """Return the canonical display form; the raw key remains protocol-only."""
    key = Ed25519PublicKey.from_public_bytes(public_key) if len(public_key) == 32 else None
    if key is not None:
        public_key = key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    digest = hashlib.sha256(public_key).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _fingerprint_text(digest: bytes) -> str:
    """将已计算好的 SHA-256 摘要转换为 UI/日志统一格式。"""
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _hardware_ids() -> tuple[str, str]:
    if sys.platform == "win32":
        import winreg

        machine_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        device_id = str(winreg.QueryValueEx(machine_key, "MachineGuid")[0])
        cpu_key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        )
        cpuid = str(winreg.QueryValueEx(cpu_key, "Identifier")[0])
        return cpuid, device_id
    return platform.processor() or platform.machine(), platform.node()


def _identity_files(data_dir: Path) -> tuple[Ed25519PrivateKey, bytes]:
    encrypted_path = data_dir / "identity.enc"
    cpuid, device_id = _hardware_ids()
    if encrypted_path.exists():
        blob = encrypted_path.read_bytes()
        private_key = decrypt_private_key(blob, cpuid, device_id)
        try:
            document = json.loads(blob.decode("utf-8"))
            if int(document.get("format_version", 2)) == 1:
                encrypted_path.write_bytes(
                    encrypt_private_key(
                        private_key,
                        int(document["install_timestamp_ms"]),
                        cpuid,
                        device_id,
                    )
                )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
        # The certificate is ephemeral. TLS only needs its public key to bind
        # the business handshake, so no certificate is kept on disk.
        return private_key, _certificate(private_key)
    identity = generate_identity()
    encrypted_path.write_bytes(
        encrypt_private_key(identity.private_key, int(time.time() * 1000), cpuid, device_id)
    )
    return identity.private_key, identity.certificate_pem


def _certificate(private_key: Ed25519PrivateKey) -> bytes:
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MKCB client")])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .sign(private_key, None)
        .public_bytes(serialization.Encoding.PEM)
    )


def _startup_command() -> str:
    install_dir = os.environ.get("MKCB_INSTALL_DIR")
    if install_dir:
        launcher = Path(install_dir) / "lovemilk-class-broadcaster.exe"
        if launcher.exists():
            return '"{}"'.format(launcher)
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        windowed = executable.with_name("pythonw.exe")
        if windowed.exists():
            executable = windowed
    return '"{}" "{}"'.format(executable, Path(__file__).resolve())


_AUTOSTART_VALUE = "MKCBClient"
_AUTOSTART_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Full path form for `reg.exe` (HKLM needs elevation; HKCU does not).
_AUTOSTART_REG_HKCU = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_REG_HKLM = r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run"


def _read_run_value(hive: int) -> Optional[str]:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(hive, _AUTOSTART_RUN_PATH) as key:
            value, _ = winreg.QueryValueEx(key, _AUTOSTART_VALUE)
            return str(value)
    except OSError:
        return None


def _write_run_value_hkcu(enabled: bool) -> None:
    """Write or delete the current-user Run value (no elevation)."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_RUN_PATH) as key:
        if enabled:
            winreg.SetValueEx(key, _AUTOSTART_VALUE, 0, winreg.REG_SZ, _startup_command())
            return
        try:
            winreg.DeleteValue(key, _AUTOSTART_VALUE)
        except FileNotFoundError:
            pass


def _reg_exe_path() -> str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return str(Path(system_root) / "System32" / "reg.exe")


def _run_reg(args: list[str], *, elevate: bool) -> None:
    """Run ``reg.exe``; when ``elevate`` is True, request UAC via ShellExecuteW runas."""
    reg = _reg_exe_path()
    if not elevate:
        completed = subprocess.run(
            [reg, *args],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip() or f"exit {completed.returncode}"
            raise OSError(f"reg failed: {detail}")
        return

    # ShellExecuteW "runas" shows the UAC consent dialog. Wait for the elevated
    # process so the UI can re-read the registry afterwards.
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError as exc:
        raise OSError("ctypes unavailable for UAC elevation") from exc

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    INFINITE = 0xFFFFFFFF
    WAIT_OBJECT_0 = 0
    STILL_ACTIVE = 259
    COINIT_APARTMENTTHREADED = 0x2

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    # Quote args the same way CreateProcess would for a command line.
    def _quote(part: str) -> str:
        if not part:
            return '""'
        if any(ch in part for ch in ' \t"'):
            return '"' + part.replace('"', '\\"') + '"'
        return part

    params = " ".join(_quote(a) for a in args)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = reg
    info.lpParameters = params
    info.lpDirectory = None
    info.nShow = SW_HIDE

    # ShellExecuteEx is more reliable after CoInitialize on some hosts.
    try:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        com_inited = True
    except Exception:
        com_inited = False
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            err = ctypes.get_last_error()
            # 1223 = ERROR_CANCELLED (user dismissed UAC).
            if err == 1223:
                raise OSError("已取消管理员授权（UAC）")
            raise OSError(f"无法请求管理员权限（错误码 {err}）")
        handle = info.hProcess
        if not handle:
            raise OSError("elevated reg process handle missing")
        wait = kernel32.WaitForSingleObject(handle, INFINITE)
        if wait != WAIT_OBJECT_0:
            kernel32.CloseHandle(handle)
            raise OSError("waiting for elevated reg process failed")
        exit_code = wintypes.DWORD(STILL_ACTIVE)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            kernel32.CloseHandle(handle)
            raise OSError("could not read elevated reg exit code")
        kernel32.CloseHandle(handle)
        if int(exit_code.value) != 0:
            raise OSError(f"elevated reg failed with exit code {int(exit_code.value)}")
    finally:
        if com_inited:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def _write_run_value_hklm(enabled: bool) -> None:
    """Write or delete HKLM Run via ``reg.exe``, elevating with UAC when needed."""
    import winreg

    command = _startup_command()
    # Fast path: already elevated / writable without a prompt.
    try:
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, _AUTOSTART_RUN_PATH) as key:
            if enabled:
                winreg.SetValueEx(key, _AUTOSTART_VALUE, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_VALUE)
                except FileNotFoundError:
                    pass
        return
    except OSError:
        pass

    if enabled:
        # reg add "HKLM\...\Run" /v MKCBClient /t REG_SZ /d "..." /f
        _run_reg(
            ["add", _AUTOSTART_REG_HKLM, "/v", _AUTOSTART_VALUE, "/t", "REG_SZ", "/d", command, "/f"],
            elevate=True,
        )
    else:
        # Deleting a missing value is fine; treat non-zero as soft if already gone.
        try:
            _run_reg(
                ["delete", _AUTOSTART_REG_HKLM, "/v", _AUTOSTART_VALUE, "/f"],
                elevate=True,
            )
        except OSError:
            if _read_run_value(winreg.HKEY_LOCAL_MACHINE):
                raise
        return

    # Confirm the elevated write landed.
    written = _read_run_value(winreg.HKEY_LOCAL_MACHINE)
    if enabled:
        if written != command:
            raise OSError("本机（所有用户）自启动写入失败或未生效")
    elif written:
        raise OSError("本机（所有用户）自启动删除失败")


def _autostart_scope() -> str:
    """Return 'hkcu', 'hklm', or '' depending on where the Run entry lives."""
    if sys.platform != "win32":
        return ""
    import winreg

    command = _startup_command()
    hklm = _read_run_value(winreg.HKEY_LOCAL_MACHINE)
    if hklm == command:
        return "hklm"
    hkcu = _read_run_value(winreg.HKEY_CURRENT_USER)
    if hkcu == command:
        return "hkcu"
    # Any HKLM entry with our value name counts as machine scope (command may
    # differ after reinstall); prefer showing machine as active.
    if hklm:
        return "hklm"
    if hkcu:
        return "hkcu"
    return ""


def _autostart_enabled() -> bool:
    return bool(_autostart_scope())


def _autostart_uses_machine() -> bool:
    return _autostart_scope() == "hklm"


def _set_autostart(enabled: bool, *, machine: bool = False) -> None:
    """Enable or disable Windows Run autostart.

    ``machine=True`` writes HKLM (all users) via ``reg.exe`` with UAC elevation.
    Otherwise HKCU. When enabling one hive, the other is cleared so only one entry remains.
    """
    if sys.platform != "win32":
        raise OSError("开机自启动仅支持 Windows")
    import winreg

    if not enabled:
        # Clear both hives so unchecking always stops autostart.
        try:
            _write_run_value_hkcu(False)
        except OSError:
            pass
        try:
            if _read_run_value(winreg.HKEY_LOCAL_MACHINE):
                _write_run_value_hklm(False)
        except OSError as exc:
            if _read_run_value(winreg.HKEY_LOCAL_MACHINE):
                raise OSError(f"取消本机（所有用户）自启动失败：{exc}") from exc
        return

    if machine:
        _write_run_value_hklm(True)
        try:
            _write_run_value_hkcu(False)
        except OSError:
            pass
        return

    _write_run_value_hkcu(True)
    try:
        if _read_run_value(winreg.HKEY_LOCAL_MACHINE):
            _write_run_value_hklm(False)
    except OSError:
        # Leaving an HKLM entry is OK if UAC was denied; both would start the same app.
        pass


def _play_notification_sound() -> None:
    """Play the Windows notification sound when a broadcast appears (independent of TTS)."""
    try:
        if sys.platform == "win32":
            import winsound

            # Prefer the user-configured “通知” sound (Settings → System → Sound →
            # Notifications). Fall back through older aliases, then MessageBeep.
            flags = winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT
            for alias in (
                "Notification.Default",  # Windows 8+ toast / 通知
                "SystemNotification",
                "SystemAsterisk",
            ):
                try:
                    if winsound.PlaySound(alias, flags):
                        return
                except RuntimeError:
                    continue
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        # Best-effort on Linux/dev hosts.
        print("\a", end="", flush=True)
    except Exception as exc:
        LOGGER.debug("notification sound failed error={}", exc)


class ScanWorker(QThread):
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, destination: str) -> None:
        super().__init__()
        self.destination = destination

    def run(self) -> None:
        try:
            targets, broadcast = parse_scan_destinations(self.destination)
            results = discover(targets, timeout=2.0, broadcast=broadcast)
            self.found.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class ListenerWorker(QThread):
    """Optional client listen port: TLS accept + signed PING/PONG only (English logs)."""

    DEFAULT_IDLE_TIMEOUT_SECONDS = 60

    status = Signal(str)

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        certificate: bytes,
        port: int,
        state_store: StateStore,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self.private_key = private_key
        self.certificate = certificate
        self.port = int(port)
        self.state_store = state_store
        self.idle_timeout_seconds = max(1, min(3600, int(idle_timeout_seconds or self.DEFAULT_IDLE_TIMEOUT_SECONDS)))
        self.keep_running = True
        self.sent = 0
        self.received = 0
        self.latencies: list[float] = []
        self._listener: Optional[socket.socket] = None
        self._log = logger.bind(name="mkcb.client.listener")

    def stop(self) -> None:
        self.keep_running = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    def run(self) -> None:
        cert_path: Optional[Path] = None
        key_path: Optional[Path] = None
        try:
            with tempfile.TemporaryDirectory(prefix="mkcb-listener-") as temporary:
                cert_path = Path(temporary) / "client.pem"
                key_path = Path(temporary) / "client-key.pem"
                cert_path.write_bytes(self.certificate)
                key_path.write_bytes(self.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls_context.minimum_version = ssl.TLSVersion.TLSv1_3
                tls_context.maximum_version = ssl.TLSVersion.TLSv1_3
                tls_context.load_cert_chain(str(cert_path), str(key_path))
                # Probe trust is SPKI hash inside the app packet; TLS is encryption only.
                tls_context.verify_mode = ssl.CERT_NONE
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("0.0.0.0", self.port))
                listener.listen(8)
                listener.settimeout(1.0)
                self._listener = listener
                self._log.info("listener mode started port={}", self.port)
                self.status.emit("listener mode started port={}".format(self.port))
                while self.keep_running:
                    try:
                        raw, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        with tls_context.wrap_socket(raw, server_side=True) as connection:
                            # Listener idle applies only to S->C listen probes (default 60s).
                            connection.settimeout(self.idle_timeout_seconds)
                            packet = read_packet(connection)
                            challenge = packet.payload
                            nonce = challenge[:32]
                            server_public_key = challenge[32:64]
                            server_fingerprint = public_key_fingerprint(server_public_key) if len(server_public_key) == 32 else b""
                            signed_challenge = b"MKCB-listener-probe-v1\x00" + nonce + server_public_key
                            server_signature_valid = False
                            if len(server_public_key) == 32 and len(challenge) == 128:
                                try:
                                    Ed25519PublicKey.from_public_bytes(server_public_key).verify(challenge[64:], signed_challenge)
                                    server_signature_valid = True
                                except InvalidSignature:
                                    pass
                            if (
                                packet.packet_type != PING
                                or len(challenge) != 128
                                or not self.state_store.trusts_server_fingerprint(server_fingerprint)
                                or not server_signature_valid
                            ):
                                self._log.warning(
                                    "listener rejected untrusted server probe fingerprint={}",
                                    _fingerprint_text(server_fingerprint),
                                )
                                continue
                            started = time.monotonic()
                            client_public_key = self.private_key.public_key().public_bytes(
                                serialization.Encoding.Raw, serialization.PublicFormat.Raw
                            )
                            response = client_public_key + self.private_key.sign(signed_challenge)
                            connection.sendall(Packet(1, 0, PONG, packet.sequence, 0, response).encode())
                            self.sent += 1
                            self.received += 1
                            self.latencies.append((time.monotonic() - started) * 1000)
                            self._log.info(
                                "listener probe answered count={} latency_ms={:.1f}",
                                self.received,
                                self.latencies[-1],
                            )
                            self.status.emit("listener mode answered packets={}".format(self.received))
                    except Exception as exc:
                        self._log.debug("listener probe failed: {}", exc)
                    finally:
                        try:
                            raw.close()
                        except OSError:
                            pass
                listener.close()
                self._log.info("listener mode stopped port={}", self.port)
        except Exception as exc:
            self._log.exception("listener mode failed")
            self.status.emit("listener mode unavailable: {}".format(exc))
        finally:
            self._listener = None


class UpdateApplyWorker(QThread):
    """Download + verify + launch updater off the UI thread (TLS download can take minutes)."""

    finished_ok = Signal(str)  # sha256
    finished_error = Signal(str, str)  # sha256, error
    quit_for_update = Signal()

    def __init__(
        self,
        update: dict[str, Any],
        data_dir: Path,
        certificate: bytes,
        private_key: Ed25519PrivateKey,
        parent: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self.update = dict(update)
        self.data_dir = Path(data_dir)
        self.certificate = certificate
        self.private_key = private_key

    def run(self) -> None:
        digest = str(self.update.get("sha256") or "").lower()
        components = normalize_components(self.update)
        component = str(self.update.get("component") or "")
        version = str(self.update.get("version") or "")
        seq = int(self.update.get("seq") or 0)
        package = self.update.get("package")
        download_token = str(self.update.get("download_token") or "")
        download_url = str(self.update.get("download_url") or self.update.get("release_url") or "")
        force = bool(self.update.get("force"))
        LOGGER.info(
            "update apply worker start components={} primary={} version={} seq={} sha256={} force={} has_package={} has_token={}",
            components,
            component,
            version,
            seq,
            digest,
            force,
            bool(isinstance(package, (bytes, bytearray)) and package),
            bool(download_token),
        )
        try:
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"invalid sha256={self.update.get('sha256')!r}")
            update_dir = self.data_dir / "updates"
            update_dir.mkdir(parents=True, exist_ok=True)
            package_path = ClientWindow._update_package_path(update_dir, digest)
            metadata_path = update_dir / f"{digest}.metadata.json"
            skip_keys = {"package", "download_token", "source_fingerprint"}
            metadata = {key: value for key, value in self.update.items() if key not in skip_keys}
            metadata["components"] = components
            metadata["sha256"] = digest
            if isinstance(package, (bytes, bytearray)) and package:
                package_path.write_bytes(bytes(package))
                source = "inline"
            elif download_token:
                self._download_tls(package_path, digest, download_token)
                source = "tls"
            elif download_url:
                self._download_http_legacy(download_url, package_path)
                source = "http-legacy"
            elif package_path.is_file():
                source = "cached"
            else:
                raise ValueError("update notification has neither package bytes nor download token")
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=True, sort_keys=True, default=str),
                encoding="utf-8",
            )
            LOGGER.info(
                "update package stored path={} components={} version={} seq={} source={}",
                package_path,
                components,
                version,
                seq,
                source,
            )
            should_quit = self._verify_and_launch(package_path, metadata)
            # Persist before quit so a force catch-up after restart cannot re-apply.
            mark_update_applied(self.data_dir, digest)
            self.finished_ok.emit(digest)
            if should_quit:
                self.quit_for_update.emit()
        except Exception as exc:
            LOGGER.exception(
                "update apply worker failed sha256={} version={} seq={} error={}",
                digest,
                version,
                seq,
                exc,
            )
            self.finished_error.emit(digest, str(exc))

    def _download_peer(self) -> tuple[str, int, bytes]:
        host = str(self.update.get("source_host") or "").strip()
        try:
            port = int(self.update.get("source_tcp_port") or 0)
        except (TypeError, ValueError):
            port = 0
        fingerprint = self.update.get("source_fingerprint") or b""
        if not isinstance(fingerprint, (bytes, bytearray)):
            fingerprint = b""
        fingerprint = bytes(fingerprint)
        if host and port > 0 and len(fingerprint) == 32:
            return host, port, fingerprint
        raise ValueError("no trusted TLS peer available for update download")

    def _download_tls(self, destination: Path, expected_sha256: str, download_token: str) -> None:
        host, port, fingerprint = self._download_peer()
        expires_at = int(self.update.get("expires_at") or 0)
        if expires_at and expires_at <= int(time.time()):
            raise ValueError("update download token expired")
        LOGGER.info(
            "update TLS download starting host={} port={} sha256={} dest={}",
            host,
            port,
            expected_sha256,
            destination,
        )
        with tempfile.TemporaryDirectory(prefix="mkcb-update-dl-") as temporary:
            cert_path = Path(temporary) / "client.pem"
            key_path = Path(temporary) / "client-key.pem"
            cert_path.write_bytes(self.certificate)
            key_path.write_bytes(
                self.private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            download_update_package_tls(
                host,
                port,
                str(cert_path),
                str(key_path),
                fingerprint,
                expected_sha256,
                download_token,
                destination,
                timeout=30.0,
                idle_warn_seconds=5.0,
                idle_fail_seconds=120.0,
            )
            LOGGER.info(
                "update TLS download finished host={} port={} sha256={} dest={}",
                host,
                port,
                expected_sha256,
                destination,
            )

    def _download_http_legacy(self, url: str, destination: Path) -> None:
        name = destination.name.lower()
        if not (name.endswith(".tar.zst") or name.endswith(".tar.zstd")):
            stem = destination.name
            for suffix in (".zst", ".zstd", ".zip", ".tar"):
                if stem.lower().endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            destination = destination.with_name(stem + ".tar.zst")
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": f"mkcb-client/{VERSION_STR}"})
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        temporary = destination.with_name(destination.name + ".partial")
        try:
            with urllib.request.urlopen(request, timeout=120, context=context) as response:
                status = getattr(response, "status", None) or response.getcode()
                if int(status) != 200:
                    raise RuntimeError(f"download status {status}")
                with temporary.open("wb") as out:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
            with temporary.open("rb") as probe:
                magic = probe.read(4)
            if magic != b"\x28\xb5\x2f\xfd":
                raise RuntimeError("downloaded update package is not zstd-compressed (.tar.zst expected)")
            temporary.replace(destination)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _verify_and_launch(self, package_path: Path, metadata: dict[str, Any]) -> bool:
        """Return True when the main process should quit for the detached updater."""
        components = normalize_components(metadata)
        version = str(metadata.get("version") or "")
        digest = str(metadata.get("sha256") or "").lower()
        LOGGER.info(
            "update verify start path={} components={} version={} sha256={}",
            package_path,
            components,
            version,
            digest,
        )
        verify_metadata(metadata, b"")
        verify_package_files(package_path, metadata)
        LOGGER.info("update package verified path={} sha256={}", package_path, digest)
        install_dir = resolve_install_dir()
        if not (os.environ.get("MKCB_INSTALL_DIR") or "").strip():
            LOGGER.warning(
                "update apply skipped: MKCB_INSTALL_DIR is unset (dev mode); package kept at {}",
                package_path,
            )
            return False
        installed_updater = resolve_updater_executable(install_dir)
        # Short apply path: full sha twice under deep Desktop folders exceeds Windows MAX_PATH.
        work_dir = prepare_apply_workspace(self.data_dir, digest)
        durable_package = stage_package_for_apply(package_path, work_dir)
        detached_updater = prepare_detached_updater(installed_updater, work_dir)
        if not durable_package.is_file():
            raise UpdateError(f"staged update package missing after copy: {durable_package}")
        updater_log = self.data_dir / "updater.log"
        LOGGER.info(
            "update launching updater path={} package={} package_bytes={} install_dir={} "
            "work_dir={} components={} version={} log={}",
            detached_updater,
            durable_package,
            durable_package.stat().st_size,
            install_dir,
            work_dir,
            components,
            version,
            updater_log,
        )
        process = launch_updater(
            detached_updater,
            durable_package,
            install_dir,
            pid=os.getpid(),
            detach=True,
            log_file=updater_log,
        )
        LOGGER.info(
            "update updater started pid={} waiting_for_client_exit components={} version={}",
            process.pid,
            components,
            version,
        )
        return True


class ConnectionWorker(QThread):
    # status/config carry endpoint_key so the UI can manage many concurrent servers.
    status = Signal(object, str)
    connection_state = Signal(object, bool)
    received = Signal(object, object)  # endpoint_key, IncomingMessage
    withdrawn = Signal(object, object)  # endpoint_key, IncomingWithdrawal
    config_updated = Signal(object, object)  # endpoint_key, ConfigSnapshot
    certificate_mismatch = Signal(object)
    update_available = Signal(object)

    def __init__(
        self,
        endpoint: dict[str, Any],
        private_key: Ed25519PrivateKey,
        certificate: bytes,
        state_store: StateStore,
        data_dir: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.private_key = private_key
        self.certificate = certificate
        self.state_store = state_store
        self.data_dir = Path(data_dir) if data_dir is not None else _data_dir()
        self.keep_running = True
        self._manual_reconnect = threading.Event()
        self.admin_disconnected = False
        self.server_connection_mode = "pull"
        self.acks: queue.Queue = queue.Queue()
        self.trust_decisions: queue.Queue = queue.Queue()
        # (reason, detail) intentional goodbye; flushed on the connection thread only.
        self.session_end_queue: queue.Queue = queue.Queue()
        self._session_end_sent = threading.Event()
        self.dedup = MessageDeduplicator()
        self.completed_acks: dict[str, tuple[str, Optional[str]]] = {}
        self._active_connection: Optional[Connection] = None
        self.last_snapshot: Optional[ConfigSnapshot] = None
        self.listener_mode, self.listener_port = state_store.listener_settings()
        fingerprint = endpoint.get("fingerprint", b"")
        if isinstance(fingerprint, str):
            try:
                fingerprint = base64.b64decode(fingerprint)
            except (ValueError, TypeError):
                fingerprint = b""
        self.server_fingerprint = bytes(fingerprint)

    def endpoint_key(self) -> tuple[str, int]:
        return str(self.endpoint["host"]), int(self.endpoint["tcp_port"])

    def acknowledge(self, message_id: str, status: str, error: Optional[str] = None) -> None:
        self.acks.put((message_id, status, error))

    def accept_server_fingerprint(self, fingerprint: Optional[bytes]) -> None:
        """交由 UI 确认运行时发现的服务端指纹变化。"""
        self.trust_decisions.put(bytes(fingerprint) if fingerprint is not None else None)

    def report_session_end(self, reason: str = "user_exit", detail: str = "") -> None:
        """Queue an intentional goodbye; the connection thread sends it on the TLS socket."""
        self._session_end_sent.clear()
        try:
            self.session_end_queue.put_nowait((str(reason or "user_exit"), str(detail or "")))
        except queue.Full:
            pass
        # Nudge the read loop off a blocking receive so flush can run promptly.
        self._manual_reconnect.set()

    def stop(self) -> None:
        self.keep_running = False
        self._manual_reconnect.set()
        if self._active_connection is not None:
            try:
                self._active_connection.close()
            except OSError:
                pass

    def request_manual_reconnect(self) -> None:
        self._manual_reconnect.set()

    def run(self) -> None:
        backoff = ReconnectBackoff()
        approval_deadline: Optional[float] = None
        while self.keep_running:
            connection: Optional[Connection] = None
            normal_shutdown = False
            manual_attempt = self._manual_reconnect.is_set()
            if manual_attempt:
                self._manual_reconnect.clear()
            reconnect_probe = self.admin_disconnected and not manual_attempt
            try:
                LOGGER.info(
                    "connection attempt {}:{}",
                    self.endpoint.get("host"),
                    self.endpoint.get("tcp_port"),
                )
                with tempfile.TemporaryDirectory(prefix="mkcb-tls-") as temporary:
                    cert_path = Path(temporary) / "client.pem"
                    key_path = Path(temporary) / "client-key.pem"
                    cert_path.write_bytes(self.certificate)
                    key_path.write_bytes(
                        self.private_key.private_bytes(
                            serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption(),
                        )
                    )
                    endpoint_config_id = None
                    for saved in self.state_store.servers():
                        if (
                            str(saved.get("host", "")).strip() == str(self.endpoint["host"])
                            and int(saved.get("tcp_port", 0) or 0) == int(self.endpoint["tcp_port"])
                        ):
                            candidate = saved.get("config_id")
                            if isinstance(candidate, str) and candidate:
                                endpoint_config_id = candidate
                            break
                    connection, response = connect(
                        self.endpoint["host"],
                        self.endpoint["tcp_port"],
                        str(cert_path),
                        str(key_path),
                        self.endpoint["fingerprint"],
                        config_id=endpoint_config_id,
                        timeout=3,
                        listener_mode=self.listener_mode,
                        listener_port=self.listener_port if self.listener_mode in ("auto", "listen") else 0,
                        reconnect_probe=reconnect_probe,
                        manual_connect=manual_attempt,
                    )
                # A successful business handshake means authorization has
                # completed. Any later disconnect must use ordinary backoff,
                # never the 15-second approval retry window.
                approval_deadline = None
                mode = response.get("connection_mode") if isinstance(response, dict) else None
                if mode in ("pull", "listen"):
                    self.server_connection_mode = mode
                if isinstance(response, dict) and response.get("connect_allowed") is False:
                    LOGGER.info("server denied reconnect probe mode={}", self.server_connection_mode)
                    self.status.emit(self.endpoint_key(), "服务端当前不允许连接；5 分钟后再次探测")
                    if connection is not None:
                        connection.close()
                        connection = None
                    raise ConnectionError("server_denied_reconnect_probe")
                actual_fingerprint = response.get("_server_fingerprint") if isinstance(response, dict) else None
                server_public_key = response.get("_server_public_key") if isinstance(response, dict) else None
                if isinstance(server_public_key, (bytes, bytearray)) and len(server_public_key) == 32:
                    self.endpoint["server_public_key"] = bytes(server_public_key)
                if isinstance(actual_fingerprint, bytes) and len(actual_fingerprint) == 32:
                    if not any(self.server_fingerprint) or self.server_fingerprint != actual_fingerprint:
                        self.server_fingerprint = actual_fingerprint
                        self.endpoint["fingerprint"] = actual_fingerprint
                        self.state_store.set_server(
                            host=str(self.endpoint["host"]),
                            tcp_port=int(self.endpoint["tcp_port"]),
                            http_port=int(self.endpoint.get("http_port", 39003)),
                            fingerprint=actual_fingerprint,
                            auto_connect=True,
                        )
                self._active_connection = connection
                self.admin_disconnected = False
                endpoint_key = (str(self.endpoint["host"]), int(self.endpoint["tcp_port"]))
                self.connection_state.emit(endpoint_key, True)
                config = response.get("config")
                if isinstance(config, dict):
                    snapshot = ConfigSnapshot.from_wire(config)
                else:
                    # Servers may omit an unchanged snapshot. Keep this endpoint's last ID.
                    snapshot = ConfigSnapshot(
                        config_id=endpoint_config_id or "0" * 13 + ".0",
                        issued_at=int(time.time() * 1000),
                        heartbeat_interval=15,
                        heartbeat_timeout=45,
                        message_ttl=86400,
                        max_speech_depth=8,
                        max_repeat_expansion=100,
                        display_position="center",
                        display_duration_ratio=0.4,
                        listener_idle_timeout_seconds=60,
                    )
                self.last_snapshot = snapshot
                self.state_store.set_server_config_id(
                    host=str(self.endpoint["host"]),
                    tcp_port=int(self.endpoint["tcp_port"]),
                    config_id=snapshot.config_id,
                )
                connection.socket.settimeout(0.25)
                backoff.reset()
                self.status.emit(
                    self.endpoint_key(),
                    "已连接（配置 {}）".format(snapshot.config_id),
                )
                self.config_updated.emit(self.endpoint_key(), snapshot)
                LOGGER.info(
                    "connection established {}:{} config_id={}",
                    self.endpoint["host"],
                    self.endpoint["tcp_port"],
                    snapshot.config_id,
                )
                # Detached updater wrote data/updater.log while we were offline —
                # queue those lines for ClientLog upload with logger=mkcb.updater.
                try:
                    queued = queue_pending_updater_logs(self.data_dir, _enqueue_client_log_entry)
                    if queued:
                        LOGGER.info("queued {} updater log line(s) for server upload", queued)
                except Exception as exc:
                    LOGGER.debug("updater log upload queue skipped error={}", exc)
                last_ping = time.monotonic()
                last_pong = last_ping
                while self.keep_running:
                    self._flush_acks(connection)
                    self._flush_client_logs(connection)
                    if self._flush_session_end(connection):
                        # Intentional goodbye sent; leave the session cleanly.
                        break
                    now = time.monotonic()
                    if now - last_ping >= snapshot.heartbeat_interval:
                        connection.send(Packet(1, 0, PING, 0, 0, b""))
                        last_ping = now
                    if now - last_pong > snapshot.heartbeat_timeout:
                        raise TimeoutError("服务端心跳超时")
                    try:
                        packet = connection.receive()
                    except socket.timeout:
                        continue
                    if packet.packet_type == MESSAGE:
                        message = decode_message(packet)
                        connection.acknowledge(message.message_id, "received")
                        if self.dedup.observe(self.server_fingerprint, message.message_id):
                            tagged = message.model_copy(
                                update={
                                    "source_host": str(self.endpoint["host"]),
                                    "source_tcp_port": int(self.endpoint["tcp_port"]),
                                }
                            )
                            self.received.emit(self.endpoint_key(), tagged)
                        elif message.message_id in self.completed_acks:
                            status, error = self.completed_acks[message.message_id]
                            connection.acknowledge(message.message_id, status, error)
                    elif packet.packet_type == 0x1003:
                        withdrawal = decode_withdrawal(packet)
                        tagged_withdrawal = withdrawal.model_copy(
                            update={
                                "source_host": str(self.endpoint["host"]),
                                "source_tcp_port": int(self.endpoint["tcp_port"]),
                            }
                        )
                        self.withdrawn.emit(self.endpoint_key(), tagged_withdrawal)
                    elif packet.packet_type == CONFIG_UPDATE:
                        document = decode(packet.payload)
                        candidate = document.get("config", document)
                        if not isinstance(candidate, dict):
                            raise ValueError("配置更新格式错误")
                        updated = ConfigSnapshot.from_wire(candidate)
                        # Apply after full validation and persist the ID atomically.
                        snapshot = updated
                        self.last_snapshot = snapshot
                        self.state_store.set_server_config_id(
                            host=str(self.endpoint["host"]),
                            tcp_port=int(self.endpoint["tcp_port"]),
                            config_id=snapshot.config_id,
                        )
                        self.config_updated.emit(self.endpoint_key(), snapshot)
                        self.status.emit(self.endpoint_key(), "配置已更新：{}".format(snapshot.config_id))
                    elif packet.packet_type == UPDATE_AVAILABLE:
                        update = decode_update_available(packet)
                        # Tag the peer that pushed the update so TLS download uses the same host:tcp_port.
                        update["source_host"] = str(self.endpoint.get("host") or "")
                        update["source_tcp_port"] = int(self.endpoint.get("tcp_port") or 0)
                        fingerprint = self.server_fingerprint or self.endpoint.get("fingerprint") or b""
                        if isinstance(fingerprint, (bytes, bytearray)) and len(fingerprint) == 32:
                            update["source_fingerprint"] = bytes(fingerprint)
                        elif isinstance(fingerprint, str) and fingerprint:
                            try:
                                update["source_fingerprint"] = base64.b64decode(
                                    fingerprint + "=" * (-len(fingerprint) % 4)
                                )
                            except (ValueError, TypeError):
                                pass
                        LOGGER.info(
                            "update available component={} version={} platform={} has_token={} force={} seq={} host={}:{}",
                            update["component"],
                            update["version"],
                            update["platform"],
                            bool(update.get("download_token")),
                            bool(update.get("force")),
                            int(update.get("seq") or 0),
                            update.get("source_host"),
                            update.get("source_tcp_port"),
                        )
                        # Queued connection so UI thread is never blocked by TLS download.
                        self.update_available.emit(update)
                        self.status.emit(
                            self.endpoint_key(),
                            "update available: {} {}".format(update["component"], update["version"]),
                        )
                    elif packet.packet_type == PONG:
                        last_pong = time.monotonic()
                    elif packet.packet_type == CLIENT_DISCONNECT:
                        LOGGER.info("connection closed by server administrator")
                        approval_deadline = None
                        self.admin_disconnected = True
                        self.status.emit(self.endpoint_key(), "已由管理员断开；正在按服务端连接模式等待")
                        break
                    elif packet.packet_type == SERVER_SHUTDOWN:
                        normal_shutdown = True
                        # Let the settings UI initiate a manual handshake while
                        # the worker waits on the maintenance retry interval.
                        self.connection_state.emit(self.endpoint_key(), False)
                        self.status.emit(self.endpoint_key(), "服务端正在维护，5 分钟后检测")
                        raise ConnectionError("server_shutdown")
            except Exception as exc:
                if isinstance(exc, ServerFingerprintMismatch):
                    self.certificate_mismatch.emit({
                        "host": str(self.endpoint["host"]),
                        "tcp_port": int(self.endpoint["tcp_port"]),
                        "fingerprint": exc.actual_fingerprint,
                    })
                    self.status.emit(self.endpoint_key(), "服务端证书指纹已变化，等待确认")
                    while self.keep_running:
                        try:
                            approved_fingerprint = self.trust_decisions.get(timeout=0.25)
                            break
                        except queue.Empty:
                            continue
                    else:
                        break
                    if approved_fingerprint is None:
                        self.keep_running = False
                        break
                    self.server_fingerprint = approved_fingerprint
                    self.endpoint["fingerprint"] = approved_fingerprint
                    self.state_store.set_server(
                        host=str(self.endpoint["host"]),
                        tcp_port=int(self.endpoint["tcp_port"]),
                        http_port=int(self.endpoint.get("http_port", 39001)),
                        fingerprint=approved_fingerprint,
                    )
                    continue
                if isinstance(exc, ClientNotApprovedError):
                    if approval_deadline is None:
                        approval_deadline = time.monotonic() + 180
                    remaining = approval_deadline - time.monotonic()
                    if remaining <= 0:
                        LOGGER.error("server authorization timed out after 180 seconds")
                        self.status.emit(self.endpoint_key(), "服务端授权超时，已取消此服务端信任")
                        self.state_store.remove_server(
                            host=str(self.endpoint["host"]),
                            tcp_port=int(self.endpoint["tcp_port"]),
                        )
                        self.keep_running = False
                        break
                    LOGGER.warning("client is waiting for server approval; retrying in 15 seconds")
                    self.status.emit(self.endpoint_key(), "等待服务端批准；每 15 秒重试，最多 3 分钟")
                    self._wait(min(15, remaining))
                    continue
                # All non-authorization failures, including a connection
                # closed after approval, belong to the normal retry policy.
                approval_deadline = None
                if self.keep_running and self.admin_disconnected:
                    LOGGER.warning("reconnect attempt failed while administrator-disconnected: {}", exc)
                    self.status.emit(self.endpoint_key(), "重连未成功；保留原探测计划：{}".format(exc))
                elif self.keep_running:
                    LOGGER.exception(
                        "connection failed {}:{}",
                        self.endpoint.get("host"),
                        self.endpoint.get("tcp_port"),
                    )
                    delay = 300 if normal_shutdown else backoff.next_delay()
                    self.status.emit(self.endpoint_key(), "连接中断：{}；{} 秒后重试".format(exc, delay))
                    self._wait(delay)
                    if delay >= 300 and self.keep_running:
                        try:
                            probe = socket.create_connection(
                                (self.endpoint["host"], self.endpoint["tcp_port"]), timeout=3
                            )
                            probe.close()
                        except OSError:
                            self._wait(300)
                            continue
            finally:
                if connection is not None:
                    connection.close()
                self._active_connection = None
                self.connection_state.emit(self.endpoint_key(), False)
            if self.admin_disconnected and self.keep_running:
                if self.server_connection_mode == "listen":
                    LOGGER.info("admin disconnected; listen mode will not auto-reconnect (manual connect allowed)")
                    self.status.emit(self.endpoint_key(), "admin disconnected; listen mode idle until manual connect")
                    self._manual_reconnect.wait()
                    self._manual_reconnect.clear()
                else:
                    LOGGER.info("admin disconnected; pull mode will probe handshake in 5 minutes")
                    self.status.emit(self.endpoint_key(), "admin disconnected; pull mode probe in 5 minutes")
                    self._manual_reconnect.wait(300)

    def _flush_acks(self, connection: Connection) -> None:
        while True:
            try:
                message_id, status, error = self.acks.get_nowait()
            except queue.Empty:
                return
            self.completed_acks[message_id] = (status, error)
            connection.acknowledge(message_id, status, error)

    def _flush_session_end(self, connection: Connection) -> bool:
        """Send queued ClientSessionEnd on this thread. Returns True if a goodbye was sent."""
        sent = False
        while True:
            try:
                reason, detail = self.session_end_queue.get_nowait()
            except queue.Empty:
                break
            try:
                connection.report_session_end(str(reason), str(detail))
                LOGGER.info(
                    "session end reported host={}:{} reason={} detail={}",
                    self.endpoint.get("host"),
                    self.endpoint.get("tcp_port"),
                    reason,
                    detail or "",
                )
                sent = True
            except Exception as exc:
                LOGGER.warning(
                    "session end report failed host={}:{} reason={} error={}",
                    self.endpoint.get("host"),
                    self.endpoint.get("tcp_port"),
                    reason,
                    exc,
                )
        if sent:
            self._session_end_sent.set()
        return sent

    def _flush_client_logs(self, connection: Connection) -> None:
        for _ in range(20):
            try:
                entry = _CLIENT_LOG_QUEUE.get_nowait()
            except queue.Empty:
                return
            try:
                connection.send(Packet(1, 0, CLIENT_LOG_UPLOAD, 0, 0, encode({"entry": entry})))
            except Exception:
                try:
                    _CLIENT_LOG_QUEUE.put_nowait(entry)
                except queue.Full:
                    pass
                raise

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self.keep_running and time.monotonic() < deadline:
            if self._manual_reconnect.is_set():
                return
            self.msleep(100)


class SpeechWorker(QThread):
    finished_playback = Signal(str, bool, str)
    text_part = Signal(str, str)

    def __init__(self, message_id: str, parts: list[Any], data_dir: Path) -> None:
        super().__init__()
        self.message_id = message_id
        self.parts = parts
        self.data_dir = data_dir
        self.stop_event = threading.Event()
        self.cancelled = False

    def stop(self) -> None:
        self.cancelled = True
        self.stop_event.set()

    def run(self) -> None:
        piper_dir = self.data_dir / "piper"
        model_paths = list(piper_dir.glob("*.onnx")) if piper_dir.is_dir() else []
        engines = [
            SAPIEngine(),
            PiperEngine(
                executable=str(piper_dir / "piper.exe")
                if (piper_dir / "piper.exe").is_file()
                else None,
                model=str(model_paths[0]) if model_paths else None,
            ),
        ]
        try:
            engine_name = speak_with_fallback(
                self.parts,
                engines,
                self.stop_event,
                EnginePreference(self.data_dir / "tts-engine.json"),
                lambda text: self.text_part.emit(self.message_id, text),
            )
            LOGGER.info(
                "TTS playback completed message_id={} engine={}", self.message_id, engine_name
            )
            self.finished_playback.emit(
                self.message_id, not self.cancelled, "cancelled" if self.cancelled else ""
            )
        except Exception as exc:
            if not self.cancelled:
                LOGGER.error("TTS unavailable message_id={} error={}", self.message_id, exc)
            self.finished_playback.emit(self.message_id, False, str(exc))


class MarqueeLabel(QLabel):
    """超长单行消息在可用宽度内向左滚动；含换行时仍居中静态显示（撤回提示应传单行）。

    滚动节奏：开头停顿 ≥0.5s → 在剩余显示时间内滚完整段文字 → 结尾停顿 ≥0.5s。
    若窗口仍可见则循环。显示时长为 0（手动关闭）时使用舒适默认速度，但仍保留首尾停顿。
    """

    _TICK_MS = 40
    _MIN_HOLD_MS = 500
    # Comfortable fallback when the operator leaves the window open forever.
    _DEFAULT_SCROLL_PX_PER_SEC = 80.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source_text = ""
        self._offset = 0.0
        self._text_width = 0
        self._side_margin_px = 0
        self._display_duration_ms = 0
        self._phase = "idle"  # idle | hold_start | scroll | hold_end
        self._phase_elapsed_ms = 0
        self._hold_start_ms = self._MIN_HOLD_MS
        self._hold_end_ms = self._MIN_HOLD_MS
        self._scroll_px_per_tick = 2.0
        self._cycle = 1
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._advance)
        self.setMinimumWidth(1)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._source_text = str(text)
        self._offset = 0.0
        self._phase = "idle"
        self._phase_elapsed_ms = 0
        # Keep an empty QLabel text so sizeHint is driven by our constraints,
        # not the full unwrapped string (which would expand past the screen).
        QLabel.setText(self, "" if "\n" not in self._source_text else self._source_text)
        self._update_marquee()
        self.updateGeometry()
        self.update()

    def set_side_margin_px(self, pixels: int) -> None:
        self._side_margin_px = max(0, int(pixels))
        self._update_marquee()
        self.update()

    def set_display_duration_ms(self, duration_ms: int) -> None:
        """Tell the marquee how long the message window will stay open."""
        self._display_duration_ms = max(0, int(duration_ms))
        self._recompute_scroll_profile()
        if self._needs_marquee() and self._phase == "idle":
            self._begin_cycle()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._update_marquee()

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        if "\n" in self._source_text:
            return hint
        metrics = self.fontMetrics()
        height = max(hint.height(), metrics.height() + 8)
        return hint.__class__(max(1, hint.width()), height)

    def minimumSizeHint(self) -> Any:
        metrics = self.fontMetrics()
        return super().minimumSizeHint().__class__(1, metrics.height() + 8)

    def _content_width(self) -> int:
        return max(1, self.width() - 2 * self._side_margin_px)

    def _needs_marquee(self) -> bool:
        if not self._source_text or "\n" in self._source_text:
            return False
        return self._text_width > self._content_width()

    def _gap(self) -> int:
        return max(32, self.fontMetrics().horizontalAdvance("    "))

    def _recompute_scroll_profile(self) -> None:
        """Pick hold times and px/tick so one full pass fits the display window."""
        gap = self._gap()
        # One full pass: scroll the whole string (+ gap) so every character is seen.
        self._cycle = max(1, self._text_width + gap)
        duration = self._display_duration_ms
        hold = self._MIN_HOLD_MS
        if duration <= 0:
            # Manual close: comfortable constant speed, still pause at ends.
            self._hold_start_ms = hold
            self._hold_end_ms = hold
            px_per_sec = self._DEFAULT_SCROLL_PX_PER_SEC
        else:
            # Always pause ≥0.5s at start and end; spend the rest scrolling so
            # one full pass finishes inside the display window (speed up if needed).
            self._hold_start_ms = hold
            self._hold_end_ms = hold
            scroll_ms = max(self._TICK_MS, duration - 2 * hold)
            px_per_sec = (self._cycle * 1000.0) / float(scroll_ms)
        self._scroll_px_per_tick = max(0.5, px_per_sec * (self._TICK_MS / 1000.0))

    def _begin_cycle(self) -> None:
        self._offset = 0.0
        self._phase_elapsed_ms = 0
        self._recompute_scroll_profile()
        self._phase = "hold_start"
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _update_marquee(self) -> None:
        self._text_width = self.fontMetrics().horizontalAdvance(self._source_text)
        if self._needs_marquee():
            self._recompute_scroll_profile()
            if self._phase == "idle":
                self._begin_cycle()
            elif not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset = 0.0
            self._phase = "idle"
            self._phase_elapsed_ms = 0

    def _advance(self) -> None:
        if not self._needs_marquee():
            self._timer.stop()
            self._offset = 0.0
            self._phase = "idle"
            self.update()
            return
        self._phase_elapsed_ms += self._TICK_MS
        if self._phase == "hold_start":
            if self._phase_elapsed_ms >= self._hold_start_ms:
                self._phase = "scroll"
                self._phase_elapsed_ms = 0
        elif self._phase == "scroll":
            self._offset += self._scroll_px_per_tick
            if self._offset >= self._cycle:
                self._offset = float(self._cycle)
                self._phase = "hold_end"
                self._phase_elapsed_ms = 0
        elif self._phase == "hold_end":
            if self._phase_elapsed_ms >= self._hold_end_ms:
                # Loop while the window stays open (TTS / long ratio / manual).
                self._begin_cycle()
                return
        self.update()

    def paintEvent(self, event: Any) -> None:
        if "\n" in self._source_text or not self._needs_marquee():
            if "\n" not in self._source_text and self._source_text:
                painter = QPainter(self)
                painter.setPen(self.palette().color(self.foregroundRole()))
                painter.setFont(self.font())
                rect = self.rect().adjusted(self._side_margin_px, 0, -self._side_margin_px, 0)
                painter.drawText(rect, int(Qt.AlignCenter | Qt.AlignVCenter), self._source_text)
                painter.end()
                return
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.setClipRect(self.rect().adjusted(self._side_margin_px, 0, -self._side_margin_px, 0))
        baseline = (self.height() + self.fontMetrics().ascent() - self.fontMetrics().descent()) // 2
        cycle = max(1, self._cycle)
        x = self._side_margin_px - int(self._offset)
        right = self.width() - self._side_margin_px
        while x < right:
            painter.drawText(x, baseline, self._source_text)
            x += cycle
        painter.end()


class MessageDisplayDialog(QDialog):
    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)


class ClientWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.instance_lock: Optional[SingleInstanceLock] = None
        self.data_dir = _data_dir()
        _configure_logging(self.data_dir)
        LOGGER.info("client started")
        self.state_store = StateStore(self.data_dir / "state.json")
        self.private_key, self.certificate = _identity_files(self.data_dir)
        self.servers: list[DiscoveryResponse] = []
        self.scan_worker: Optional[ScanWorker] = None
        # Concurrent TLS workers keyed by (host, tcp_port).
        self.connection_workers: dict[tuple[str, int], ConnectionWorker] = {}
        self.listener_worker: Optional[ListenerWorker] = None
        self._connection_attempts: set[tuple[str, int]] = set()
        self._connected_endpoint_keys: set[tuple[str, int]] = set()
        self._stopping_workers: list[ConnectionWorker] = []
        self.dedup = MessageDeduplicator()
        self.pending_messages = PriorityQueue()
        self.current_message: Optional[IncomingMessage] = None
        self.current_speech: Optional[SpeechWorker] = None
        self.current_displayed = False
        self.current_speech_done = False
        self.current_speech_success = False
        self._config_snapshot: Optional[ConfigSnapshot] = None
        # Per-server runtime state so multi-server configs do not clobber each other.
        self._server_display_defaults: dict[tuple[str, int], tuple[str, float]] = {}
        self._server_config_ids: dict[tuple[str, int], str] = {}
        # Deduplicate concurrent UPDATE_AVAILABLE pushes for the same package digest.
        # Seed from disk so a force catch-up after restart cannot re-apply the same sha.
        self._update_apply_in_progress: set[str] = set()
        self._update_apply_done: set[str] = set(load_applied_digests(self.data_dir))
        self._update_workers: list[UpdateApplyWorker] = []
        self._server_status_text: dict[tuple[str, int], str] = {}
        self.current_error = ""
        self.display_position = "center"
        self.display_duration_ratio = 0.4
        self.withdrawn_ids: set[str] = set()
        self.setWindowTitle("lovemilk class broadcaster")
        self.resize(720, 520)
        self._build_ui()
        self._build_display()
        self._build_tray()
        self._load_saved_server()
        self._refresh_autostart_ui()
        self._ensure_default_autostart()
        self._load_listener_settings()
        # Startup reconnects only endpoints marked auto_connect (set by Connect).
        for saved in self.state_store.servers():
            if not bool(saved.get("auto_connect", True)):
                continue
            fingerprint = b""
            try:
                fingerprint = base64.b64decode(saved.get("fingerprint", ""), validate=True)
            except (TypeError, ValueError):
                fingerprint = b""
            if not fingerprint or not any(fingerprint):
                continue
            config_id = saved.get("config_id")
            if isinstance(config_id, str) and config_id:
                self._server_config_ids[(str(saved["host"]), int(saved["tcp_port"]))] = config_id
            self._start_connection(
                {
                    "host": str(saved["host"]),
                    "tcp_port": int(saved["tcp_port"]),
                    "http_port": int(saved.get("http_port", 39003) or 39003),
                    "fingerprint": fingerprint,
                }
            )

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("lovemilk class broadcaster")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        settings_layout = QHBoxLayout()
        settings_layout.setSpacing(16)
        self.settings_categories = QListWidget()
        self.settings_categories.setFixedWidth(150)
        self.settings_categories.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_categories.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_categories.addItems(["连接与服务端", "客户端身份", "系统", "关于"])
        self.settings_categories.currentRowChanged.connect(self.settings_pages_index_changed)
        settings_layout.addWidget(self.settings_categories)

        self.settings_pages = QStackedWidget()
        settings_layout.addWidget(self.settings_pages, 1)
        layout.addLayout(settings_layout, 1)

        identity = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.client_fingerprint = _public_key_fingerprint(identity)
        self.identity_label = QLabel("客户端公钥指纹：\n{}".format(self.client_fingerprint))
        self.identity_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        connection_page = QWidget()
        connection_layout = QVBoxLayout(connection_page)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_group = QGroupBox("连接与服务端")
        connection_group_layout = QVBoxLayout(connection_group)
        form = QFormLayout()
        self.destination = QLineEdit("255.255.255.255")
        self.destination.setPlaceholderText("例如 255.255.255.255、192.168.1.0/24、192.168.1.10 或 auto")
        self.destination.returnPressed.connect(self.scan_servers)
        scan_address_row = QWidget()
        scan_address_layout = QHBoxLayout(scan_address_row)
        scan_address_layout.setContentsMargins(0, 0, 0, 0)
        scan_address_layout.addWidget(self.destination, 1)
        self.scan_button = QPushButton("扫描服务端")
        self.scan_button.clicked.connect(self.scan_servers)
        scan_address_layout.addWidget(self.scan_button)
        form.addRow("扫描地址", scan_address_row)
        connection_group_layout.addLayout(form)
        manual_row = QHBoxLayout()
        self.manual_host = QLineEdit()
        self.manual_host.setPlaceholderText("服务器 IP 或主机名")
        self.manual_port = QSpinBox()
        self.manual_port.setRange(1, 65535)
        self.manual_port.setValue(39002)
        manual_row.addWidget(self.manual_host, 1)
        manual_row.addWidget(self.manual_port)
        self.manual_host.returnPressed.connect(self.connect_manual_server)
        self.manual_port.lineEdit().returnPressed.connect(self.connect_manual_server)
        self.manual_connect_button = QPushButton("连接")
        self.manual_connect_button.clicked.connect(self.connect_manual_server)
        manual_row.addWidget(self.manual_connect_button)
        connection_group_layout.addWidget(QLabel("手动连接（连接 = 保存 + 连接 + 开机自动连接）"))
        connection_group_layout.addLayout(manual_row)

        lists_row = QHBoxLayout()
        lists_row.setSpacing(12)

        scanned_column = QVBoxLayout()
        scanned_column.addWidget(QLabel("扫描到的服务端"))
        self.scanned_list = QListWidget()
        self.scanned_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.scanned_list.currentRowChanged.connect(lambda _row: self._on_scanned_selection_changed())
        self.scanned_list.itemDoubleClicked.connect(lambda _item: self.connect_selected())
        scanned_column.addWidget(self.scanned_list, 1)
        scanned_actions = QHBoxLayout()
        self.connect_scanned_button = QPushButton("连接")
        self.connect_scanned_button.clicked.connect(self.connect_selected)
        scanned_actions.addWidget(self.connect_scanned_button)
        scanned_column.addLayout(scanned_actions)
        lists_row.addLayout(scanned_column, 1)

        saved_column = QVBoxLayout()
        saved_column.addWidget(QLabel("已保存 / 已连接"))
        self.saved_list = QListWidget()
        self.saved_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.saved_list.currentRowChanged.connect(lambda _row: self._on_saved_selection_changed())
        self.saved_list.itemDoubleClicked.connect(lambda _item: self.connect_selected())
        saved_column.addWidget(self.saved_list, 1)
        saved_actions = QHBoxLayout()
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self.connect_selected)
        saved_actions.addWidget(self.connect_button)
        self.remove_server_button = QPushButton("删除")
        self.remove_server_button.clicked.connect(self.remove_selected_server)
        self.remove_server_button.setEnabled(False)
        saved_actions.addWidget(self.remove_server_button)
        saved_column.addLayout(saved_actions)
        lists_row.addLayout(saved_column, 1)

        connection_group_layout.addLayout(lists_row, 1)
        # Keep a single "active" selection source: "scanned" | "saved".
        self._selection_source = "saved"
        self.fingerprint_label = QLabel(
            "左侧扫描、右侧已保存。连接 = 保存 + 连接 + 自动连接；删除只删除。"
        )
        self.fingerprint_label.setWordWrap(True)
        connection_group_layout.addWidget(self.fingerprint_label)
        self.config_status_label = QLabel("配置：—")
        self.config_status_label.setWordWrap(True)
        connection_group_layout.addWidget(self.config_status_label)
        self.status_label = QLabel("未连接")
        self.status_label.setWordWrap(True)
        connection_group_layout.addWidget(self.status_label)
        connection_layout.addWidget(connection_group, 1)
        self.settings_pages.addWidget(connection_page)

        identity_page = QWidget()
        identity_layout = QVBoxLayout(identity_page)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_group = QGroupBox("客户端身份")
        identity_group_layout = QVBoxLayout(identity_group)
        identity_group_layout.addWidget(self.identity_label)
        copy_identity_button = QPushButton("复制 SHA256 指纹")
        copy_identity_button.clicked.connect(self.copy_identity_fingerprint)
        identity_group_layout.addWidget(copy_identity_button)
        identity_group_layout.addWidget(
            QLabel("此 SHA-256 指纹是客户端在服务端信任列表中的唯一标识。")
        )
        identity_group_layout.addStretch(1)
        identity_layout.addWidget(identity_group, 1)
        self.settings_pages.addWidget(identity_page)

        system_page = QWidget()
        system_layout = QVBoxLayout(system_page)
        system_layout.setContentsMargins(0, 0, 0, 0)
        system_group = QGroupBox("系统")
        system_group_layout = QVBoxLayout(system_group)
        self.autostart = QCheckBox("登录 Windows 时自动启动（默认开启）")
        self.autostart.toggled.connect(self._toggle_autostart)
        self.autostart_machine = QCheckBox("写入本机（所有用户，将弹出 UAC 提权）")
        self.autostart_machine.toggled.connect(self._toggle_autostart_machine)
        self.autostart_label = QLabel(
            "默认写入当前用户启动项（HKCU）。勾选“本机”将通过 reg 写入 HKLM（所有用户），"
            "并自动请求管理员授权（UAC）；取消自启动会同时清理 HKCU/HKLM。"
        )
        self.autostart_label.setWordWrap(True)
        system_group_layout.addWidget(self.autostart)
        system_group_layout.addWidget(self.autostart_machine)
        system_group_layout.addWidget(self.autostart_label)
        listener_form = QFormLayout()
        self.listener_port = QSpinBox()
        self.listener_port.setRange(1, 65535)
        self.listener_port.setValue(39004)
        listener_form.addRow("连接模式", QLabel("由服务端管理（客户端仅上报能力）"))
        listener_form.addRow("监听端口", self.listener_port)
        self.listener_save = QPushButton("保存监听端口")
        self.listener_save.clicked.connect(self._save_listener_settings)
        listener_form.addRow("", self.listener_save)
        system_group_layout.addLayout(listener_form)
        system_group_layout.addStretch(1)
        system_layout.addWidget(system_group, 1)
        self.settings_pages.addWidget(system_page)

        about_page = QWidget()
        about_layout = QVBoxLayout(about_page)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_group = QGroupBox("关于")
        about_group_layout = QVBoxLayout(about_group)
        about_group_layout.addWidget(QLabel("lovemilk class broadcaster"))
        about_group_layout.addWidget(QLabel("客户端配置与叫号接收程序"))
        about_version_text = display_version()
        self.about_version_button = QPushButton(f"版本 {about_version_text}（复制）")
        self.about_version_button.setToolTip(
            "点击复制版本与构建时间；连续点击 7 次打开开发者面板"
        )
        self.about_version_button.clicked.connect(self.about_version_clicked)
        about_group_layout.addWidget(self.about_version_button)
        build_label = QLabel(
            "构建时间：{}".format(BUILD_DATE if BUILD_DATE else "开发版（未打构建戳）")
        )
        build_label.setStyleSheet("color: #666;")
        about_group_layout.addWidget(build_label)
        about_group_layout.addStretch(1)
        about_layout.addWidget(about_group, 1)
        self.settings_pages.addWidget(about_page)

        self.settings_categories.setCurrentRow(0)
        self.setCentralWidget(root)

    def _load_listener_settings(self) -> None:
        mode, port = self.state_store.listener_settings()
        if hasattr(self, "listener_port"):
            self.listener_port.setValue(port)
        if mode in ("auto", "listen"):
            self._start_listener(port)

    def _save_listener_settings(self) -> None:
        # 连接模式由服务端强制策略决定，客户端设置页只允许调整监听端口。
        mode, _ = self.state_store.listener_settings()
        port = int(self.listener_port.value())
        try:
            self.state_store.set_listener_settings(mode, port)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self._stop_listener()
        if mode in ("auto", "listen"):
            self._start_listener(port)
        self.status_label.setText("listener port saved")
        LOGGER.info("listener port saved port={}", port)

    def _listener_idle_timeout_seconds(self) -> int:
        """Prefer last handshake snapshot idle; fall back to 60s default."""
        timeout = ListenerWorker.DEFAULT_IDLE_TIMEOUT_SECONDS
        if self._config_snapshot is not None:
            try:
                timeout = int(self._config_snapshot.listener_idle_timeout_seconds or timeout)
            except (TypeError, ValueError, AttributeError):
                pass
        else:
            for worker in self.connection_workers.values():
                if worker.last_snapshot is not None:
                    try:
                        timeout = int(worker.last_snapshot.listener_idle_timeout_seconds or timeout)
                        break
                    except (TypeError, ValueError, AttributeError):
                        continue
        return max(1, min(3600, timeout))

    def _start_listener(self, port: int) -> None:
        if self.listener_worker is not None and self.listener_worker.isRunning():
            # Refresh idle timeout from the latest snapshot without restarting.
            self.listener_worker.idle_timeout_seconds = self._listener_idle_timeout_seconds()
            return
        self.listener_worker = ListenerWorker(
            self.private_key,
            self.certificate,
            port,
            self.state_store,
            idle_timeout_seconds=self._listener_idle_timeout_seconds(),
        )
        self.listener_worker.status.connect(self._listener_status)
        self.listener_worker.start()

    def _stop_listener(self) -> None:
        if self.listener_worker is None:
            return
        worker = self.listener_worker
        self.listener_worker = None
        worker.stop()
        worker.wait(2000)

    def _listener_status(self, status: str) -> None:
        LOGGER.info("{}", status)
        if self.listener_worker is not None:
            self.status_label.setText(status)

    def settings_pages_index_changed(self, index: int) -> None:
        self.settings_pages.setCurrentIndex(max(0, index))

    def copy_identity_fingerprint(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.client_fingerprint)

    def about_version_clicked(self) -> None:
        now = time.monotonic()
        if now - getattr(self, "_about_last_click", 0.0) > 1.0:
            self._about_clicks = 0
        self._about_last_click = now
        self._about_clicks = getattr(self, "_about_clicks", 0) + 1
        if self._about_clicks >= 7:
            self._about_clicks = 0
            self._show_developer_panel()
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(display_version())
            self.status_label.setText("已复制版本信息：{}".format(display_version()))

    def _show_developer_panel(self) -> None:
        if not hasattr(self, "developer_dialog"):
            dialog = QDialog(self)
            dialog.setWindowTitle("lovemilk class broadcaster - Developer")
            dialog.setModal(False)
            dialog.resize(760, 480)
            layout = QVBoxLayout(dialog)
            log_view = QPlainTextEdit(dialog)
            log_view.setReadOnly(True)
            log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            log_view.setPlaceholderText("No client logs")
            layout.addWidget(log_view, 1)
            actions = QHBoxLayout()
            refresh = QPushButton("Refresh", dialog)
            refresh.clicked.connect(lambda: self._refresh_developer_log(log_view))
            actions.addWidget(refresh)
            copy = QPushButton("Copy all", dialog)
            copy.clicked.connect(lambda: self._copy_developer_log(log_view))
            actions.addWidget(copy)
            actions.addStretch(1)
            close = QPushButton("Close", dialog)
            close.clicked.connect(dialog.hide)
            actions.addWidget(close)
            exit_button = QPushButton("Exit client", dialog)
            exit_button.setToolTip("Stop the client and exit the process")
            exit_button.clicked.connect(self.quit_application)
            actions.addWidget(exit_button)
            layout.addLayout(actions)
            self.developer_dialog = dialog
            self.developer_log_view = log_view
        self._refresh_developer_log(self.developer_log_view)
        self.developer_dialog.show()
        self.developer_dialog.raise_()
        self.developer_dialog.activateWindow()

    def _refresh_developer_log(self, view: QPlainTextEdit) -> None:
        view.setPlainText(_read_client_log(self.data_dir))
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())

    def _copy_developer_log(self, view: QPlainTextEdit) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(view.toPlainText())

    def _build_display(self) -> None:
        # Keep the message window independent from the settings window. This
        # prevents showing a notification from activating the settings page.
        self.display = MessageDisplayDialog()
        self.display.setWindowTitle("lovemilk class broadcaster")
        self.display.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.display.resize(900, 360)
        layout = QVBoxLayout(self.display)
        # Side text margin 0.25rem relative to content font size (1rem = font px).
        content_font_px = 38
        side_margin_px = max(1, int(round(content_font_px * 0.25)))
        layout.setContentsMargins(side_margin_px, 12, side_margin_px, 12)
        layout.setSpacing(8)
        self.display_time = QLabel()
        self.display_time.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.display_time.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self.display_time)
        self.display_content = MarqueeLabel()
        self.display_content.setAlignment(Qt.AlignCenter)
        self.display_content.setWordWrap(False)
        self.display_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.display_content.setStyleSheet(
            "font-size: {}px; font-weight: 600;".format(content_font_px)
        )
        self.display_content.set_side_margin_px(side_margin_px)
        layout.addWidget(self.display_content, 1)
        close_row = QHBoxLayout()
        self.display_close_button = QPushButton("关闭")
        self.display_close_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.display_close_button.setMinimumHeight(22)
        self.display_close_button.clicked.connect(self._dismiss_current)
        self.display_close_button.hide()
        close_row.addWidget(self.display_close_button)
        layout.addLayout(close_row)
        self.display_timer = QTimer(self)
        self.display_timer.setSingleShot(True)
        self.display_timer.timeout.connect(self._display_minimum_elapsed)
        self.withdrawal_timer = QTimer(self)
        self.withdrawal_timer.setSingleShot(True)
        self.withdrawal_timer.timeout.connect(self._withdrawal_display_elapsed)
        self.withdrawal_notice_active = False
        self.withdrawal_displayed = False
        self.withdrawal_speech_done = True
        self.withdrawal_speech: Optional[SpeechWorker] = None
        self._withdrawal_speech_id = "withdrawal-notice"

    def _config_updated(self, endpoint_key: object, snapshot: ConfigSnapshot) -> None:
        # Keep per-server defaults; only apply global UI defaults when this endpoint
        # is the currently selected server (or no selection yet).
        key: Optional[tuple[str, int]] = None
        if isinstance(endpoint_key, tuple) and len(endpoint_key) == 2:
            key = (str(endpoint_key[0]), int(endpoint_key[1]))
            self._server_display_defaults[key] = (
                snapshot.display_position,
                float(snapshot.display_duration_ratio),
            )
            self._server_config_ids[key] = snapshot.config_id
            self.state_store.set_server_config_id(
                host=key[0], tcp_port=key[1], config_id=snapshot.config_id
            )
        selected = self._selected_list_item()
        selected_key = self._endpoint_key(selected.data(Qt.UserRole)) if selected is not None else None
        if key is None or selected_key is None or selected_key == key:
            self._config_snapshot = snapshot
            self.display_position = snapshot.display_position
            self.display_duration_ratio = snapshot.display_duration_ratio
        self._refresh_selection_details()
        if self.listener_worker is not None:
            self.listener_worker.idle_timeout_seconds = self._listener_idle_timeout_seconds()
        if self.display.isVisible() and (selected_key is None or selected_key == key):
            self._position_display()

    def _position_display(self, position: Optional[str] = None) -> None:
        screen = self.display.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        margin = 24
        # Cap at 95% of the screen that hosts this window; force width first so
        # MarqueeLabel measures against the real viewport and starts scrolling.
        max_width = max(1, math.ceil(area.width() * 0.95))
        self.display.setMaximumWidth(max_width)
        self.display_content.setMaximumWidth(max_width)
        # Preferred height from content, width never exceeds 95% screen.
        self.display.adjustSize()
        natural_width = self.display.sizeHint().width()
        # Short messages can shrink; long messages fill up to max_width and marquee.
        text = getattr(self.display_content, "_source_text", "") or ""
        metrics = self.display_content.fontMetrics()
        side = max(0, int(getattr(self.display_content, "_side_margin_px", 0)))
        layout_margins = self.display.layout().contentsMargins() if self.display.layout() is not None else None
        outer_side = (layout_margins.left() + layout_margins.right()) if layout_margins is not None else 0
        text_width = metrics.horizontalAdvance(text.split("\n", 1)[0]) if text else 0
        needed = text_width + 2 * side + outer_side + 8
        width = min(max_width, max(320, needed if "\n" not in text else natural_width, natural_width))
        width = min(width, max_width)
        height = min(max(self.display.sizeHint().height(), 160), max(1, area.height() - margin * 2))
        self.display.resize(width, height)
        content_width = max(1, width - outer_side)
        self.display_content.setMinimumWidth(1)
        self.display_content.setMaximumWidth(content_width)
        self.display_content.resize(content_width, max(1, self.display_content.height()))
        self.display_content._update_marquee()
        horizontal = {
            "left": area.left() + margin,
            "center": area.left() + (area.width() - width) // 2,
            "right": area.right() - width - margin,
        }
        vertical = {
            "top": area.top() + margin,
            "center": area.top() + (area.height() - height) // 2,
            "bottom": area.bottom() - height - margin,
        }
        position = position or self.display_position
        position = (
            position
            if position
            in {
                "top-left",
                "top",
                "top-right",
                "left",
                "center",
                "right",
                "bottom-left",
                "bottom",
                "bottom-right",
            }
            else "center"
        )
        column, row = {
            "top-left": ("left", "top"),
            "top": ("center", "top"),
            "top-right": ("right", "top"),
            "left": ("left", "center"),
            "center": ("center", "center"),
            "right": ("right", "center"),
            "bottom-left": ("left", "bottom"),
            "bottom": ("center", "bottom"),
            "bottom-right": ("right", "bottom"),
        }[position]
        x = max(area.left() + margin, min(horizontal[column], area.left() + area.width() - width - margin))
        y = max(area.top() + margin, min(vertical[row], area.top() + area.height() - height - margin))
        point = QPoint(x, y)
        self.display.move(point)
        window_handle = self.display.windowHandle()
        if window_handle is not None:
            window_handle.setPosition(point)

    def _raise_display(self) -> None:
        """每次消息显示时重新声明顶层窗口，兼容 Linux 窗口管理器。"""
        self.display.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.display.raise_()
        self.display.activateWindow()
        window_handle = self.display.windowHandle()
        if window_handle is not None:
            window_handle.setFlag(Qt.WindowStaysOnTopHint, True)
            window_handle.raise_()

    def _show_display(self, position: Optional[str] = None) -> None:
        """显示无边框消息窗并在窗口管理器完成映射后再次定位和置顶。"""
        self.display.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.display.show()
        self._raise_display()
        QTimer.singleShot(0, lambda: self._position_display(position))
        QTimer.singleShot(0, self._raise_display)

    def _start_connection(self, endpoint: dict[str, Any]) -> None:
        endpoint_key = (str(endpoint["host"]), int(endpoint["tcp_port"]))
        existing = self.connection_workers.get(endpoint_key)
        if existing is not None and existing.isRunning():
            # Already connected/connecting this endpoint; nudge a manual reconnect.
            existing.request_manual_reconnect()
            self._update_connect_button()
            return
        if existing is not None:
            self.connection_workers.pop(endpoint_key, None)
        worker = ConnectionWorker(endpoint, self.private_key, self.certificate, self.state_store, self.data_dir)
        LOGGER.info("starting connection {}:{}", endpoint.get("host"), endpoint.get("tcp_port"))
        self.connection_workers[endpoint_key] = worker
        self._connection_attempts.add(endpoint_key)
        self._update_remove_button()
        worker.status.connect(self._connection_status_changed)
        worker.connection_state.connect(self._connection_state_changed)
        worker.finished.connect(lambda key=endpoint_key, w=worker: self._connection_worker_finished(key, w))
        worker.config_updated.connect(self._config_updated)
        worker.certificate_mismatch.connect(self._confirm_server_fingerprint_change)
        worker.received.connect(self._message_received)
        worker.withdrawn.connect(self._message_withdrawn)
        # QueuedConnection keeps the connection thread free while UI schedules the apply worker.
        worker.update_available.connect(self._update_available, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._update_connect_button()

    def _connection_worker_finished(self, endpoint_key: tuple[str, int], worker: ConnectionWorker) -> None:
        current = self.connection_workers.get(endpoint_key)
        if current is worker:
            self.connection_workers.pop(endpoint_key, None)
        self._connected_endpoint_keys.discard(endpoint_key)
        self._update_connect_button()
        self._refresh_server_list_states()

    def _update_available(self, update: dict[str, Any]) -> None:
        """Schedule download/verify/apply on a background thread (never block the UI loop)."""
        if not isinstance(update, dict):
            LOGGER.error("update available ignored: payload is not a dict type={}", type(update).__name__)
            return
        digest = str(update.get("sha256") or "").lower()
        target_client_version = str(update.get("client_version") or "").strip()
        LOGGER.info(
            "update available received on UI thread sha256={} version={} client_version={} force={} has_token={}",
            digest,
            update.get("version"),
            target_client_version or "-",
            bool(update.get("force")),
            bool(update.get("download_token")),
        )
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            LOGGER.error("update available ignored: invalid sha256={!r}", update.get("sha256"))
            return
        # Ignore packages we already applied (persisted across restart) or whose
        # client_version we already meet/exceed — stops force catch-up restart loops.
        if is_update_applied(self.data_dir, digest) or digest in self._update_apply_done:
            LOGGER.info("update apply skipped (already applied) sha256={}", digest)
            self._update_apply_done.add(digest)
            return
        if target_client_version and not _client_version_behind(VERSION_STR, target_client_version):
            LOGGER.info(
                "update apply skipped (client_version={} meets/exceeds target={}) sha256={}",
                VERSION_STR,
                target_client_version,
                digest,
            )
            mark_update_applied(self.data_dir, digest)
            self._update_apply_done.add(digest)
            return
        if digest in self._update_apply_in_progress:
            LOGGER.info("update apply skipped (already in progress) sha256={}", digest)
            return
        # Ensure peer routing fields even if the signal payload lost them.
        if not update.get("source_host") or not update.get("source_tcp_port"):
            for worker in list(self.connection_workers.values()):
                endpoint = getattr(worker, "endpoint", None) or {}
                host = str(endpoint.get("host") or "").strip()
                try:
                    port = int(endpoint.get("tcp_port") or 0)
                except (TypeError, ValueError):
                    port = 0
                if host and port > 0:
                    update["source_host"] = host
                    update["source_tcp_port"] = port
                    fp = getattr(worker, "server_fingerprint", None) or endpoint.get("fingerprint") or b""
                    if isinstance(fp, (bytes, bytearray)) and len(fp) == 32:
                        update["source_fingerprint"] = bytes(fp)
                    break
        self._update_apply_in_progress.add(digest)
        apply_worker = UpdateApplyWorker(
            update,
            self.data_dir,
            self.certificate,
            self.private_key,
            parent=self,
        )
        self._update_workers.append(apply_worker)

        def _on_ok(sha: str, worker_ref: UpdateApplyWorker = apply_worker) -> None:
            self._update_apply_done.add(sha)
            mark_update_applied(self.data_dir, sha)
            self._update_apply_in_progress.discard(sha)
            try:
                self._update_workers.remove(worker_ref)
            except ValueError:
                pass
            LOGGER.info("update apply worker finished ok sha256={}", sha)

        def _on_err(sha: str, error: str, worker_ref: UpdateApplyWorker = apply_worker) -> None:
            self._update_apply_in_progress.discard(sha)
            # Allow a later force catch-up / republish to retry the same digest.
            try:
                self._update_workers.remove(worker_ref)
            except ValueError:
                pass
            LOGGER.error("update apply worker finished error sha256={} error={}", sha, error)

        apply_worker.finished_ok.connect(_on_ok, Qt.ConnectionType.QueuedConnection)
        apply_worker.finished_error.connect(_on_err, Qt.ConnectionType.QueuedConnection)
        apply_worker.quit_for_update.connect(self._quit_for_update, Qt.ConnectionType.QueuedConnection)
        apply_worker.start()
        LOGGER.info("update apply worker started sha256={}", digest)

    def _quit_for_update(self) -> None:
        """Stop workers and exit so the detached updater can replace PE files and restart us."""
        if getattr(self, "_exiting_for_update", False):
            return
        self._exiting_for_update = True
        LOGGER.info("update client exiting to allow file replacement")
        try:
            if getattr(self, "tray", None) is not None:
                try:
                    self.tray.hide()
                except Exception:
                    pass
            # Tell the server this drop is intentional (update restart), not a crash.
            self._report_session_end_all("update", "apply_update")
            for worker in list(self.connection_workers.values()):
                try:
                    worker.requestInterruption()
                    worker.stop()
                    worker.quit()
                except Exception:
                    pass
            if self.listener_worker is not None:
                try:
                    self.listener_worker.requestInterruption()
                    self.listener_worker.quit()
                except Exception:
                    pass
            for worker in list(self._update_workers):
                try:
                    worker.requestInterruption()
                except Exception:
                    pass
        finally:
            # Brief delay so the detached updater can observe our still-alive PID, then force exit.
            QTimer.singleShot(400, self._quit_for_update_now)

    def _quit_for_update_now(self) -> None:
        """Release the single-instance lock and terminate; tray apps may ignore app.quit() alone."""
        LOGGER.info("update client force quit")
        try:
            if getattr(self, "tray", None) is not None:
                self.tray.hide()
        except Exception:
            pass
        try:
            self.shutdown()
        except Exception:
            LOGGER.exception("shutdown during update quit failed")
        app = QApplication.instance()
        if app is not None:
            try:
                app.quit()
            except Exception:
                pass
        # Hard exit fallback: Qt tray apps with setQuitOnLastWindowClosed(False) can linger.
        QTimer.singleShot(600, lambda: os._exit(0))

    @staticmethod
    def _update_package_path(update_dir: Path, digest: str) -> Path:
        """Return canonical ``<sha256>.tar.zst`` path; migrate legacy ``.zst`` if present."""
        canonical = update_dir / f"{digest}.tar.zst"
        if canonical.is_file():
            return canonical
        legacy = update_dir / f"{digest}.zst"
        if legacy.is_file():
            try:
                legacy.replace(canonical)
                LOGGER.info("migrated legacy update package {} -> {}", legacy, canonical)
            except OSError as exc:
                LOGGER.warning("could not rename legacy update package {} -> {}: {}", legacy, canonical, exc)
                return legacy
        return canonical

    def _confirm_server_fingerprint_change(self, event: dict[str, Any]) -> None:
        fingerprint = bytes(event.get("fingerprint", b""))
        displayed = "SHA256:" + base64.b64encode(fingerprint).decode("ascii").rstrip("=")
        answer = QMessageBox.warning(
            self,
            "服务端公钥变化",
            "服务端 {}:{} 的公钥指纹已变化，是否更新信任？\n{}".format(
                event.get("host"), event.get("tcp_port"), displayed
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        endpoint_key = (str(event.get("host", "")), int(event.get("tcp_port", 0) or 0))
        worker = self.connection_workers.get(endpoint_key)
        if worker is not None:
            worker.accept_server_fingerprint(
                fingerprint if answer == QMessageBox.StandardButton.Yes else None
            )

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_application_icon())
        menu = QMenu()
        show_action = QAction("设置", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)
        # Exit is only available from the developer panel (version click), not the tray.
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
            )
        )
        # Some Windows environments report tray as unavailable until the shell is ready.
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            self.tray.setToolTip("lovemilk class broadcaster")
        else:
            LOGGER.warning("system tray unavailable; settings window will stay visible")

    def _selected_list_item(self) -> Optional[QListWidgetItem]:
        """Active selection prefers the side the user last clicked."""
        if self._selection_source == "scanned":
            item = self.scanned_list.currentItem()
            if item is not None:
                return item
        item = self.saved_list.currentItem()
        if item is not None:
            return item
        return self.scanned_list.currentItem()

    def _on_scanned_selection_changed(self) -> None:
        if self.scanned_list.currentRow() < 0:
            return
        self._selection_source = "scanned"
        self.saved_list.blockSignals(True)
        self.saved_list.clearSelection()
        self.saved_list.blockSignals(False)
        self._refresh_selection_details()
        self._update_remove_button()
        self._update_connect_button()

    def _on_saved_selection_changed(self) -> None:
        if self.saved_list.currentRow() < 0:
            return
        self._selection_source = "saved"
        self.scanned_list.blockSignals(True)
        self.scanned_list.clearSelection()
        self.scanned_list.blockSignals(False)
        self._refresh_selection_details()
        self._update_remove_button()
        self._update_connect_button()

    def _refresh_selection_details(self) -> None:
        """Show fingerprint/config/status for the active selection only."""
        item = self._selected_list_item()
        self._show_fingerprint_for_item(item)
        key = self._endpoint_key(item.data(Qt.UserRole)) if item is not None else None
        if key is None:
            self.config_status_label.setText("配置：—")
            if not self._server_status_text:
                self.status_label.setText("未连接")
            return
        config_id = self._server_config_ids.get(key)
        if not config_id:
            for saved in self.state_store.servers():
                if self._endpoint_key(saved) == key:
                    candidate = saved.get("config_id")
                    if isinstance(candidate, str) and candidate:
                        config_id = candidate
                        self._server_config_ids[key] = candidate
                    break
        worker = self.connection_workers.get(key)
        if worker is not None and worker.last_snapshot is not None:
            config_id = worker.last_snapshot.config_id
            self._server_config_ids[key] = config_id
            defaults = self._server_display_defaults.get(key)
            if defaults is None:
                self._server_display_defaults[key] = (
                    worker.last_snapshot.display_position,
                    float(worker.last_snapshot.display_duration_ratio),
                )
        if config_id:
            defaults = self._server_display_defaults.get(key)
            if defaults:
                self.config_status_label.setText(
                    "配置：{}（显示 {} / 比率 {}）".format(config_id, defaults[0], defaults[1])
                )
            else:
                self.config_status_label.setText("配置：{}".format(config_id))
        else:
            state = self._connection_state_label(key)
            self.config_status_label.setText(
                "配置：连接后同步" if state != "已连接" else "配置：尚未收到 snapshot"
            )
        status = self._server_status_text.get(key)
        if status:
            self.status_label.setText("{}:{} — {}".format(key[0], key[1], status))
        else:
            self.status_label.setText(
                "{}:{} — {}".format(key[0], key[1], self._connection_state_label(key))
            )

    def _connection_state_label(self, endpoint_key: Optional[tuple[str, int]]) -> str:
        if endpoint_key is None:
            return "未连接"
        if endpoint_key in self._connected_endpoint_keys:
            return "已连接"
        worker = self.connection_workers.get(endpoint_key)
        if worker is not None and worker.isRunning():
            return "连接中"
        if endpoint_key in self._connection_attempts:
            return "连接中"
        return "未连接"

    def _load_saved_server(self) -> None:
        previous_key = None
        current = self.saved_list.currentItem()
        if current is not None:
            previous_key = self._endpoint_key(current.data(Qt.UserRole))
        self.saved_list.clear()
        select_row = 0
        for index, saved in enumerate(self.state_store.servers()):
            host = str(saved.get("host", ""))
            port = int(saved.get("tcp_port", 0) or 0)
            base = "{}:{}".format(host, port)
            endpoint_key = self._endpoint_key(saved)
            state = self._connection_state_label(endpoint_key)
            item = QListWidgetItem("{}  [{}]".format(base, state))
            item.setData(Qt.UserRole, saved)
            item.setData(Qt.UserRole + 1, base)
            self.saved_list.addItem(item)
            if previous_key is not None and endpoint_key == previous_key:
                select_row = index
        if self.saved_list.count():
            self.saved_list.setCurrentRow(select_row)
            self._selection_source = "saved"
        self._update_remove_button()
        self._update_connect_button()

    def _saved_endpoint_keys(self) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        for saved in self.state_store.servers():
            key = self._endpoint_key(saved)
            if key is not None:
                keys.add(key)
        return keys

    def _load_scanned_servers(self, servers: Optional[list[DiscoveryResponse]] = None) -> None:
        if servers is not None:
            self.servers = list(servers)
        previous_key = None
        current = self.scanned_list.currentItem()
        if current is not None:
            previous_key = self._endpoint_key(current.data(Qt.UserRole))
        self.scanned_list.clear()
        saved_keys = self._saved_endpoint_keys()
        select_row = 0
        visible_index = 0
        for server in self.servers:
            endpoint_key = self._endpoint_key(server)
            # Already-saved servers belong only on the right column.
            if endpoint_key is not None and endpoint_key in saved_keys:
                continue
            base = "{}  {}:{}".format(server.service_name, server.address[0], server.tcp_port)
            state = self._connection_state_label(endpoint_key)
            list_item = QListWidgetItem("{}  [{}]".format(base, state))
            list_item.setData(Qt.UserRole, server)
            list_item.setData(Qt.UserRole + 1, base)
            self.scanned_list.addItem(list_item)
            if previous_key is not None and endpoint_key == previous_key:
                select_row = visible_index
            visible_index += 1
        if self.scanned_list.count() and self._selection_source == "scanned":
            self.scanned_list.setCurrentRow(min(select_row, self.scanned_list.count() - 1))
        elif self.scanned_list.count() == 0 and self._selection_source == "scanned":
            self._selection_source = "saved"

    def scan_servers(self) -> None:
        if self.scan_worker is not None and self.scan_worker.isRunning():
            return
        self.scan_button.setEnabled(False)
        self.status_label.setText("正在扫描…")
        self.scan_worker = ScanWorker(self.destination.text())
        self.scan_worker.found.connect(self._servers_found)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.finished.connect(lambda: self.scan_button.setEnabled(True))
        self.scan_worker.start()

    def _servers_found(self, servers: list[DiscoveryResponse]) -> None:
        scanned_keys = {key for key in (self._endpoint_key(server) for server in servers) if key is not None}
        self._connection_attempts.intersection_update(scanned_keys | set(self._connected_endpoint_keys))
        # Refresh saved first so left-side filtering sees the latest right column.
        self._load_saved_server()
        self._load_scanned_servers(servers)
        known = self._saved_endpoint_keys()
        new_count = sum(1 for key in scanned_keys if key not in known)
        known_count = len(scanned_keys) - new_count
        self.status_label.setText(
            "扫描到 {} 个（新 {} / 已在右侧 {}）；右侧共 {} 个".format(
                len(scanned_keys), new_count, known_count, len(self.state_store.servers())
            )
        )
        if self.scanned_list.count():
            self._selection_source = "scanned"
            self.scanned_list.setCurrentRow(0)
            self._on_scanned_selection_changed()
        elif self.saved_list.count():
            self._selection_source = "saved"
            self._on_saved_selection_changed()

    def _scan_failed(self, message: str) -> None:
        self.status_label.setText("扫描失败：{}".format(message))

    def remove_selected_server(self) -> None:
        # Deletion only applies to the saved/connected column.
        item = self.saved_list.currentItem()
        if item is None:
            self.status_label.setText("请在右侧选择要删除的已保存服务端")
            return
        value = item.data(Qt.UserRole)
        endpoint_key = self._endpoint_key(value)
        if endpoint_key is None:
            self._update_remove_button()
            return
        worker = self.connection_workers.pop(endpoint_key, None)
        if worker is not None:
            worker.stop()
            self._stopping_workers.append(worker)
            QTimer.singleShot(0, lambda worker=worker: self._finish_worker_stop(worker))
        self._connection_attempts.discard(endpoint_key)
        self._connected_endpoint_keys.discard(endpoint_key)
        self._server_display_defaults.pop(endpoint_key, None)
        self._server_config_ids.pop(endpoint_key, None)
        self._server_status_text.pop(endpoint_key, None)
        self.state_store.remove_server(host=endpoint_key[0], tcp_port=endpoint_key[1])
        self._load_saved_server()
        self._load_scanned_servers()
        self.status_label.setText("已删除 {}:{}".format(endpoint_key[0], endpoint_key[1]))
        self._refresh_selection_details()
        self._update_remove_button()
        self._update_connect_button()

    def _finish_worker_stop(self, worker: ConnectionWorker) -> None:
        if worker.isRunning():
            QTimer.singleShot(50, lambda: self._finish_worker_stop(worker))
            return
        if worker in self._stopping_workers:
            self._stopping_workers.remove(worker)
        self._update_connect_button()

    def connect_manual_server(self) -> None:
        """Manual host/port Enter or 连接: save + connect + auto_connect."""
        host = self.manual_host.text().strip()
        if not host:
            return
        port = int(self.manual_port.value())
        existing = next(
            (
                item
                for item in self.state_store.servers()
                if str(item.get("host", "")).strip() == host and int(item.get("tcp_port", 0)) == port
            ),
            None,
        )
        fingerprint = b"\x00" * 32
        http_port = 39003
        if existing is not None:
            try:
                fingerprint = base64.b64decode(existing.get("fingerprint", ""), validate=True)
            except (ValueError, TypeError):
                fingerprint = b"\x00" * 32
            try:
                http_port = int(existing.get("http_port", 39003))
            except (TypeError, ValueError):
                http_port = 39003
        # Build a temporary selection path by stuffing values into connect flow.
        self._connect_endpoint(host, port, http_port, fingerprint)

    def _select_saved_endpoint(self, host: str, tcp_port: int) -> None:
        target = (str(host), int(tcp_port))
        for row in range(self.saved_list.count()):
            if self._endpoint_key(self.saved_list.item(row).data(Qt.UserRole)) == target:
                self._selection_source = "saved"
                self.saved_list.setCurrentRow(row)
                self._on_saved_selection_changed()
                break

    @staticmethod
    def _endpoint_key(value: object) -> Optional[tuple[str, int]]:
        if isinstance(value, DiscoveryResponse):
            return value.address[0], int(value.tcp_port)
        if isinstance(value, dict):
            try:
                return str(value["host"]), int(value["tcp_port"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _update_remove_button(self) -> None:
        item = self.saved_list.currentItem() if self._selection_source == "saved" else None
        if item is None:
            item = self.saved_list.currentItem()
        key = self._endpoint_key(item.data(Qt.UserRole)) if item is not None else None
        self.remove_server_button.setEnabled(item is not None and key is not None)

    def _refresh_list_item_state(self, item: QListWidgetItem) -> None:
        key = self._endpoint_key(item.data(Qt.UserRole))
        base = item.data(Qt.UserRole + 1) or item.text()
        if key is None:
            return
        state = self._connection_state_label(key)
        text = item.text()
        # Keep richer live status lines (e.g. reconnect countdown) until a base state applies.
        if text.startswith(str(base) + "  [") and state == "未连接" and "秒" in text:
            return
        item.setText("{}  [{}]".format(base, state))

    def _refresh_server_list_states(self) -> None:
        for row in range(self.scanned_list.count()):
            self._refresh_list_item_state(self.scanned_list.item(row))
        for row in range(self.saved_list.count()):
            self._refresh_list_item_state(self.saved_list.item(row))

    def _connection_status_changed(self, endpoint_key: object, status: str) -> None:
        if not isinstance(endpoint_key, tuple) or len(endpoint_key) != 2:
            self.status_label.setText(str(status))
            return
        key = (str(endpoint_key[0]), int(endpoint_key[1]))
        self._server_status_text[key] = str(status)
        for list_widget in (self.scanned_list, self.saved_list):
            for row in range(list_widget.count()):
                item = list_widget.item(row)
                if self._endpoint_key(item.data(Qt.UserRole)) == key:
                    base = item.data(Qt.UserRole + 1) or item.text()
                    item.setText("{}  [{}]".format(base, status))
        selected = self._selected_list_item()
        selected_key = self._endpoint_key(selected.data(Qt.UserRole)) if selected is not None else None
        if selected_key == key or selected_key is None:
            self.status_label.setText("{}:{} — {}".format(key[0], key[1], status))
        self._update_connect_button()

    def _connection_state_changed(self, endpoint_key: tuple[str, int], connected: bool) -> None:
        if connected:
            self._connected_endpoint_keys.add(endpoint_key)
            self._connection_attempts.add(endpoint_key)
            self._server_status_text[endpoint_key] = "已连接"
            # Ensure the connected server appears on the right and leaves the left.
            self._load_saved_server()
            self._load_scanned_servers()
        else:
            self._connected_endpoint_keys.discard(endpoint_key)
            if endpoint_key not in self._server_status_text:
                self._server_status_text[endpoint_key] = "未连接"
        self._refresh_server_list_states()
        self._refresh_selection_details()
        self._update_connect_button()

    def _update_connect_button(self) -> None:
        item = self._selected_list_item()
        selected_key = self._endpoint_key(item.data(Qt.UserRole)) if item is not None else None
        has_selection = selected_key is not None
        # Only "连接" and "删除". No save / reconnect labels.
        self.connect_button.setText("连接")
        self.connect_scanned_button.setText("连接")
        self.connect_button.setEnabled(has_selection)
        self.connect_scanned_button.setEnabled(self.scanned_list.currentItem() is not None)

    def _show_fingerprint_for_item(self, item: Optional[QListWidgetItem]) -> None:
        if item is None:
            self.fingerprint_label.setText(
                "左侧仅显示尚未保存的扫描结果；已保存的只在右侧。连接 = 保存 + 连接 + 自动连接。"
            )
            return
        server = item.data(Qt.UserRole)
        try:
            fingerprint = (
                server.fingerprint
                if isinstance(server, DiscoveryResponse)
                else base64.b64decode(server["fingerprint"])
            )
        except (TypeError, ValueError, KeyError):
            fingerprint = b""
        if not fingerprint or not any(fingerprint):
            self.fingerprint_label.setText("服务端公钥 SHA-256：\n尚未获取，连接时将先确认服务端证书。")
            return
        self.fingerprint_label.setText(
            "服务端公钥 SHA-256：\n{}".format(
                "SHA256:" + base64.b64encode(fingerprint).decode("ascii").rstrip("=")
            )
        )

    def connect_selected(self) -> None:
        item = self._selected_list_item()
        if item is None:
            QMessageBox.information(self, "选择服务端", "请先在左侧或右侧选择一个服务端。")
            return
        value = item.data(Qt.UserRole)
        if isinstance(value, DiscoveryResponse):
            host, tcp_port, http_port, fingerprint = (
                value.address[0],
                value.tcp_port,
                value.http_port,
                value.fingerprint,
            )
        else:
            host, tcp_port, http_port = value["host"], value["tcp_port"], value["http_port"]
            fingerprint = base64.b64decode(value["fingerprint"])
        self._connect_endpoint(str(host), int(tcp_port), int(http_port or 39003), bytes(fingerprint))

    def _connect_endpoint(self, host: str, tcp_port: int, http_port: int, fingerprint: bytes) -> None:
        """Connect = save + connect + enable auto_connect for this endpoint."""
        endpoint_key = (str(host), int(tcp_port))
        existing_worker = self.connection_workers.get(endpoint_key)
        if endpoint_key in self._connected_endpoint_keys and existing_worker is not None and existing_worker.isRunning():
            # Already online: keep other servers; just nudge this one.
            self.state_store.set_server(
                host=host,
                tcp_port=tcp_port,
                http_port=http_port,
                fingerprint=fingerprint,
                auto_connect=True,
            )
            existing_worker.request_manual_reconnect()
            self._server_status_text[endpoint_key] = "已请求刷新连接"
            self._select_saved_endpoint(host, tcp_port)
            self._refresh_selection_details()
            return
        if not fingerprint or not any(fingerprint):
            try:
                fingerprint = probe_server_fingerprint(str(host), int(tcp_port), timeout=3)
            except Exception as exc:
                self.status_label.setText("无法读取服务端证书：{}".format(exc))
                QMessageBox.warning(self, "读取服务端证书失败", "{}:{}\n{}".format(host, tcp_port, exc))
                return
        saved_for_ip = [
            candidate for candidate in self.state_store.servers() if candidate.get("host") == host
        ]
        already_trusted = any(
            any(base64.b64decode(candidate["fingerprint"]))
            and base64.b64decode(candidate["fingerprint"]) == fingerprint
            for candidate in saved_for_ip
        )
        if not already_trusted:
            displayed_fingerprint = "SHA256:" + base64.b64encode(fingerprint).decode(
                "ascii"
            ).rstrip("=")
            title = "确认信任服务端" if not saved_for_ip else "服务端公钥变化"
            explanation = (
                "本机尚未保存此 IP 的服务端公钥。是否继续连接并信任此公钥？"
                if not saved_for_ip
                else "此 IP 的服务端公钥与本机保存的公钥不同。是否继续连接并更新信任？"
            )
            answer = QMessageBox.warning(
                self,
                title,
                "{}\n{}".format(explanation, displayed_fingerprint),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.state_store.set_server(
            host=host,
            tcp_port=tcp_port,
            http_port=http_port,
            fingerprint=fingerprint,
            make_current=True,
            auto_connect=True,
        )
        self._start_connection(
            {
                "host": host,
                "tcp_port": tcp_port,
                "http_port": http_port,
                "fingerprint": fingerprint,
            }
        )
        self._load_saved_server()
        self._load_scanned_servers()
        self._select_saved_endpoint(host, tcp_port)
        self._server_status_text[endpoint_key] = "正在连接"
        self._refresh_selection_details()

    def _message_received(self, endpoint_key: object, message: IncomingMessage) -> None:
        server_id = b""
        if isinstance(endpoint_key, tuple) and len(endpoint_key) == 2:
            worker = self.connection_workers.get((str(endpoint_key[0]), int(endpoint_key[1])))
            if worker is not None and worker.server_fingerprint:
                server_id = bytes(worker.server_fingerprint)
        if not server_id and message.source_host:
            for saved in self.state_store.servers():
                if str(saved.get("host")) == message.source_host and int(saved.get("tcp_port", 0)) == int(message.source_tcp_port or 0):
                    try:
                        server_id = base64.b64decode(saved.get("fingerprint", ""), validate=True)
                    except (TypeError, ValueError):
                        server_id = b""
                    break
        if not self.dedup.observe(server_id, message.message_id):
            self._ack(message, "received")
            return
        self.pending_messages.put(message)
        if self.current_message is None:
            self._play_next()
        elif message.priority < self.current_message.priority:
            # Re-queue interrupted message for local replay; final ack is sent
            # only after that replay finishes (not at preemption time).
            interrupted = self.current_message
            self.pending_messages.put(interrupted)
            self._stop_current_speech()
            self._play_next()

    def _format_sent_time(self, milliseconds: Optional[int]) -> str:
        if milliseconds is None:
            return "发送时间未知"
        value = datetime.fromtimestamp(milliseconds / 1000, timezone(timedelta(hours=8)))
        return value.strftime("%Y-%m-%d %H:%M:%S+08:00")

    def _play_next(self) -> None:
        if self.withdrawal_notice_active:
            # Do not start the next business message while a withdrawal notice is on screen.
            return
        self.withdrawal_timer.stop()
        message = self.pending_messages.get()
        if message is None:
            self.current_message = None
            self.display.hide()
            return
        if message.message_id in self.withdrawn_ids:
            self._play_next()
            return
        self.current_message = message
        self.current_displayed = False
        self.current_speech_done = not message.tts_enabled
        self.current_speech_success = False
        self.current_error = ""
        # Resolve display defaults from the source server first (multi-server safe).
        source_key: Optional[tuple[str, int]] = None
        if message.source_host and message.source_tcp_port:
            source_key = (str(message.source_host), int(message.source_tcp_port))
        server_defaults = self._server_display_defaults.get(source_key) if source_key else None
        base_position = server_defaults[0] if server_defaults else self.display_position
        base_ratio = server_defaults[1] if server_defaults else self.display_duration_ratio
        # as_default carries deferred default config when "立即更新" is false:
        # apply it as this server's defaults before resolving this message.
        if message.as_default:
            if message.default_display_position:
                base_position = message.default_display_position
            if message.default_display_duration_ratio is not None:
                base_ratio = message.default_display_duration_ratio
            if source_key is not None:
                self._server_display_defaults[source_key] = (base_position, float(base_ratio))
        sent_time = self._format_sent_time(message.created_at_ms)
        self.display.setWindowTitle("{} | lovemilk class broadcaster".format(sent_time))
        self.display_content.setText(message.content)
        self.display_time.setText(sent_time)
        position = base_position if message.as_default else (message.display_position or base_position)
        _play_notification_sound()
        self._show_display(position)
        LOGGER.info(
            "message playback started message_id={} tts_requested={} speech_nodes={}",
            message.message_id,
            message.tts_enabled,
            len(message.speech),
        )
        try:
            parts = compile_speech(message.speech) if message.tts_enabled else []
        except (TypeError, ValueError) as exc:
            parts = []
            self.current_speech_done = True
            self.current_error = "invalid_speech: {}".format(exc)
            LOGGER.error(
                "TTS speech payload invalid message_id={} error={}", message.message_id, exc
            )
        minimum_duration_ms = max(1000, estimate_duration_ms(parts) // 4)
        display_ratio = base_ratio if message.as_default else (
            base_ratio if message.display_duration_ratio is None else message.display_duration_ratio
        )
        self.display_close_button.setVisible(display_ratio == 0)
        if display_ratio == 0:
            self.display_timer.stop()
            # Manual close: marquee uses comfortable default speed with end holds.
            self.display_content.set_display_duration_ms(0)
        else:
            configured_duration_ms = display_duration_ms(message.content, display_ratio)
            total_ms = min(120_000, max(minimum_duration_ms, configured_duration_ms))
            self.display_timer.start(total_ms)
            # Drive marquee so one full pass (+ ≥0.5s holds) fits this window.
            self.display_content.set_display_duration_ms(total_ms)
        if message.tts_enabled and parts:
            self.current_speech = SpeechWorker(message.message_id, parts, self.data_dir)
            self.current_speech.finished_playback.connect(self._speech_finished)
            self.current_speech.text_part.connect(self._speech_part_started)
            self.current_speech.start()
        elif not message.tts_enabled:
            # Displayed is the final receipt when speech is disabled.
            LOGGER.info("TTS skipped message_id={} reason=no_speech_payload", message.message_id)
        elif not parts:
            self.current_speech_done = True
            self.current_error = "empty_speech_payload"
            LOGGER.error(
                "TTS unavailable message_id={} error=empty_speech_payload", message.message_id
            )

    def _display_minimum_elapsed(self) -> None:
        self.current_displayed = True
        self._finish_current_if_ready()

    def _speech_part_started(self, message_id: str, text: str) -> None:
        if self.current_message is not None and self.current_message.message_id == message_id:
            self.display_content.setText(text)

    def _speech_finished(self, message_id: str, success: bool, error: str) -> None:
        if self.current_message is None or self.current_message.message_id != message_id:
            return
        if not success and error == "cancelled":
            return
        self.current_speech_done = True
        self.current_speech_success = success
        self.current_error = error
        self._finish_current_if_ready()

    def _finish_current_if_ready(self) -> None:
        message = self.current_message
        if message is None or not self.current_displayed or not self.current_speech_done:
            return
        if message.tts_enabled:
            self._ack(
                message,
                "spoken" if self.current_speech_success else "failed",
                None if self.current_speech_success else (self.current_error or "tts_unavailable"),
            )
        else:
            self._ack(message, "displayed")
        self.display.hide()
        self.display_close_button.hide()
        self.current_message = None
        self.current_speech = None
        self._play_next()

    def _dismiss_current(self) -> None:
        if self.withdrawal_notice_active:
            self._stop_withdrawal_speech()
            self._finish_withdrawal_notice()
            return
        message = self.current_message
        if message is None:
            return
        self.display_timer.stop()
        if self.current_speech is not None and self.current_speech.isRunning():
            self.current_speech.stop()
            self.current_speech.wait(1500)
            self.current_speech = None
            self.current_speech_done = True
            self.current_speech_success = False
            self.current_error = "manually_dismissed"
        self.current_displayed = True
        self._finish_current_if_ready()

    def _stop_current_speech(self) -> None:
        self.display_timer.stop()
        if self.current_speech is not None and self.current_speech.isRunning():
            self.current_speech.stop()
            self.current_speech.wait(1500)
        self.current_speech = None
        self.display.hide()
        self.current_message = None

    def _worker_for_message(self, message: object) -> Optional[ConnectionWorker]:
        host = getattr(message, "source_host", None)
        port = getattr(message, "source_tcp_port", None)
        if host and port:
            worker = self.connection_workers.get((str(host), int(port)))
            if worker is not None:
                return worker
        # Fallback: first online worker (single-server installs).
        for worker in self.connection_workers.values():
            if worker.isRunning():
                return worker
        return None

    def _ack(self, message: object, status: str, error: Optional[str] = None) -> None:
        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            if isinstance(message, str):
                message_id = message
            else:
                return
        worker = self._worker_for_message(message) if not isinstance(message, str) else None
        if worker is None and isinstance(message, str):
            # Legacy path: route to any live connection.
            for candidate in self.connection_workers.values():
                if candidate.isRunning():
                    worker = candidate
                    break
        if worker is not None:
            worker.acknowledge(message_id, status, error)

    @staticmethod
    def _format_withdrawal_text(withdrawal: IncomingWithdrawal) -> str:
        """Single-line notice so MarqueeLabel can auto-scroll long original content."""
        title = (withdrawal.message or "消息已撤回").strip() or "消息已撤回"
        content = (withdrawal.content or "").replace("\r", " ").replace("\n", " ").strip()
        if content:
            return "{}：{}".format(title, content)
        return title

    def _message_withdrawn(self, endpoint_key: object, withdrawal: IncomingWithdrawal) -> None:
        self.withdrawn_ids.add(withdrawal.message_id)
        self.pending_messages.withdraw(withdrawal.message_id)
        # Resolve display ratio from the withdrawn message when it is on screen;
        # otherwise use the withdrawal payload / global default.
        ratio = self.display_duration_ratio
        if self.current_message is not None and self.current_message.message_id == withdrawal.message_id:
            message = self.current_message
            if message.as_default or message.display_duration_ratio is None:
                source_key = None
                if message.source_host and message.source_tcp_port:
                    source_key = (str(message.source_host), int(message.source_tcp_port))
                server_defaults = self._server_display_defaults.get(source_key) if source_key else None
                ratio = server_defaults[1] if server_defaults else self.display_duration_ratio
            else:
                ratio = message.display_duration_ratio
            self._stop_current_speech()
        else:
            if withdrawal.display_duration_ratio is not None:
                ratio = withdrawal.display_duration_ratio
            # Another message may be playing: pause and requeue so the notice can take the window.
            if self.current_message is not None:
                interrupted = self.current_message
                if interrupted.message_id not in self.withdrawn_ids:
                    self.pending_messages.put(interrupted)
                self._stop_current_speech()
            sent_time = self._format_sent_time(int(time.time() * 1000))
            self.display.setWindowTitle("{} | lovemilk class broadcaster".format(sent_time))
            self.display_time.setText(sent_time)
        withdrawn_text = self._format_withdrawal_text(withdrawal)
        self._show_withdrawal_notice(withdrawn_text, float(ratio) if ratio is not None else self.display_duration_ratio)
        self._ack(withdrawal, "withdrawn")

    def _show_withdrawal_notice(self, text: str, ratio: float) -> None:
        """Show withdrawal notice with marquee + TTS; ratio 0 keeps a manual close button."""
        self._stop_withdrawal_speech()
        self.withdrawal_notice_active = True
        self.withdrawal_displayed = False
        self.withdrawal_speech_done = False
        display_text = str(text or "消息已撤回").replace("\r", " ").replace("\n", " ").strip() or "消息已撤回"
        self.display_content.setAlignment(Qt.AlignCenter)
        self.display_content.setText(display_text)
        self.display.adjustSize()
        self.display_close_button.setVisible(ratio <= 0)
        _play_notification_sound()
        self._show_display("center")

        parts = [SpeechPart("text", text=display_text)]
        minimum_duration_ms = max(1000, estimate_duration_ms(parts) // 4)
        if ratio <= 0:
            self.withdrawal_timer.stop()
            self.display_content.set_display_duration_ms(0)
            # Manual close: display is "done" for finish gating; speech may still run.
            self.withdrawal_displayed = True
        else:
            duration = min(120_000, max(minimum_duration_ms, display_duration_ms(display_text, ratio)))
            self.withdrawal_timer.start(duration)
            self.display_content.set_display_duration_ms(duration)

        LOGGER.info(
            "withdrawal notice shown tts=True chars={} ratio={} manual_close={}",
            len(display_text),
            ratio,
            ratio <= 0,
        )
        self.withdrawal_speech = SpeechWorker(self._withdrawal_speech_id, parts, self.data_dir)
        self.withdrawal_speech.finished_playback.connect(self._withdrawal_speech_finished)
        # Do not connect text_part: keep the full notice visible while speaking.
        self.withdrawal_speech.start()

    def _withdrawal_display_elapsed(self) -> None:
        self.withdrawal_displayed = True
        self._finish_withdrawal_if_ready()

    def _withdrawal_speech_finished(self, message_id: str, success: bool, error: str) -> None:
        if message_id != self._withdrawal_speech_id or not self.withdrawal_notice_active:
            return
        if not success and error == "cancelled":
            return
        if not success and error:
            LOGGER.warning("withdrawal TTS finished with error error={}", error)
        self.withdrawal_speech_done = True
        self.withdrawal_speech = None
        self._finish_withdrawal_if_ready()

    def _finish_withdrawal_if_ready(self) -> None:
        if not self.withdrawal_notice_active:
            return
        # Manual-close notices stay until the user dismisses (speech may finish first).
        if self.display_close_button.isVisible():
            return
        if not self.withdrawal_displayed or not self.withdrawal_speech_done:
            return
        self._finish_withdrawal_notice()

    def _stop_withdrawal_speech(self) -> None:
        self.withdrawal_timer.stop()
        if self.withdrawal_speech is not None and self.withdrawal_speech.isRunning():
            self.withdrawal_speech.stop()
            self.withdrawal_speech.wait(1500)
        self.withdrawal_speech = None
        self.withdrawal_speech_done = True

    def _finish_withdrawal_notice(self) -> None:
        self._stop_withdrawal_speech()
        self.withdrawal_notice_active = False
        self.withdrawal_displayed = False
        self.display.hide()
        self.display_close_button.hide()
        self._play_next()

    def _refresh_autostart_ui(self) -> None:
        win = sys.platform == "win32"
        for widget in (self.autostart, self.autostart_machine, self.autostart_label):
            widget.setEnabled(win)
        if not win:
            return
        self.autostart.blockSignals(True)
        self.autostart_machine.blockSignals(True)
        enabled = _autostart_enabled()
        self.autostart.setChecked(enabled)
        self.autostart_machine.setChecked(_autostart_uses_machine())
        self.autostart_machine.setEnabled(enabled)
        self.autostart.blockSignals(False)
        self.autostart_machine.blockSignals(False)

    def _ensure_default_autostart(self) -> None:
        """First Windows install: enable current-user autostart by default."""
        if sys.platform != "win32":
            return
        try:
            if self.state_store.autostart_default_applied():
                self._refresh_autostart_ui()
                return
            if not _autostart_enabled():
                _set_autostart(True, machine=False)
                LOGGER.info("enabled default Windows autostart (HKCU)")
            self.state_store.set_autostart_default_applied(True)
        except OSError as exc:
            LOGGER.warning("default autostart could not be enabled error={}", exc)
        self._refresh_autostart_ui()

    def _toggle_autostart(self, enabled: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            machine = bool(self.autostart_machine.isChecked()) if enabled else False
            _set_autostart(enabled, machine=machine)
        except OSError as exc:
            QMessageBox.warning(self, "启动项设置失败", str(exc))
        self._refresh_autostart_ui()

    def _toggle_autostart_machine(self, machine: bool) -> None:
        if sys.platform != "win32":
            return
        if not self.autostart.isChecked():
            self._refresh_autostart_ui()
            return
        try:
            _set_autostart(True, machine=bool(machine))
        except OSError as exc:
            QMessageBox.warning(self, "启动项设置失败", str(exc))
        self._refresh_autostart_ui()

    def show_window(self) -> None:
        """Force the settings window onto a visible desktop (Windows-safe)."""
        try:
            self._refresh_autostart_ui()
        except Exception:
            pass
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.raise_()
        self.activateWindow()
        # Windows sometimes needs a brief topmost pulse to bring a hidden window forward.
        if sys.platform == "win32":
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            self.show()
            self.raise_()
            self.activateWindow()
        LOGGER.info("settings window shown")

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()

    def quit_application(self) -> None:
        """Developer-panel Exit: report user_exit then stop (人为终止 on server)."""
        LOGGER.info("controlled shutdown requested reason=user_exit")
        if getattr(self, "tray", None) is not None:
            self.tray.hide()
        self._report_session_end_all("user_exit", "developer_exit")
        self.shutdown()
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def _report_session_end_all(self, reason: str, detail: str = "") -> None:
        """Queue ClientSessionEnd on every live worker and wait briefly for the flush."""
        workers = [w for w in self.connection_workers.values() if w.isRunning()]
        if not workers:
            return
        for worker in workers:
            try:
                worker._session_end_sent.clear()
                worker.report_session_end(reason, detail)
            except Exception:
                pass
        # Connection loop polls every ~0.25s; wait up to ~2s for goodbye to leave the socket.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if all(w._session_end_sent.is_set() or not w.isRunning() for w in workers):
                break
            time.sleep(0.05)

    def shutdown(self) -> None:
        self.display_timer.stop()
        self._stop_current_speech()
        try:
            self._stop_withdrawal_speech()
        except Exception:
            pass
        self._stop_listener()
        for key, worker in list(self.connection_workers.items()):
            if worker.isRunning():
                worker.stop()
                worker.wait(5000)
            self.connection_workers.pop(key, None)
        for worker in list(self._stopping_workers):
            if worker.isRunning():
                worker.stop()
                worker.wait(5000)
        self._stopping_workers.clear()
        if self.instance_lock is not None:
            self.instance_lock.release()
            self.instance_lock = None

    def needs_setup_ui(self) -> bool:
        """True when there is no usable saved server configuration yet."""
        servers = self.state_store.servers()
        if not servers:
            return True
        for saved in servers:
            try:
                fingerprint = base64.b64decode(saved.get("fingerprint", ""), validate=True)
            except (TypeError, ValueError):
                fingerprint = b""
            if fingerprint and any(fingerprint):
                return False
        return True


def _configure_qt_plugin_path() -> None:
    """Point Qt at bundled PySide6 plugins so Windows can create a GUI.

    Packaged builds keep plugins under Lib/site-packages/PySide6/plugins. Without
    QT_QPA_PLATFORM_PLUGIN_PATH, QApplication may start headless or fail silently
    under pythonw.exe and the settings window never appears.
    """
    if os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH"):
        return
    candidates: list[Path] = []
    install_dir = os.environ.get("MKCB_INSTALL_DIR")
    if install_dir:
        candidates.append(Path(install_dir) / "Lib" / "site-packages" / "PySide6" / "plugins")
        candidates.append(Path(install_dir) / "Lib" / "site-packages" / "PySide6" / "plugins" / "platforms")
    # Source / editable installs.
    try:
        import PySide6

        package_dir = Path(PySide6.__file__).resolve().parent
        candidates.append(package_dir / "plugins")
        candidates.append(package_dir / "plugins" / "platforms")
    except Exception:
        pass
    for path in candidates:
        if path.is_dir() and (path / "platforms").is_dir() or path.name == "platforms" and path.is_dir():
            plugin_root = path if path.name == "plugins" else path.parent
            platforms = plugin_root / "platforms"
            if not platforms.is_dir():
                continue
            os.environ["QT_PLUGIN_PATH"] = str(plugin_root)
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(platforms)
            # Help Windows locate Qt/PySide DLLs next to the plugins package.
            if sys.platform == "win32":
                bin_dir = plugin_root.parent
                path_env = os.environ.get("PATH", "")
                prefix = str(bin_dir)
                if prefix and prefix.lower() not in path_env.lower():
                    os.environ["PATH"] = prefix + os.pathsep + path_env
                try:
                    os.add_dll_directory(str(bin_dir))
                except (AttributeError, FileNotFoundError, OSError):
                    pass
            return


def main() -> None:
    # 在创建主窗口前取得安装目录锁，避免两个进程同时消费同一状态文件。
    _configure_qt_plugin_path()
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    had_existing_state = (data_dir / "state.json").is_file()
    instance_lock = SingleInstanceLock(data_dir / "client.lock")
    if not instance_lock.acquire():
        # Another instance already holds the lock; try to surface a visible hint.
        app = QApplication(sys.argv)
        QMessageBox.information(
            None,
            "lovemilk class broadcaster",
            "客户端已在运行。请从系统托盘打开设置，或先退出已有进程。",
        )
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("lovemilk class broadcaster")
    app.setWindowIcon(_application_icon())
    try:
        window = ClientWindow()
    except Exception as exc:
        instance_lock.release()
        LOGGER.exception("client window failed to start")
        QMessageBox.critical(None, "客户端启动失败", str(exc))
        return
    window.instance_lock = instance_lock
    # Open settings when there is no data/state or no usable saved server.
    # Closing the window only hides it to the tray; it does not exit.
    if (not had_existing_state) or window.needs_setup_ui() or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show_window()
        LOGGER.info(
            "opened settings on launch reason={}",
            "no_state"
            if not had_existing_state
            else ("no_server" if window.needs_setup_ui() else "no_tray"),
        )
    else:
        LOGGER.info("running in tray; open settings from the tray icon")
    app.aboutToQuit.connect(window.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
