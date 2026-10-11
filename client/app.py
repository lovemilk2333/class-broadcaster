"""Tray application for configuring and connecting the class-broadcaster client.

Qt binding: PySide2 on Debian 11 / Linux (Python ≥3.10), PySide6 on Windows packages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import queue
import platform
import signal
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

from .qtcompat import (  # noqa: E402
    AlignCenter,
    AlignLeft,
    AlignVCenter,
    FramelessWindowHint,
    Key_Escape,
    PlainText,
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QCloseEvent,
    QColor,
    QComboBox,
    QDesktopServices,
    QDialog,
    QFont,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QIcon,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPainter,
    QPixmap,
    QPlainTextEdit,
    QPoint,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    Qt,
    QThread,
    QTimer,
    QUrl,
    QueuedConnection,
    QVBoxLayout,
    QWidget,
    RichText,
    ScrollBarAlwaysOff,
    Signal,
    TextSelectableByMouse,
    Tool,
    UserRole,
    Window,
    WindowActive,
    WindowMinimized,
    WindowStaysOnTopHint,
    configure_platform_for_popup,
    default_message_font_family,
    list_system_font_families,
    qt_binding,
    resolve_message_font_family,
    text_width,
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
from .core.delivery import (
    ConfigSnapshot,
    MessageDeduplicator,
    PriorityQueue,
    ReconnectBackoff,
    display_duration_ms,
)
from .core.fontsize import DEFAULT_EXPR as FONT_DEFAULT_EXPR
from .core.fontsize import DEFAULT_FALLBACK_PX as FONT_DEFAULT_PX
from .core.fontsize import resolve_px as resolve_font_size_px
from .core.richtext import (
    html_to_plain_text,
    looks_like_html,
    needs_static_rich_display,
    rewrite_data_images_for_qt,
    sanitize_html,
)
from .core.discovery import DiscoveryResponse, discover, parse_scan_destinations
from .core.identity import (
    decrypt_private_key,
    encrypt_private_key,
    generate_identity,
    public_key_fingerprint,
)
from .core.protocol.bson import decode, encode
from .core.protocol.packet import Packet, read_packet
from .core.store import StateStore
from .core.tts import (
    DEFAULT_SPEECH_RATE,
    EnginePreference,
    SAPIEngine,
    SpeechPart,
    VC_REDIST_X64_URL,
    compile_speech,
    diagnose_windows_tts_runtime,
    estimate_duration_ms,
    is_vc_redist_prompt_dismissed,
    mark_vc_redist_prompt_dismissed,
    normalize_speech_rate,
    play_cached_speech_plan,
    speak_with_fallback,
    synthesize_parts_to_plan,
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
from .core.applied_updates import (
    is_update_applied,
    load_applied_digests,
    mark_update_applied,
    mark_update_reported,
    pending_update_reports,
)


LOGGER = logger.bind(name="mkcb.client")
CLIENT_LOG_UPLOAD = 0x0206
_CLIENT_LOG_QUEUE: queue.Queue = queue.Queue(maxsize=1000)


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
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#0f172a"))
    painter.setBrush(QColor("#38bdf8"))
    painter.drawRoundedRect(2, 2, 28, 28, 7, 7)
    painter.setPen(QColor("#082f49"))
    painter.drawText(pixmap.rect(), AlignCenter, "M")
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
            detail = (
                completed.stderr or completed.stdout or ""
            ).strip() or f"exit {completed.returncode}"
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
            [
                "add",
                _AUTOSTART_REG_HKLM,
                "/v",
                _AUTOSTART_VALUE,
                "/t",
                "REG_SZ",
                "/d",
                command,
                "/f",
            ],
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
        self.idle_timeout_seconds = max(
            1, min(3600, int(idle_timeout_seconds or self.DEFAULT_IDLE_TIMEOUT_SECONDS))
        )
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
                key_path.write_bytes(
                    self.private_key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
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
                            server_fingerprint = (
                                public_key_fingerprint(server_public_key)
                                if len(server_public_key) == 32
                                else b""
                            )
                            signed_challenge = (
                                b"MKCB-listener-probe-v1\x00" + nonce + server_public_key
                            )
                            server_signature_valid = False
                            if len(server_public_key) == 32 and len(challenge) == 128:
                                try:
                                    Ed25519PublicKey.from_public_bytes(server_public_key).verify(
                                        challenge[64:], signed_challenge
                                    )
                                    server_signature_valid = True
                                except InvalidSignature:
                                    pass
                            if (
                                packet.packet_type != PING
                                or len(challenge) != 128
                                or not self.state_store.trusts_server_fingerprint(
                                    server_fingerprint
                                )
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
                            connection.sendall(
                                Packet(1, 0, PONG, packet.sequence, 0, response).encode()
                            )
                            self.sent += 1
                            self.received += 1
                            self.latencies.append((time.monotonic() - started) * 1000)
                            self._log.info(
                                "listener probe answered count={} latency_ms={:.1f}",
                                self.received,
                                self.latencies[-1],
                            )
                            self.status.emit(
                                "listener mode answered packets={}".format(self.received)
                            )
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
            "update apply worker start components={} primary={} version={} seq={} digest={} force={} has_package={} has_token={}",
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
                raise ValueError(f"invalid digest={self.update.get('sha256')!r}")
            update_dir = self.data_dir / "updates"
            update_dir.mkdir(parents=True, exist_ok=True)
            package_path = ClientWindow._update_package_path(update_dir, digest)
            metadata_path = update_dir / f"{digest}.metadata.json"
            skip_keys = {"package", "download_token", "source_fingerprint"}
            metadata = {key: value for key, value in self.update.items() if key not in skip_keys}
            metadata["components"] = components
            # Wire UPDATE_AVAILABLE still names the 64-hex identity ``sha256``
            # (value is BLAKE3 for new packages). Mirror into blake3 so verify
            # prefers the BLAKE3 algorithm against the package's files-v1.
            metadata["sha256"] = digest
            metadata["blake3"] = digest
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
                "update apply worker failed digest={} version={} seq={} error={}",
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
            "update TLS download starting host={} port={} digest={} dest={}",
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
                "update TLS download finished host={} port={} digest={} dest={}",
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
        request = urllib.request.Request(
            url, method="GET", headers={"User-Agent": f"mkcb-client/{VERSION_STR}"}
        )
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
                raise RuntimeError(
                    "downloaded update package is not zstd-compressed (.tar.zst expected)"
                )
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
            "update verify start path={} components={} version={} digest={}",
            package_path,
            components,
            version,
            digest,
        )
        verify_metadata(metadata, b"")
        verify_package_files(package_path, metadata)
        LOGGER.info("update package verified path={} digest={}", package_path, digest)
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
    server_font_updated = Signal(object, str, str)  # endpoint_key, family, size_expr

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
        # (digest, version, seq) UpdateApplied reports; flushed after reconnect.
        self.update_applied_queue: queue.Queue = queue.Queue()
        # (state, message_id, detail, elapsed_ms) ClientActivity reports.
        self.activity_queue: queue.Queue = queue.Queue()
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

    def report_activity(
        self,
        state: str,
        *,
        message_id: str = "",
        detail: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        """Queue ClientActivity for the connection thread (processing / idle)."""
        try:
            self.activity_queue.put_nowait(
                (
                    str(state or "idle"),
                    str(message_id or ""),
                    str(detail or ""),
                    int(elapsed_ms or 0),
                )
            )
        except queue.Full:
            pass

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

    def queue_update_applied_report(
        self, digest: str, version: str = "", seq: int = 0
    ) -> None:
        """Queue an UpdateApplied report for the connection thread to flush."""
        digest = str(digest or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            return
        try:
            seq_value = int(seq or 0)
        except (TypeError, ValueError):
            seq_value = 0
        try:
            self.update_applied_queue.put_nowait(
                (digest, str(version or ""), max(0, seq_value))
            )
        except queue.Full:
            pass

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
                        if str(saved.get("host", "")).strip() == str(self.endpoint["host"]) and int(
                            saved.get("tcp_port", 0) or 0
                        ) == int(self.endpoint["tcp_port"]):
                            candidate = saved.get("config_id")
                            if isinstance(candidate, str) and candidate:
                                endpoint_config_id = candidate
                            break
                    fonts: list[str] = []
                    platform_font = ""
                    try:
                        fonts = list_system_font_families(limit=200)
                    except Exception:
                        fonts = []
                    try:
                        platform_font = str(default_message_font_family() or "").strip()
                    except Exception:
                        platform_font = ""
                    connection, response = connect(
                        self.endpoint["host"],
                        self.endpoint["tcp_port"],
                        str(cert_path),
                        str(key_path),
                        self.endpoint["fingerprint"],
                        config_id=endpoint_config_id,
                        timeout=3,
                        listener_mode=self.listener_mode,
                        listener_port=self.listener_port
                        if self.listener_mode in ("auto", "listen")
                        else 0,
                        reconnect_probe=reconnect_probe,
                        manual_connect=manual_attempt,
                        available_fonts=fonts,
                        platform_default_font=platform_font,
                    )
                # A successful business handshake means authorization has
                # completed. Any later disconnect must use ordinary backoff,
                # never the 15-second approval retry window.
                approval_deadline = None
                mode = response.get("connection_mode") if isinstance(response, dict) else None
                if mode in ("pull", "listen"):
                    self.server_connection_mode = mode
                if isinstance(response, dict):
                    raw_size = response.get("default_font_size")
                    if isinstance(raw_size, (int, float)) and not isinstance(raw_size, bool):
                        size_expr = f"{int(raw_size)}px" if int(raw_size) > 0 else ""
                    else:
                        size_expr = str(raw_size or "").strip()
                    self.server_font_updated.emit(
                        self.endpoint_key(),
                        str(response.get("default_font_family") or "").strip(),
                        size_expr,
                    )
                if isinstance(response, dict) and response.get("connect_allowed") is False:
                    LOGGER.info(
                        "server denied reconnect probe mode={}", self.server_connection_mode
                    )
                    self.status.emit(self.endpoint_key(), "服务端当前不允许连接；5 分钟后再次探测")
                    if connection is not None:
                        connection.close()
                        connection = None
                    raise ConnectionError("server_denied_reconnect_probe")
                actual_fingerprint = (
                    response.get("_server_fingerprint") if isinstance(response, dict) else None
                )
                server_public_key = (
                    response.get("_server_public_key") if isinstance(response, dict) else None
                )
                if (
                    isinstance(server_public_key, (bytes, bytearray))
                    and len(server_public_key) == 32
                ):
                    self.endpoint["server_public_key"] = bytes(server_public_key)
                if isinstance(actual_fingerprint, bytes) and len(actual_fingerprint) == 32:
                    if (
                        not any(self.server_fingerprint)
                        or self.server_fingerprint != actual_fingerprint
                    ):
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
                        speech_rate=0.75,
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
                # After restart, report locally-applied digests the server has not acked yet.
                try:
                    for digest in pending_update_reports(self.data_dir):
                        self.queue_update_applied_report(digest)
                    if not self.update_applied_queue.empty():
                        LOGGER.info(
                            "queued pending UpdateApplied report(s) for server host={}:{}",
                            self.endpoint.get("host"),
                            self.endpoint.get("tcp_port"),
                        )
                except Exception as exc:
                    LOGGER.debug("update applied report queue skipped error={}", exc)
                last_ping = time.monotonic()
                last_pong = last_ping
                while self.keep_running:
                    self._flush_acks(connection)
                    self._flush_client_logs(connection)
                    self._flush_update_applied(connection)
                    self._flush_activity(connection)
                    if self._flush_session_end(connection):
                        # Intentional goodbye sent. Stop the outer reconnect loop too —
                        # otherwise we briefly re-handshake (ClearSessionEnd) and the
                        # subsequent quit looks like 意外终止 instead of 更新重启中.
                        self.keep_running = False
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
                        self.status.emit(
                            self.endpoint_key(), "配置已更新：{}".format(snapshot.config_id)
                        )
                    elif packet.packet_type == UPDATE_AVAILABLE:
                        update = decode_update_available(packet)
                        # Tag the peer that pushed the update so TLS download uses the same host:tcp_port.
                        update["source_host"] = str(self.endpoint.get("host") or "")
                        update["source_tcp_port"] = int(self.endpoint.get("tcp_port") or 0)
                        fingerprint = (
                            self.server_fingerprint or self.endpoint.get("fingerprint") or b""
                        )
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
                            "update available: {} {}".format(
                                update["component"], update["version"]
                            ),
                        )
                    elif packet.packet_type == PONG:
                        last_pong = time.monotonic()
                    elif packet.packet_type == CLIENT_DISCONNECT:
                        LOGGER.info("connection closed by server administrator")
                        approval_deadline = None
                        self.admin_disconnected = True
                        self.status.emit(
                            self.endpoint_key(), "已由管理员断开；正在按服务端连接模式等待"
                        )
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
                    self.certificate_mismatch.emit(
                        {
                            "host": str(self.endpoint["host"]),
                            "tcp_port": int(self.endpoint["tcp_port"]),
                            "fingerprint": exc.actual_fingerprint,
                        }
                    )
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
                    self.status.emit(
                        self.endpoint_key(), "等待服务端批准；每 15 秒重试，最多 3 分钟"
                    )
                    self._wait(min(15, remaining))
                    continue
                # All non-authorization failures, including a connection
                # closed after approval, belong to the normal retry policy.
                approval_deadline = None
                if self.keep_running and self.admin_disconnected:
                    LOGGER.warning(
                        "reconnect attempt failed while administrator-disconnected: {}", exc
                    )
                    self.status.emit(
                        self.endpoint_key(), "重连未成功；保留原探测计划：{}".format(exc)
                    )
                elif self.keep_running:
                    LOGGER.exception(
                        "connection failed {}:{}",
                        self.endpoint.get("host"),
                        self.endpoint.get("tcp_port"),
                    )
                    delay = 300 if normal_shutdown else backoff.next_delay()
                    self.status.emit(
                        self.endpoint_key(), "连接中断：{}；{} 秒后重试".format(exc, delay)
                    )
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
                    LOGGER.info(
                        "admin disconnected; listen mode will not auto-reconnect (manual connect allowed)"
                    )
                    self.status.emit(
                        self.endpoint_key(),
                        "admin disconnected; listen mode idle until manual connect",
                    )
                    self._manual_reconnect.wait()
                    self._manual_reconnect.clear()
                else:
                    LOGGER.info("admin disconnected; pull mode will probe handshake in 5 minutes")
                    self.status.emit(
                        self.endpoint_key(), "admin disconnected; pull mode probe in 5 minutes"
                    )
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

    def _flush_update_applied(self, connection: Connection) -> None:
        """Send queued UpdateApplied packets; mark digests reported on success."""
        while True:
            try:
                digest, version, seq = self.update_applied_queue.get_nowait()
            except queue.Empty:
                return
            try:
                connection.report_update_applied(str(digest), str(version or ""), int(seq or 0))
                mark_update_reported(self.data_dir, str(digest))
                LOGGER.info(
                    "update applied reported host={}:{} digest={} version={} seq={}",
                    self.endpoint.get("host"),
                    self.endpoint.get("tcp_port"),
                    digest,
                    version or "",
                    seq,
                )
            except Exception as exc:
                LOGGER.warning(
                    "update applied report failed host={}:{} digest={} error={}",
                    self.endpoint.get("host"),
                    self.endpoint.get("tcp_port"),
                    digest,
                    exc,
                )
                try:
                    self.update_applied_queue.put_nowait((digest, version, seq))
                except queue.Full:
                    pass
                return

    def _flush_activity(self, connection: Connection) -> None:
        """Send queued ClientActivity packets (processing / idle)."""
        while True:
            try:
                state, message_id, detail, elapsed_ms = self.activity_queue.get_nowait()
            except queue.Empty:
                return
            try:
                connection.report_activity(
                    str(state),
                    message_id=str(message_id or ""),
                    detail=str(detail or ""),
                    elapsed_ms=int(elapsed_ms or 0),
                )
            except Exception as exc:
                LOGGER.debug(
                    "client activity report failed host={}:{} state={} error={}",
                    self.endpoint.get("host"),
                    self.endpoint.get("tcp_port"),
                    state,
                    exc,
                )
                try:
                    self.activity_queue.put_nowait((state, message_id, detail, elapsed_ms))
                except queue.Full:
                    pass
                return

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


class TtsCacheEntry:
    """In-memory prefetch state for one message (WAV plan, not persisted)."""

    __slots__ = (
        "message_id",
        "parts",
        "speech_rate",
        "plan",
        "error",
        "ready",
        "cancel",
        "bytes_total",
    )

    def __init__(
        self,
        message_id: str,
        parts: list[Any],
        speech_rate: float,
    ) -> None:
        self.message_id = message_id
        self.parts = list(parts)
        self.speech_rate = float(speech_rate)
        self.plan: Optional[list[tuple[str, Any]]] = None
        self.error: Optional[str] = None
        self.ready = threading.Event()
        self.cancel = threading.Event()
        self.bytes_total = 0


class TtsPrefetchWorker(QThread):
    """Background Piper synthesize-only worker; fills a TtsCacheEntry."""

    finished_prefetch = Signal(str, bool, str)  # message_id, ok, error

    def __init__(self, entry: TtsCacheEntry, data_dir: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.data_dir = Path(data_dir)

    def stop(self) -> None:
        self.entry.cancel.set()

    def run(self) -> None:
        try:
            plan = synthesize_parts_to_plan(
                self.entry.parts,
                data_dir=self.data_dir,
                speech_rate=self.entry.speech_rate,
                stop=self.entry.cancel,
            )
            if self.entry.cancel.is_set():
                self.entry.error = "cancelled"
                self.entry.ready.set()
                self.finished_prefetch.emit(self.entry.message_id, False, "cancelled")
                return
            self.entry.plan = plan
            self.entry.bytes_total = sum(
                len(payload) for kind, payload in plan if kind == "wav" and isinstance(payload, (bytes, bytearray))
            )
            self.entry.ready.set()
            LOGGER.info(
                "TTS prefetch ready message_id={} segments={} bytes={}",
                self.entry.message_id,
                sum(1 for kind, _ in plan if kind == "wav"),
                self.entry.bytes_total,
            )
            self.finished_prefetch.emit(self.entry.message_id, True, "")
        except Exception as exc:
            if self.entry.cancel.is_set():
                self.entry.error = "cancelled"
            else:
                self.entry.error = str(exc) or exc.__class__.__name__
                LOGGER.warning(
                    "TTS prefetch failed message_id={} error={}",
                    self.entry.message_id,
                    self.entry.error,
                )
            self.entry.ready.set()
            self.finished_prefetch.emit(
                self.entry.message_id, False, self.entry.error or "prefetch_failed"
            )


class SpeechWorker(QThread):
    finished_playback = Signal(str, bool, str)
    text_part = Signal(str, str)

    def __init__(
        self,
        message_id: str,
        parts: list[Any],
        data_dir: Path,
        speech_rate: float = 0.75,
        *,
        cache_entry: Optional[TtsCacheEntry] = None,
    ) -> None:
        super().__init__()
        self.message_id = message_id
        self.parts = parts
        self.data_dir = data_dir
        self.speech_rate = float(speech_rate) if speech_rate else 0.75
        self.cache_entry = cache_entry
        self.stop_event = threading.Event()
        self.cancelled = False

    def stop(self) -> None:
        self.cancelled = True
        self.stop_event.set()
        entry = self.cache_entry
        if entry is not None:
            entry.cancel.set()

    def run(self) -> None:
        from .core.tts import (
            PiperLibraryEngine,
            ensure_windows_native_dll_dirs,
            resolve_tts_model_dirs,
        )

        # Register onnxruntime DLL dirs before any piper import (avoids 0xC0000005).
        ensure_windows_native_dll_dirs(include_qt=False)
        try:
            if self._play_from_cache_if_ready():
                return
            model_dirs = resolve_tts_model_dirs(self.data_dir)
            # Prefer classic piper-tts (zh_CN-huayan + en_US-lessac) on both platforms.
            # No CLI fallback — SAPI remains the only Windows backup when models/lib fail.
            engines = [
                PiperLibraryEngine(model_dirs),
                SAPIEngine(),
            ]
            engine_name = speak_with_fallback(
                self.parts,
                engines,
                self.stop_event,
                EnginePreference(self.data_dir / "tts-engine.json"),
                lambda text: self.text_part.emit(self.message_id, text),
                speech_rate=self.speech_rate,
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

    def _play_from_cache_if_ready(self) -> bool:
        """Wait for prefetch (if any) and play cached WAVs. False → live synth fallback."""
        entry = self.cache_entry
        if entry is None:
            return False
        # Wait until prefetch finishes (or we are cancelled). Popup is deferred
        # until ready when possible; this wait covers late/live-fallback paths.
        while not entry.ready.is_set():
            if self.stop_event.is_set() or entry.cancel.is_set():
                self.finished_playback.emit(self.message_id, False, "cancelled")
                return True
            entry.ready.wait(0.05)
        if self.stop_event.is_set() or entry.cancel.is_set():
            self.finished_playback.emit(self.message_id, False, "cancelled")
            return True
        if entry.error or not entry.plan:
            LOGGER.info(
                "TTS cache miss message_id={} error={}; falling back to live synth",
                self.message_id,
                entry.error or "empty_plan",
            )
            return False
        try:
            play_cached_speech_plan(
                entry.plan,
                self.stop_event,
                on_text=lambda text: self.text_part.emit(self.message_id, text) if text else None,
            )
            LOGGER.info(
                "TTS playback completed message_id={} engine=piper-cache segments={}",
                self.message_id,
                sum(1 for kind, _ in entry.plan if kind == "wav"),
            )
            self.finished_playback.emit(
                self.message_id, not self.cancelled, "cancelled" if self.cancelled else ""
            )
            return True
        except Exception as exc:
            if self.cancelled or self.stop_event.is_set():
                self.finished_playback.emit(self.message_id, False, "cancelled")
                return True
            LOGGER.warning(
                "TTS cache play failed message_id={} error={}; falling back",
                self.message_id,
                exc,
            )
            return False


class RuntimeProbeWorker(QThread):
    """Background Windows TTS/runtime probe (piper-tts + CRT diagnostics)."""

    finished_probe = Signal(object)  # dict from diagnose_windows_tts_runtime

    def __init__(self, data_dir: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)

    def run(self) -> None:
        try:
            report = diagnose_windows_tts_runtime(self.data_dir)
        except Exception as exc:
            LOGGER.warning("runtime probe failed error={}", exc)
            report = {
                "applicable": platform.system() == "Windows",
                "piper_ok": False,
                "needs_vc_redist": False,
                "summary": f"运行环境检测失败：{exc}",
                "crt_files": {},
                "prompt_dismissed": False,
            }
        self.finished_probe.emit(report)


class MarqueeLabel(QLabel):
    """超长单行消息按 Token 停留策略向左滚动；含换行 / 图片时静态富文本显示。

    节奏（prefer 单 Token）：开头 padding 停顿 → 每个 Token：滚入边缘 → 停顿 → 滚出窗口
    → 结尾停顿；窗口仍可见则循环。时长为 0（手动关闭）时用舒适默认速度，仍保留首尾停顿。

    含 ``br`` / ``img`` / ``svg`` 或行内强调（``b`` / ``i`` / ``u`` / font-weight|style）
    时经 ``sanitize_html`` 后以 QLabel 富文本静态展示，保证中文粗斜体可见。
    仅颜色等非强调 ``span`` 仍可走单行 marquee。撤回提示可 ``force_rich``。
    """

    _TICK_MS = 40
    _MIN_HOLD_MS = 500
    _MIN_ENTER_MS = 125
    _MIN_EXIT_MS = 125
    _MIN_TOKEN_DWELL_MS = 250
    _DEFAULT_SCROLL_PX_PER_SEC = 80.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source_text = ""
        self._rich_mode = False
        self._rich_image_resources: list[tuple[str, QPixmap]] = []
        self._rich_image_temp_files: list[str] = []
        self._tokens: list[str] = []
        self._token_widths: list[int] = []
        self._token_offsets: list[float] = []  # left edge of each token in the full string
        self._offset = 0.0
        self._text_width = 0
        self._side_margin_px = 0
        self._display_duration_ms = 0
        self._phase = "idle"  # idle | hold_start | enter | dwell | exit | hold_end
        self._phase_elapsed_ms = 0
        self._token_index = 0
        self._hold_start_ms = self._MIN_HOLD_MS
        self._hold_end_ms = self._MIN_HOLD_MS
        self._enter_ms = self._MIN_ENTER_MS
        self._dwell_ms = self._MIN_TOKEN_DWELL_MS
        self._exit_ms = self._MIN_EXIT_MS
        self._scroll_from = 0.0
        self._scroll_to = 0.0
        self._cycle = 1
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._advance)
        self.setMinimumWidth(1)

    def _clear_rich_image_resources(self) -> None:
        self._rich_image_resources = []
        for path in list(getattr(self, "_rich_image_temp_files", []) or []):
            try:
                Path(path).unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                # Python 3.7 compat: missing_ok added in 3.8
                try:
                    p = Path(path)
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
            except OSError:
                pass
        self._rich_image_temp_files = []
        setter = getattr(self, "setResourceProvider", None)
        if callable(setter):
            try:
                setter(None)
            except Exception:
                pass

    def _materialize_rich_images(
        self, sanitized_html: str
    ) -> str:
        """Rewrite data-URI images so QLabel can paint them on Qt5 and Qt6.

        Prefer ``setResourceProvider`` (Qt ≥ 6.1). Otherwise write short-lived
        temp files and point ``img src`` at absolute paths (Qt rich-text subset
        loads local files reliably; long ``data:`` URLs often render blank).
        """
        display_html, image_resources = rewrite_data_images_for_qt(sanitized_html)
        self._clear_rich_image_resources()
        if not image_resources:
            return sanitized_html

        loaded: list[tuple[str, QPixmap]] = []
        for name, raw in image_resources:
            pixmap = QPixmap()
            if not pixmap.loadFromData(raw):
                LOGGER.warning("rich-text image decode failed resource={}", name)
                continue
            loaded.append((name, pixmap))
        self._rich_image_resources = loaded
        if not loaded:
            return sanitized_html

        mapping = {name: pixmap for name, pixmap in loaded}
        setter = getattr(self, "setResourceProvider", None)
        if callable(setter):
            def _provider(url: Any, type_: Any = None) -> Any:
                key = url.toString() if hasattr(url, "toString") else str(url)
                return mapping.get(key)

            try:
                setter(_provider)
                return display_html
            except Exception:
                LOGGER.debug("setResourceProvider failed; falling back to temp files", exc_info=True)

        # Qt5 / no provider: replace mkcb:// with temp file paths.
        rewritten = display_html
        temp_files: list[str] = []
        # Pair original bytes with successfully decoded names only.
        raw_by_name = {name: raw for name, raw in image_resources}
        for name, pixmap in loaded:
            raw = raw_by_name.get(name, b"")
            try:
                fd, path = tempfile.mkstemp(prefix="mkcb-img-", suffix=".png")
                os.close(fd)
                path_obj = Path(path)
                written = False
                if raw:
                    try:
                        path_obj.write_bytes(raw)
                        written = True
                    except OSError:
                        written = False
                if not written and not pixmap.save(str(path_obj), "PNG"):
                    try:
                        path_obj.unlink()
                    except OSError:
                        pass
                    continue
                temp_files.append(str(path_obj))
                file_url = QUrl.fromLocalFile(str(path_obj)).toString()
                rewritten = rewritten.replace(name, file_url)
            except Exception:
                LOGGER.warning("rich-text temp image write failed resource={}", name, exc_info=True)
        self._rich_image_temp_files = temp_files
        return rewritten

    def setText(self, text: str, *, force_rich: bool = False) -> None:  # type: ignore[override]
        raw = str(text)
        # Detect markup before sanitize so plain "a < b" stays marquee-friendly;
        # br/img/svg → static rich text; pure inline b/i/u/span → marquee (plain)
        # unless force_rich (e.g. withdrawal notice that must keep bold/italic).
        looks_html = looks_like_html(raw)
        sanitized = sanitize_html(raw) if looks_html else raw
        self._rich_mode = bool(
            looks_html and (force_rich or needs_static_rich_display(sanitized))
        )
        if self._rich_mode:
            self._source_text = sanitized if looks_html else raw
        elif looks_html:
            # Inline-only HTML: marquee paints glyphs, not Qt rich text — strip tags.
            self._source_text = html_to_plain_text(sanitized)
        else:
            self._source_text = raw
        self._offset = 0.0
        self._phase = "idle"
        self._phase_elapsed_ms = 0
        self._token_index = 0
        if self._rich_mode:
            self.setWordWrap(True)
            if RichText is not None:
                self.setTextFormat(RichText)
            display_html = self._materialize_rich_images(self._source_text)
            QLabel.setText(self, display_html)
            self._timer.stop()
            self._tokens = []
            self._token_widths = []
            self._token_offsets = []
            self._text_width = 0
        else:
            self._clear_rich_image_resources()
            self.setWordWrap(False)
            if PlainText is not None:
                self.setTextFormat(PlainText)
            # Keep an empty QLabel text so sizeHint is driven by our constraints,
            # not the full unwrapped string (which would expand past the screen).
            QLabel.setText(self, "" if "\n" not in self._source_text else self._source_text)
            self._rebuild_tokens()
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
        if self._rich_mode or "\n" in self._source_text:
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
        if self._rich_mode or not self._source_text or "\n" in self._source_text:
            return False
        return self._text_width > self._content_width()

    def _gap(self) -> int:
        return max(32, text_width(self.fontMetrics(), "    "))

    def _rebuild_tokens(self) -> None:
        from .core.tokenize import tokenize

        if self._rich_mode:
            self._tokens = []
            self._token_widths = []
            self._token_offsets = []
            self._text_width = 0
            return
        metrics = self.fontMetrics()
        raw = tokenize(self._source_text) if self._source_text and "\n" not in self._source_text else []
        # Keep visual order: rebuild offsets by walking the original string when possible.
        self._tokens = raw or ([self._source_text] if self._source_text and "\n" not in self._source_text else [])
        self._token_widths = [max(1, text_width(metrics, token)) for token in self._tokens]
        self._token_offsets = []
        # Prefer measuring cumulative width of joined tokens (jieba drops spaces).
        cursor = 0.0
        for width in self._token_widths:
            self._token_offsets.append(cursor)
            cursor += float(width)
        # Authoritative width is the full string: summing jieba tokens can undershoot
        # (kerning / CJK metrics), which made short lines like 「请…室」clip the ends.
        full_width = text_width(metrics, self._source_text) if self._source_text else 0
        token_sum = int(round(cursor)) if self._tokens else 0
        # leftBearing/rightBearing can be negative; reserve a few px so glyphs are not clipped.
        bearing = 0
        try:
            if self._source_text:
                first = self._source_text[0]
                last = self._source_text[-1]
                left = int(getattr(metrics, "leftBearing", lambda _c: 0)(first) or 0)
                right = int(getattr(metrics, "rightBearing", lambda _c: 0)(last) or 0)
                bearing = max(0, -left) + max(0, -right)
        except Exception:
            bearing = 0
        self._text_width = max(1, full_width, token_sum) + bearing + 2
        # If token-sum pacing is shorter than the full string, scale offsets so the
        # last glyph still lines up with the measured full width (minus bearing pad).
        paced = max(1, token_sum)
        target = max(1, self._text_width - bearing - 2)
        if self._token_offsets and paced != target:
            scale = float(target) / float(paced)
            self._token_offsets = [offset * scale for offset in self._token_offsets]
            self._token_widths = [max(1, int(round(width * scale))) for width in self._token_widths]

    def _recompute_scroll_profile(self) -> None:
        """Allocate start/end holds and per-token enter/dwell/exit within the display window."""
        gap = self._gap()
        self._cycle = max(1, self._text_width + gap)
        token_count = max(1, len(self._tokens))
        duration = self._display_duration_ms
        hold = self._MIN_HOLD_MS
        if duration <= 0:
            self._hold_start_ms = hold
            self._hold_end_ms = hold
            # Comfortable defaults for manual-close windows.
            self._enter_ms = self._MIN_ENTER_MS
            self._exit_ms = self._MIN_EXIT_MS
            self._dwell_ms = max(self._MIN_TOKEN_DWELL_MS, int(1000 * token_count / max(1, self._DEFAULT_SCROLL_PX_PER_SEC / 40)))
            return
        # Reserve ≥0.5s padding at both ends; distribute the rest across tokens.
        # Per token: ~12.5% enter + ~75% dwell + ~12.5% exit (matches the design example).
        usable = max(self._TICK_MS, duration - 2 * hold)
        per_token = max(self._MIN_ENTER_MS + self._MIN_TOKEN_DWELL_MS + self._MIN_EXIT_MS, usable // token_count)
        self._hold_start_ms = hold
        self._hold_end_ms = hold
        self._enter_ms = max(self._MIN_ENTER_MS, int(per_token * 0.125))
        self._exit_ms = max(self._MIN_EXIT_MS, int(per_token * 0.125))
        self._dwell_ms = max(self._MIN_TOKEN_DWELL_MS, per_token - self._enter_ms - self._exit_ms)

    def _token_focus_offset(self, index: int) -> float:
        """Offset that places token ``index`` at the left content edge."""
        if not self._token_offsets:
            return 0.0
        index = max(0, min(index, len(self._token_offsets) - 1))
        return float(self._token_offsets[index])

    def _token_exit_offset(self, index: int) -> float:
        """Offset that scrolls token ``index`` fully past the left edge."""
        if not self._token_offsets:
            return float(self._cycle)
        index = max(0, min(index, len(self._token_offsets) - 1))
        return float(self._token_offsets[index] + self._token_widths[index])

    def _begin_cycle(self) -> None:
        self._offset = 0.0
        self._phase_elapsed_ms = 0
        self._token_index = 0
        self._recompute_scroll_profile()
        self._phase = "hold_start"
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _start_enter(self) -> None:
        self._scroll_from = self._offset
        self._scroll_to = self._token_focus_offset(self._token_index)
        self._phase = "enter"
        self._phase_elapsed_ms = 0

    def _start_exit(self) -> None:
        self._scroll_from = self._offset
        if self._token_index >= len(self._tokens) - 1:
            self._scroll_to = float(self._cycle)
        else:
            self._scroll_to = self._token_exit_offset(self._token_index)
        self._phase = "exit"
        self._phase_elapsed_ms = 0

    def _lerp_offset(self, duration_ms: int) -> None:
        if duration_ms <= 0:
            self._offset = self._scroll_to
            return
        progress = min(1.0, self._phase_elapsed_ms / float(duration_ms))
        self._offset = self._scroll_from + (self._scroll_to - self._scroll_from) * progress

    def _update_marquee(self) -> None:
        self._rebuild_tokens()
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
            self._token_index = 0

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
                if self._tokens:
                    self._start_enter()
                else:
                    self._phase = "hold_end"
                    self._phase_elapsed_ms = 0
        elif self._phase == "enter":
            self._lerp_offset(self._enter_ms)
            if self._phase_elapsed_ms >= self._enter_ms:
                self._offset = self._scroll_to
                self._phase = "dwell"
                self._phase_elapsed_ms = 0
        elif self._phase == "dwell":
            if self._phase_elapsed_ms >= self._dwell_ms:
                self._start_exit()
        elif self._phase == "exit":
            self._lerp_offset(self._exit_ms)
            if self._phase_elapsed_ms >= self._exit_ms:
                self._offset = self._scroll_to
                self._token_index += 1
                if self._token_index < len(self._tokens):
                    self._start_enter()
                else:
                    self._phase = "hold_end"
                    self._phase_elapsed_ms = 0
        elif self._phase == "hold_end":
            if self._phase_elapsed_ms >= self._hold_end_ms:
                self._begin_cycle()
                return
        self.update()

    def paintEvent(self, event: Any) -> None:
        if self._rich_mode:
            # QLabel paints whitelist HTML (b/i/u/span/br/img); skip custom marquee paint.
            super().paintEvent(event)
            return
        if "\n" in self._source_text or not self._needs_marquee():
            if "\n" not in self._source_text and self._source_text:
                painter = QPainter(self)
                painter.setPen(self.palette().color(self.foregroundRole()))
                painter.setFont(self.font())
                # Do not clip to side margins here — margins are already empty space;
                # clipping the draw rect was cutting CJK glyphs at both ends.
                rect = self.rect().adjusted(self._side_margin_px, 0, -self._side_margin_px, 0)
                painter.setClipRect(self.rect())
                painter.drawText(rect, int(AlignCenter | AlignVCenter), self._source_text)
                painter.end()
                return
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        # Clip to the full label; side margins are padding via x0, not a hard crop.
        # Cropping to margin-adjusted rect clipped the first/last CJK glyph.
        painter.setClipRect(self.rect())
        baseline = (self.height() + self.fontMetrics().ascent() - self.fontMetrics().descent()) // 2
        # Draw tokens at measured offsets so jieba splits stay visually contiguous.
        x0 = self._side_margin_px - int(self._offset)
        if self._tokens and self._token_offsets:
            for index, token in enumerate(self._tokens):
                x = x0 + int(self._token_offsets[index])
                painter.drawText(x, baseline, token)
            # Optional trailing copy after the gap so the loop feels continuous.
            trail = x0 + self._cycle
            if trail < self.width() - self._side_margin_px:
                for index, token in enumerate(self._tokens):
                    painter.drawText(trail + int(self._token_offsets[index]), baseline, token)
        else:
            painter.drawText(x0, baseline, self._source_text)
        painter.end()


_FALLBACK_THIRD_PARTY_LICENSES = {
    "title": "开源软件版权声明",
    "intro": "未能加载完整许可证列表。本项目采用 BSD-3-Clause；依赖组件许可证详见仓库 THIRD_PARTY_LICENSES.md。",
    "entries": [
        {
            "name": "lovemilk class broadcaster",
            "license": "BSD-3-Clause",
            "url": "https://github.com/lovemilk/class-broadcaster",
            "note": "本项目",
        }
    ],
}


def _third_party_licenses_candidates() -> list[Path]:
    """Resolve packaged / source paths for third_party_licenses.json."""
    here = Path(__file__).resolve().parent
    candidates: list[Path] = [
        # Prefer next to the client package (dev tree and packaged Lib/client/).
        here / "resources" / "third_party_licenses.json",
        # Fallback when imported from a different layout.
        here.parent / "client" / "resources" / "third_party_licenses.json",
    ]
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None
    if cwd is not None:
        candidates.append(cwd / "client" / "resources" / "third_party_licenses.json")
        candidates.append(cwd / "resources" / "third_party_licenses.json")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _load_third_party_licenses() -> dict[str, Any]:
    for path in _third_party_licenses_candidates():
        try:
            if not path.is_file():
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, UnicodeError):
            continue
        if isinstance(document, dict) and isinstance(document.get("entries"), list):
            return document
    return dict(_FALLBACK_THIRD_PARTY_LICENSES)


def _format_third_party_licenses(document: dict[str, Any]) -> str:
    title = str(document.get("title") or "开源软件版权声明")
    intro = str(document.get("intro") or "").strip()
    lines = [title, ""]
    if intro:
        lines.extend([intro, ""])
    entries = document.get("entries")
    if not isinstance(entries, list):
        entries = []
    for index, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip() or f"条目 {index}"
        license_name = str(raw.get("license") or "").strip() or "未知"
        url = str(raw.get("url") or "").strip()
        note = str(raw.get("note") or "").strip()
        lines.append(f"{index}. {name}")
        lines.append(f"   许可证：{license_name}")
        if url:
            lines.append(f"   地址：{url}")
        if note:
            lines.append(f"   说明：{note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class MessageDisplayDialog(QDialog):
    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Key_Escape:
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
        # message_id → in-memory Piper WAV prefetch (not persisted).
        self._tts_cache: dict[str, TtsCacheEntry] = {}
        self._tts_prefetch_workers: dict[str, TtsPrefetchWorker] = {}
        self._tts_cache_max_entries = 8
        # When True, _play_next is waiting for Piper prefetch before showing the popup.
        self._awaiting_tts_ready = False
        # Ephemeral "处理" activity while deferred on TTS prefetch (no popup yet).
        self._processing_message_id = ""
        self._processing_plain = ""
        self._processing_started_at = 0.0
        self._processing_last_log_at = 0.0
        self.current_displayed = False
        self.current_speech_done = False
        self.current_speech_success = False
        self._config_snapshot: Optional[ConfigSnapshot] = None
        # Per-server runtime state so multi-server configs do not clobber each other.
        # (display_position, display_duration_ratio, speech_rate)
        self._server_display_defaults: dict[tuple[str, int], tuple[str, float, float]] = {}
        self._server_config_ids: dict[tuple[str, int], str] = {}
        self.speech_rate = 0.75
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
        # Windows: probe piper-tts / CRT at startup (not on first message).
        QTimer.singleShot(800, self._refresh_runtime_diagnostics)

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
        self.settings_categories.setHorizontalScrollBarPolicy(ScrollBarAlwaysOff)
        self.settings_categories.setVerticalScrollBarPolicy(ScrollBarAlwaysOff)
        self.settings_categories.addItems(["连接与服务端", "客户端身份", "系统", "运行环境", "关于"])
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
        self.identity_label.setTextInteractionFlags(TextSelectableByMouse)

        connection_page = QWidget()
        connection_layout = QVBoxLayout(connection_page)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_group = QGroupBox("连接与服务端")
        connection_group_layout = QVBoxLayout(connection_group)
        form = QFormLayout()
        self.destination = QLineEdit("255.255.255.255")
        self.destination.setPlaceholderText(
            "例如 255.255.255.255、192.168.1.0/24、192.168.1.10 或 auto"
        )
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
        self.scanned_list.currentRowChanged.connect(
            lambda _row: self._on_scanned_selection_changed()
        )
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

        runtime_page = QWidget()
        runtime_layout = QVBoxLayout(runtime_page)
        runtime_layout.setContentsMargins(0, 0, 0, 0)
        runtime_group = QGroupBox("运行环境")
        runtime_group_layout = QVBoxLayout(runtime_group)
        self.runtime_summary_label = QLabel("正在检测…")
        self.runtime_summary_label.setWordWrap(True)
        runtime_group_layout.addWidget(self.runtime_summary_label)
        self.runtime_detail_label = QLabel("")
        self.runtime_detail_label.setWordWrap(True)
        self.runtime_detail_label.setTextInteractionFlags(TextSelectableByMouse)
        self.runtime_detail_label.setStyleSheet("color: #555;")
        runtime_group_layout.addWidget(self.runtime_detail_label)
        runtime_actions = QHBoxLayout()
        self.runtime_refresh_button = QPushButton("重新检测")
        self.runtime_refresh_button.clicked.connect(self._refresh_runtime_diagnostics)
        runtime_actions.addWidget(self.runtime_refresh_button)
        self.runtime_vc_download_button = QPushButton("打开 VC++ 运行库下载")
        self.runtime_vc_download_button.setToolTip(
            "Microsoft Visual C++ 2015–2022 Redistributable (x64)\n{}".format(VC_REDIST_X64_URL)
        )
        self.runtime_vc_download_button.clicked.connect(self._open_vc_redist_download)
        runtime_actions.addWidget(self.runtime_vc_download_button)
        runtime_actions.addStretch(1)
        runtime_group_layout.addLayout(runtime_actions)
        runtime_hint = QLabel(
            "高质量朗读（piper-tts / huayan·lessac）依赖较新的 VC++ 2015–2022 x64 运行库。"
            "若检测失败，可从此处打开微软官方下载；安装后请重启客户端。"
        )
        runtime_hint.setWordWrap(True)
        runtime_hint.setStyleSheet("color: #666;")
        runtime_group_layout.addWidget(runtime_hint)
        runtime_group_layout.addStretch(1)
        runtime_layout.addWidget(runtime_group, 1)
        self.settings_pages.addWidget(runtime_page)
        self._runtime_report: dict[str, Any] = {}
        self._runtime_probe_worker: Optional[RuntimeProbeWorker] = None

        about_page = QWidget()
        about_layout = QVBoxLayout(about_page)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_group = QGroupBox("关于")
        about_group_layout = QVBoxLayout(about_group)
        about_group_layout.addWidget(QLabel("lovemilk class broadcaster"))
        about_group_layout.addWidget(QLabel("客户端配置与叫号接收程序"))
        about_version_text = display_version()
        self.about_version_button = QPushButton(f"版本 {about_version_text}（复制）")
        self.about_version_button.setToolTip("点击复制版本与构建时间；连续点击 7 次打开开发者面板")
        self.about_version_button.clicked.connect(self.about_version_clicked)
        about_group_layout.addWidget(self.about_version_button)
        build_label = QLabel(
            "构建时间：{}".format(BUILD_DATE if BUILD_DATE else "开发版（未打构建戳）")
        )
        build_label.setStyleSheet("color: #666;")
        about_group_layout.addWidget(build_label)
        self.about_licenses_button = QPushButton("开源软件版权声明")
        self.about_licenses_button.setToolTip("查看本软件使用的开源组件与许可证")
        self.about_licenses_button.clicked.connect(self.show_third_party_licenses)
        about_group_layout.addWidget(self.about_licenses_button)
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
        # Category order: 连接 / 身份 / 系统 / 运行环境 / 关于
        if index == 3:
            self._refresh_runtime_diagnostics()

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

    def show_third_party_licenses(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("开源软件版权声明")
        dialog.setModal(True)
        dialog.resize(640, 480)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(dialog)
        text.setReadOnly(True)
        text.setPlainText(_format_third_party_licenses(_load_third_party_licenses()))
        layout.addWidget(text, 1)
        close_btn = QPushButton("关闭", dialog)
        close_btn.clicked.connect(dialog.accept)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(close_btn)
        layout.addLayout(actions)
        dialog.exec_()

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
        self.display.setWindowFlags(Window | FramelessWindowHint | WindowStaysOnTopHint | Tool)
        self.display.resize(900, 360)
        self._display_layout = QVBoxLayout(self.display)
        self._display_layout.setSpacing(8)
        self.display_time = QLabel()
        self.display_time.setAlignment(AlignLeft | AlignVCenter)
        self._display_layout.addWidget(self.display_time)
        self.display_content = MarqueeLabel()
        self.display_content.setAlignment(AlignCenter)
        self.display_content.setWordWrap(False)
        self.display_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._display_layout.addWidget(self.display_content, 1)
        close_row = QHBoxLayout()
        self.display_close_button = QPushButton("关闭")
        self.display_close_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.display_close_button.setMinimumHeight(22)
        self.display_close_button.clicked.connect(self._dismiss_current)
        self.display_close_button.hide()
        close_row.addWidget(self.display_close_button)
        self._display_layout.addLayout(close_row)
        self.display_timer = QTimer(self)
        self.display_timer.setSingleShot(True)
        self.display_timer.timeout.connect(self._display_minimum_elapsed)
        self.withdrawal_timer = QTimer(self)
        self.withdrawal_timer.setSingleShot(True)
        self.withdrawal_timer.timeout.connect(self._withdrawal_display_elapsed)
        # Periodic log + activity refresh while waiting on TTS prefetch.
        self._processing_timer = QTimer(self)
        self._processing_timer.setInterval(5000)
        self._processing_timer.timeout.connect(self._tick_processing_activity)
        self.withdrawal_notice_active = False
        self.withdrawal_displayed = False
        self.withdrawal_speech_done = True
        self.withdrawal_speech: Optional[SpeechWorker] = None
        self._withdrawal_speech_id = "withdrawal-notice"
        # Admin-pushed from ConnectResp; empty family / size → platform defaults (render only).
        self._server_default_font_family = ""
        self._server_default_font_size = ""  # expression; empty → built-in 25vh
        self._apply_message_font()

    def _active_screen_size(self) -> tuple[int, int]:
        """Width/height of the screen that hosts the message popup (for vw/vh)."""
        try:
            widget = getattr(self, "display", None)
            if widget is not None:
                screen = widget.screen()
                if screen is not None:
                    geo = screen.availableGeometry()
                    return int(geo.width()), int(geo.height())
            app = QApplication.instance()
            if app is not None:
                screen = app.primaryScreen()
                if screen is not None:
                    geo = screen.availableGeometry()
                    return int(geo.width()), int(geo.height())
        except Exception:
            pass
        return 0, 0

    def _effective_message_font_family(self) -> str:
        """Admin per-device family when set; otherwise platform default."""
        server_family = str(getattr(self, "_server_default_font_family", "") or "").strip()
        return resolve_message_font_family(server_family)

    def _resolve_font_size_expr(self, expr: object) -> int:
        width, height = self._active_screen_size()
        return resolve_font_size_px(
            expr,
            screen_width=width,
            screen_height=height,
            default_px=FONT_DEFAULT_PX,
        )

    def _effective_message_font_size(self, override: Optional[str] = None) -> int:
        """Message override → device default expression → built-in 25vh."""
        if override is not None and str(override).strip():
            return self._resolve_font_size_expr(override)
        return self._resolve_font_size_expr(getattr(self, "_server_default_font_size", "") or "")

    def _apply_message_font(
        self, size_px: Optional[int] = None, family: Optional[str] = None
    ) -> None:
        """Update broadcast body/time styles, family, and 0.25em side margins."""
        from .core.fontsize import clamp_px as clamp_font_px

        content_font_px = (
            int(size_px)
            if size_px is not None
            else self._effective_message_font_size()
        )
        # Already resolved to pixels — only clamp for Qt, do NOT re-validate as "Npx"
        # (admin px max is 120; 25vh on a tall screen is often larger).
        content_font_px = clamp_font_px(content_font_px)
        family_name = family if family is not None else self._effective_message_font_family()
        family_name = resolve_message_font_family(family_name)
        side_margin_px = max(1, int(round(content_font_px * 0.25)))
        time_font_px = max(12, min(36, int(round(content_font_px * 0.47))))
        if hasattr(self, "_display_layout") and self._display_layout is not None:
            self._display_layout.setContentsMargins(side_margin_px, 12, side_margin_px, 12)
        family_css = family_name.replace("\\", "\\\\").replace('"', '\\"')
        if hasattr(self, "display_time") and self.display_time is not None:
            self.display_time.setStyleSheet(
                'font-family: "{}"; font-size: {}px; font-weight: 600;'.format(
                    family_css, time_font_px
                )
            )
        if hasattr(self, "display_content") and self.display_content is not None:
            # Body default is regular; do NOT set a widget-level font-weight that
            # Qt rich text inherits onto <b>/<i> (CJK bold would look identical).
            # Emphasize via document CSS so <b>/<strong>/<i>/<em> stay distinct.
            self.display_content.setStyleSheet(
                'font-family: "{}"; font-size: {}px;'
                " font-weight: 400;"
                " b, strong {{ font-weight: 700; }}"
                " i, em {{ font-style: italic; }}".format(family_css, content_font_px)
            )
            try:
                font = QFont(family_name, content_font_px)
                font.setBold(False)
                font.setItalic(False)
                # Prefer synthetic bold/italic when the CJK face has no dedicated style.
                try:
                    font.setStyleStrategy(
                        getattr(QFont, "PreferAntialias", 0)
                        | getattr(QFont, "ForceOutline", 0)
                    )
                except Exception:
                    pass
                self.display_content.setFont(font)
            except Exception:
                pass
            self.display_content.set_side_margin_px(side_margin_px)
            try:
                self.display_content.updateGeometry()
                self.display_content.update()
            except Exception:
                pass
        if getattr(self, "display", None) is not None and self.display.isVisible():
            self._position_display()

    def _server_font_updated(
        self, endpoint_key: object, family: str, size_expr: str = ""
    ) -> None:
        """Apply admin per-device font family/size expression from ConnectResp."""
        _ = endpoint_key
        cleaned = str(family or "").strip()
        if cleaned in ("(默认)", "(default)", "default"):
            cleaned = ""
        size = str(size_expr or "").strip()
        if size in ("0", "(默认)", "(default)", "default"):
            size = ""
        previous_family = str(getattr(self, "_server_default_font_family", "") or "")
        previous_size = str(getattr(self, "_server_default_font_size", "") or "")
        self._server_default_font_family = cleaned
        self._server_default_font_size = size
        if cleaned != previous_family or size != previous_size:
            LOGGER.info(
                "server default font applied family={!r} size={!r}",
                cleaned or "(platform)",
                size or FONT_DEFAULT_EXPR,
            )
            self._apply_message_font()

    def _config_updated(self, endpoint_key: object, snapshot: ConfigSnapshot) -> None:
        # Keep per-server defaults; only apply global UI defaults when this endpoint
        # is the currently selected server (or no selection yet).
        if not isinstance(snapshot, ConfigSnapshot):
            return
        key: Optional[tuple[str, int]] = None
        if isinstance(endpoint_key, tuple) and len(endpoint_key) == 2:
            key = (str(endpoint_key[0]), int(endpoint_key[1]))
            self._server_display_defaults[key] = (
                snapshot.display_position,
                float(snapshot.display_duration_ratio),
                float(getattr(snapshot, "speech_rate", DEFAULT_SPEECH_RATE) or DEFAULT_SPEECH_RATE),
            )
            self._server_config_ids[key] = snapshot.config_id
            self.state_store.set_server_config_id(
                host=key[0], tcp_port=key[1], config_id=snapshot.config_id
            )
        selected = self._selected_list_item()
        selected_key = self._endpoint_key(selected.data(UserRole)) if selected is not None else None
        if key is None or selected_key is None or selected_key == key:
            self._config_snapshot = snapshot
            self.display_position = snapshot.display_position
            self.display_duration_ratio = snapshot.display_duration_ratio
            self.speech_rate = float(
                getattr(snapshot, "speech_rate", DEFAULT_SPEECH_RATE) or DEFAULT_SPEECH_RATE
            )
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
        rich_mode = bool(getattr(self.display_content, "_rich_mode", False))
        metrics = self.display_content.fontMetrics()
        side = max(0, int(getattr(self.display_content, "_side_margin_px", 0)))
        layout_margins = (
            self.display.layout().contentsMargins() if self.display.layout() is not None else None
        )
        outer_side = (
            (layout_margins.left() + layout_margins.right()) if layout_margins is not None else 0
        )
        if rich_mode:
            # Prefer measured plain width; do not let unbounded rich sizeHint dominate.
            plain = html_to_plain_text(text)
            first_line = plain.split("\n", 1)[0] if plain else ""
            measured = text_width(metrics, first_line) if first_line else 0
            needed = measured + 2 * side + outer_side + 8
            # Allow some growth for images / multi-line, but stay under 95% screen.
            width = min(max_width, max(320, needed, min(natural_width, max_width)))
        else:
            measured = text_width(metrics, text.split("\n", 1)[0]) if text else 0
            needed = measured + 2 * side + outer_side + 8
            width = min(
                max_width, max(320, needed if "\n" not in text else natural_width, natural_width)
            )
        width = min(width, max_width)
        # Rich (br/img) can inflate sizeHint toward full screen — cap at ~45% height.
        max_height = max(1, area.height() - margin * 2)
        if rich_mode:
            max_height = min(max_height, max(160, math.ceil(area.height() * 0.45)))
        height = min(max(self.display.sizeHint().height(), 160), max_height)
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
        x = max(
            area.left() + margin,
            min(horizontal[column], area.left() + area.width() - width - margin),
        )
        y = max(
            area.top() + margin, min(vertical[row], area.top() + area.height() - height - margin)
        )
        point = QPoint(x, y)
        # X11 (Debian classroom): move + setGeometry + QWindow.setPosition; re-assert after map.
        self.display.move(point)
        try:
            self.display.setGeometry(x, y, width, height)
        except Exception:
            pass
        window_handle = self.display.windowHandle()
        if window_handle is not None:
            try:
                window_handle.setPosition(point)
            except Exception:
                pass
        self._last_display_position = position
        self._last_display_point = point

    def _raise_display(self) -> None:
        """每次消息显示时重新声明顶层窗口，兼容 Linux 窗口管理器。"""
        self.display.setWindowFlag(WindowStaysOnTopHint, True)
        # setWindowFlag can hide the widget on some Qt5 builds — ensure visible again.
        if not self.display.isVisible():
            self.display.show()
        self.display.raise_()
        self.display.activateWindow()
        window_handle = self.display.windowHandle()
        if window_handle is not None:
            try:
                window_handle.setFlag(WindowStaysOnTopHint, True)
                window_handle.raise_()
            except Exception:
                pass

    def _show_display(self, position: Optional[str] = None) -> None:
        """显示无边框消息窗并在窗口管理器完成映射后再次定位和置顶。

        Linux/X11: deferred re-position (0ms / 50ms / 200ms) so WMs that ignore the
        first move() still land on the nine-grid slot after map.
        """
        self.display.setWindowFlag(WindowStaysOnTopHint, True)
        self.display.show()
        self._raise_display()
        self._position_display(position)
        for delay_ms in (0, 50, 200):
            QTimer.singleShot(delay_ms, lambda pos=position: self._position_display(pos))
            QTimer.singleShot(delay_ms, self._raise_display)

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
        worker = ConnectionWorker(
            endpoint, self.private_key, self.certificate, self.state_store, self.data_dir
        )
        LOGGER.info("starting connection {}:{}", endpoint.get("host"), endpoint.get("tcp_port"))
        self.connection_workers[endpoint_key] = worker
        self._connection_attempts.add(endpoint_key)
        self._update_remove_button()
        worker.status.connect(self._connection_status_changed)
        worker.connection_state.connect(self._connection_state_changed)
        worker.finished.connect(
            lambda key=endpoint_key, w=worker: self._connection_worker_finished(key, w)
        )
        worker.config_updated.connect(self._config_updated)
        worker.certificate_mismatch.connect(self._confirm_server_fingerprint_change)
        worker.received.connect(self._message_received)
        worker.withdrawn.connect(self._message_withdrawn)
        worker.server_font_updated.connect(self._server_font_updated, QueuedConnection)
        # QueuedConnection keeps the connection thread free while UI schedules the apply worker.
        worker.update_available.connect(self._update_available, QueuedConnection)
        worker.start()
        self._update_connect_button()

    def _connection_worker_finished(
        self, endpoint_key: tuple[str, int], worker: ConnectionWorker
    ) -> None:
        current = self.connection_workers.get(endpoint_key)
        if current is worker:
            self.connection_workers.pop(endpoint_key, None)
        self._connected_endpoint_keys.discard(endpoint_key)
        self._update_connect_button()
        self._refresh_server_list_states()

    def _update_available(self, update: dict[str, Any]) -> None:
        """Schedule download/verify/apply on a background thread (never block the UI loop)."""
        if not isinstance(update, dict):
            LOGGER.error(
                "update available ignored: payload is not a dict type={}", type(update).__name__
            )
            return
        digest = str(update.get("sha256") or "").lower()
        LOGGER.info(
            "update available received on UI thread digest={} version={} client_version={} force={} has_token={}",
            digest,
            update.get("version"),
            str(update.get("client_version") or "").strip() or "-",
            bool(update.get("force")),
            bool(update.get("download_token")),
        )
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            LOGGER.error("update available ignored: invalid digest={!r}", update.get("sha256"))
            return
        # Server decides whether to offer (applied_seq + version). Once offered,
        # the client always applies unless this exact digest was already applied
        # (local memory / pending UpdateApplied after reconnect).
        seq = 0
        try:
            seq = int(update.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        version = str(update.get("version") or "")
        if is_update_applied(self.data_dir, digest) or digest in self._update_apply_done:
            LOGGER.info("update apply skipped (already applied) digest={}", digest)
            self._update_apply_done.add(digest)
            mark_update_applied(self.data_dir, digest)
            self._queue_update_applied_report_all(digest, version, seq)
            return
        if digest in self._update_apply_in_progress:
            LOGGER.info("update apply skipped (already in progress) digest={}", digest)
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
                    fp = (
                        getattr(worker, "server_fingerprint", None)
                        or endpoint.get("fingerprint")
                        or b""
                    )
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
            # Pending report persists across quit; reconnect flush sends UpdateApplied.
            # Also queue now in case we stay up (dev mode / no quit).
            self._queue_update_applied_report_all(sha, version, seq)
            try:
                self._update_workers.remove(worker_ref)
            except ValueError:
                pass
            LOGGER.info("update apply worker finished ok digest={}", sha)

        def _on_err(sha: str, error: str, worker_ref: UpdateApplyWorker = apply_worker) -> None:
            self._update_apply_in_progress.discard(sha)
            # Allow a later force catch-up / republish to retry the same digest.
            try:
                self._update_workers.remove(worker_ref)
            except ValueError:
                pass
            LOGGER.error("update apply worker finished error digest={} error={}", sha, error)

        apply_worker.finished_ok.connect(_on_ok, QueuedConnection)
        apply_worker.finished_error.connect(_on_err, QueuedConnection)
        apply_worker.quit_for_update.connect(self._quit_for_update, QueuedConnection)
        apply_worker.start()
        LOGGER.info("update apply worker started digest={}", digest)

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
                LOGGER.warning(
                    "could not rename legacy update package {} -> {}: {}", legacy, canonical, exc
                )
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
        self.tray.activated.connect(self._tray_activated)
        # Some Windows environments report tray as unavailable until the shell is ready.
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            self.tray.setToolTip("lovemilk class broadcaster")
        else:
            LOGGER.warning("system tray unavailable; settings window will stay visible")

    def _tray_activated(self, reason: object) -> None:
        """Open settings on left-click; ignore other activation reasons.

        PySide2 exposes ``QSystemTrayIcon.Trigger``; PySide6 nests it under
        ``ActivationReason``. Guard against mid-shutdown clicks after Ctrl+C.
        """
        if getattr(self, "_shutting_down", False):
            return
        trigger = getattr(QSystemTrayIcon, "Trigger", None)
        nested = getattr(QSystemTrayIcon, "ActivationReason", None)
        if nested is not None:
            trigger = getattr(nested, "Trigger", trigger)
        if trigger is None or reason != trigger:
            return
        try:
            self.show_window()
        except Exception:
            LOGGER.exception("tray activation failed to show settings window")

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
        key = self._endpoint_key(item.data(UserRole)) if item is not None else None
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
                snap = worker.last_snapshot
                self._server_display_defaults[key] = (
                    snap.display_position,
                    float(snap.display_duration_ratio),
                    float(getattr(snap, "speech_rate", DEFAULT_SPEECH_RATE) or DEFAULT_SPEECH_RATE),
                )
        if config_id:
            defaults = self._server_display_defaults.get(key)
            if defaults:
                self.config_status_label.setText(
                    "配置：{}（显示 {} / 比率 {} / 语速 {}）".format(
                        config_id, defaults[0], defaults[1], defaults[2]
                    )
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
            previous_key = self._endpoint_key(current.data(UserRole))
        self.saved_list.clear()
        select_row = 0
        for index, saved in enumerate(self.state_store.servers()):
            host = str(saved.get("host", ""))
            port = int(saved.get("tcp_port", 0) or 0)
            base = "{}:{}".format(host, port)
            endpoint_key = self._endpoint_key(saved)
            state = self._connection_state_label(endpoint_key)
            item = QListWidgetItem("{}  [{}]".format(base, state))
            item.setData(UserRole, saved)
            item.setData(UserRole + 1, base)
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
            previous_key = self._endpoint_key(current.data(UserRole))
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
            list_item.setData(UserRole, server)
            list_item.setData(UserRole + 1, base)
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
        scanned_keys = {
            key for key in (self._endpoint_key(server) for server in servers) if key is not None
        }
        self._connection_attempts.intersection_update(
            scanned_keys | set(self._connected_endpoint_keys)
        )
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
        value = item.data(UserRole)
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
                if str(item.get("host", "")).strip() == host
                and int(item.get("tcp_port", 0)) == port
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
            if self._endpoint_key(self.saved_list.item(row).data(UserRole)) == target:
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
        key = self._endpoint_key(item.data(UserRole)) if item is not None else None
        self.remove_server_button.setEnabled(item is not None and key is not None)

    def _refresh_list_item_state(self, item: QListWidgetItem) -> None:
        key = self._endpoint_key(item.data(UserRole))
        base = item.data(UserRole + 1) or item.text()
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
                if self._endpoint_key(item.data(UserRole)) == key:
                    base = item.data(UserRole + 1) or item.text()
                    item.setText("{}  [{}]".format(base, status))
        selected = self._selected_list_item()
        selected_key = self._endpoint_key(selected.data(UserRole)) if selected is not None else None
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
        selected_key = self._endpoint_key(item.data(UserRole)) if item is not None else None
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
        server = item.data(UserRole)
        try:
            fingerprint = (
                server.fingerprint
                if isinstance(server, DiscoveryResponse)
                else base64.b64decode(server["fingerprint"])
            )
        except (TypeError, ValueError, KeyError):
            fingerprint = b""
        if not fingerprint or not any(fingerprint):
            self.fingerprint_label.setText(
                "服务端公钥 SHA-256：\n尚未获取，连接时将先确认服务端证书。"
            )
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
        value = item.data(UserRole)
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
        self._connect_endpoint(
            str(host), int(tcp_port), int(http_port or 39003), bytes(fingerprint)
        )

    def _connect_endpoint(
        self, host: str, tcp_port: int, http_port: int, fingerprint: bytes
    ) -> None:
        """Connect = save + connect + enable auto_connect for this endpoint."""
        endpoint_key = (str(host), int(tcp_port))
        existing_worker = self.connection_workers.get(endpoint_key)
        if (
            endpoint_key in self._connected_endpoint_keys
            and existing_worker is not None
            and existing_worker.isRunning()
        ):
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
                QMessageBox.warning(
                    self, "读取服务端证书失败", "{}:{}\n{}".format(host, tcp_port, exc)
                )
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
                if str(saved.get("host")) == message.source_host and int(
                    saved.get("tcp_port", 0)
                ) == int(message.source_tcp_port or 0):
                    try:
                        server_id = base64.b64decode(saved.get("fingerprint", ""), validate=True)
                    except (TypeError, ValueError):
                        server_id = b""
                    break
        if not self.dedup.observe(server_id, message.message_id):
            self._ack(message, "received")
            return
        self.pending_messages.put(message)
        # Prefetch Piper WAV while the message waits in the queue (or behind current).
        if message.tts_enabled:
            self._start_tts_prefetch(message)
        if self.current_message is None:
            self._play_next()
        elif message.priority < self.current_message.priority:
            # Re-queue interrupted message for local replay; final ack is sent
            # only after that replay finishes (not at preemption time).
            interrupted = self.current_message
            self.pending_messages.put(interrupted)
            self._stop_current_speech()
            self._play_next()

    def _resolve_message_speech_rate(self, message: IncomingMessage) -> float:
        source_key: Optional[tuple[str, int]] = None
        if message.source_host and message.source_tcp_port:
            source_key = (str(message.source_host), int(message.source_tcp_port))
        server_defaults = self._server_display_defaults.get(source_key) if source_key else None
        base_speech_rate = (
            server_defaults[2]
            if server_defaults and len(server_defaults) >= 3
            else self.speech_rate
        )
        if message.as_default and message.default_speech_rate is not None:
            base_speech_rate = float(message.default_speech_rate)
        return normalize_speech_rate(
            message.speech_rate
            if message.speech_rate is not None
            else (
                message.default_speech_rate
                if message.default_speech_rate is not None
                else base_speech_rate
            ),
            default=DEFAULT_SPEECH_RATE,
        )

    def _compile_message_speech_parts(self, message: IncomingMessage) -> list[SpeechPart]:
        """Strip HTML from speech nodes / content and compile SpeechPart list."""
        plain_for_tts = html_to_plain_text(message.content)
        speech_nodes = list(message.speech) if message.speech else []
        if speech_nodes:
            stripped: list[dict[str, Any]] = []
            for node in speech_nodes:
                if not isinstance(node, dict):
                    stripped.append(node)  # type: ignore[arg-type]
                    continue
                if str(node.get("type", "text")) == "text":
                    value = html_to_plain_text(str(node.get("value", "")))
                    stripped.append({**node, "value": value})
                else:
                    stripped.append(node)
            return compile_speech(stripped)
        if plain_for_tts:
            return compile_speech([{"type": "text", "value": plain_for_tts}])
        return []

    def _start_tts_prefetch(self, message: IncomingMessage) -> None:
        """Kick off Piper synthesize-only prefetch for a queued message."""
        message_id = message.message_id
        if not message.tts_enabled or message_id in self.withdrawn_ids:
            return
        existing = self._tts_cache.get(message_id)
        if existing is not None and (not existing.ready.is_set() or existing.plan):
            return
        try:
            parts = self._compile_message_speech_parts(message)
        except (TypeError, ValueError) as exc:
            LOGGER.warning(
                "TTS prefetch skipped message_id={} reason=invalid_speech error={}",
                message_id,
                exc,
            )
            return
        if not parts:
            return
        speech_rate = self._resolve_message_speech_rate(message)
        self._evict_tts_cache_if_needed(keep=message_id)
        entry = TtsCacheEntry(message_id, parts, speech_rate)
        self._tts_cache[message_id] = entry
        worker = TtsPrefetchWorker(entry, self.data_dir, self)
        self._tts_prefetch_workers[message_id] = worker
        worker.finished_prefetch.connect(
            lambda mid, _ok, _err: self._on_tts_prefetch_finished(mid),
            QueuedConnection,
        )
        worker.start()
        LOGGER.info(
            "TTS prefetch started message_id={} parts={} speech_rate={}",
            message_id,
            len(parts),
            speech_rate,
        )

    def _on_tts_prefetch_finished(self, message_id: str) -> None:
        worker = self._tts_prefetch_workers.pop(message_id, None)
        if worker is not None:
            worker.wait(100)
        # Show the popup only after prefetch settles (success or failure → live fallback).
        if (
            self._awaiting_tts_ready
            and self.current_message is not None
            and self.current_message.message_id == message_id
            and message_id not in self.withdrawn_ids
        ):
            elapsed_ms = self._processing_elapsed_ms()
            LOGGER.info(
                "TTS ready; presenting deferred message message_id={} elapsed_ms={} content={}",
                message_id,
                elapsed_ms,
                self._processing_plain or html_to_plain_text(self.current_message.content),
            )
            self._clear_processing_activity()
            self._present_current_message()

    def _processing_elapsed_ms(self) -> int:
        if self._processing_started_at <= 0:
            return 0
        return max(0, int((time.monotonic() - self._processing_started_at) * 1000))

    def _report_activity_to_workers(
        self,
        state: str,
        *,
        message_id: str = "",
        detail: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        """Fan-out ClientActivity to every live TLS worker (multi-server safe)."""
        for worker in list(self.connection_workers.values()):
            try:
                worker.report_activity(
                    state,
                    message_id=message_id,
                    detail=detail,
                    elapsed_ms=elapsed_ms,
                )
            except Exception:
                pass

    def _begin_processing_activity(self, message: IncomingMessage) -> None:
        """Mark admin status as 处理 and start progress logging (no popup)."""
        plain = html_to_plain_text(message.content).replace("\r", " ").replace("\n", " ").strip()
        if len(plain) > 200:
            plain = plain[:200] + "…"
        self._awaiting_tts_ready = True
        self._processing_message_id = str(message.message_id)
        self._processing_plain = plain
        self._processing_started_at = time.monotonic()
        self._processing_last_log_at = self._processing_started_at
        LOGGER.info(
            "message display deferred until TTS ready message_id={} content={}",
            message.message_id,
            plain,
        )
        self._report_activity_to_workers(
            "processing",
            message_id=str(message.message_id),
            detail=plain,
            elapsed_ms=0,
        )
        if not self._processing_timer.isActive():
            self._processing_timer.start()

    def _tick_processing_activity(self) -> None:
        """Periodic log + activity refresh while waiting on TTS prefetch."""
        if not self._awaiting_tts_ready or not self._processing_message_id:
            self._processing_timer.stop()
            return
        message = self.current_message
        if message is None or message.message_id != self._processing_message_id:
            self._clear_processing_activity()
            return
        elapsed_ms = self._processing_elapsed_ms()
        elapsed_s = elapsed_ms / 1000.0
        LOGGER.info(
            "TTS processing message_id={} elapsed={:.1f}s content={}",
            self._processing_message_id,
            elapsed_s,
            self._processing_plain,
        )
        self._processing_last_log_at = time.monotonic()
        self._report_activity_to_workers(
            "processing",
            message_id=self._processing_message_id,
            detail=self._processing_plain,
            elapsed_ms=elapsed_ms,
        )

    def _clear_processing_activity(self) -> None:
        """Clear 处理 state after popup, withdraw, or interrupt."""
        was_processing = bool(self._awaiting_tts_ready or self._processing_message_id)
        self._awaiting_tts_ready = False
        message_id = self._processing_message_id
        self._processing_message_id = ""
        self._processing_plain = ""
        self._processing_started_at = 0.0
        self._processing_last_log_at = 0.0
        if self._processing_timer.isActive():
            self._processing_timer.stop()
        if was_processing:
            self._report_activity_to_workers("idle", message_id=message_id)

    def _evict_tts_cache_if_needed(self, *, keep: str = "") -> None:
        """Drop oldest ready cache entries when over the in-memory cap."""
        while len(self._tts_cache) >= self._tts_cache_max_entries:
            victim: Optional[str] = None
            for mid, entry in self._tts_cache.items():
                if mid == keep:
                    continue
                if self.current_message is not None and self.current_message.message_id == mid:
                    continue
                if entry.ready.is_set():
                    victim = mid
                    break
            if victim is None:
                # Prefer cancelling a non-current in-flight prefetch over growing unbounded.
                for mid, entry in self._tts_cache.items():
                    if mid == keep:
                        continue
                    if self.current_message is not None and self.current_message.message_id == mid:
                        continue
                    victim = mid
                    break
            if victim is None:
                break
            self._drop_tts_cache(victim)

    def _drop_tts_cache(self, message_id: str) -> None:
        entry = self._tts_cache.pop(message_id, None)
        if entry is not None:
            entry.cancel.set()
            entry.plan = None
        worker = self._tts_prefetch_workers.pop(message_id, None)
        if worker is not None:
            worker.stop()
            if worker.isRunning():
                worker.wait(500)

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
        self._clear_processing_activity()
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
        # TTS messages: wait for Piper prefetch before showing the popup so users
        # do not see a silent window while synthesis is still running.
        if message.tts_enabled:
            self._start_tts_prefetch(message)
            cache_entry = self._tts_cache.get(message.message_id)
            if cache_entry is not None and not cache_entry.ready.is_set():
                self._begin_processing_activity(message)
                return
        self._present_current_message()

    def _present_current_message(self) -> None:
        """Show the current message popup and start TTS/timer (after any TTS wait)."""
        message = self.current_message
        if message is None or message.message_id in self.withdrawn_ids:
            self._clear_processing_activity()
            self._play_next()
            return
        self._clear_processing_activity()
        # Resolve display defaults from the source server first (multi-server safe).
        source_key: Optional[tuple[str, int]] = None
        if message.source_host and message.source_tcp_port:
            source_key = (str(message.source_host), int(message.source_tcp_port))
        server_defaults = self._server_display_defaults.get(source_key) if source_key else None
        base_position = server_defaults[0] if server_defaults else self.display_position
        base_ratio = server_defaults[1] if server_defaults else self.display_duration_ratio
        base_speech_rate = (
            server_defaults[2]
            if server_defaults and len(server_defaults) >= 3
            else self.speech_rate
        )
        # as_default carries deferred default config when "立即更新" is false:
        # apply it as this server's defaults before resolving this message.
        if message.as_default:
            if message.default_display_position:
                base_position = message.default_display_position
            if message.default_display_duration_ratio is not None:
                base_ratio = message.default_display_duration_ratio
            if message.default_speech_rate is not None:
                base_speech_rate = float(message.default_speech_rate)
            if source_key is not None:
                self._server_display_defaults[source_key] = (
                    base_position,
                    float(base_ratio),
                    float(base_speech_rate),
                )
        speech_rate = self._resolve_message_speech_rate(message)
        sent_time = self._format_sent_time(message.created_at_ms)
        self.display.setWindowTitle("{} | lovemilk class broadcaster".format(sent_time))
        # Display keeps sanitized HTML; TTS uses tag-stripped plain text.
        self.display_content.setText(message.content)
        plain_for_tts = html_to_plain_text(message.content)
        self.display_time.setText(sent_time)
        # Per-message font_size overrides device default for this playback only.
        self._apply_message_font(size_px=self._effective_message_font_size(message.font_size))
        position = (
            base_position if message.as_default else (message.display_position or base_position)
        )
        _play_notification_sound()
        self._show_display(position)
        LOGGER.info(
            "message playback started message_id={} tts_requested={} speech_nodes={} speech_rate={}",
            message.message_id,
            message.tts_enabled,
            len(message.speech),
            speech_rate,
        )
        cache_entry = self._tts_cache.get(message.message_id) if message.tts_enabled else None
        try:
            if message.tts_enabled:
                # Prefer parts compiled at prefetch time so cache matches playback.
                if cache_entry is not None and cache_entry.parts:
                    parts = list(cache_entry.parts)
                    speech_rate = float(cache_entry.speech_rate)
                else:
                    parts = self._compile_message_speech_parts(message)
            else:
                parts = []
        except (TypeError, ValueError) as exc:
            parts = []
            self.current_speech_done = True
            self.current_error = "invalid_speech: {}".format(exc)
            LOGGER.error(
                "TTS speech payload invalid message_id={} error={}", message.message_id, exc
            )
        display_ratio = (
            base_ratio
            if message.as_default
            else (
                base_ratio
                if message.display_duration_ratio is None
                else message.display_duration_ratio
            )
        )
        self.display_close_button.setVisible(display_ratio == 0)
        if display_ratio == 0:
            self.display_timer.stop()
            # Manual close: marquee uses comfortable default speed with end holds.
            self.display_content.set_display_duration_ms(0)
        elif message.tts_enabled and parts:
            # Prefer measured WAV duration when prefetch already finished.
            if (
                cache_entry is not None
                and cache_entry.ready.is_set()
                and cache_entry.plan
                and not cache_entry.error
            ):
                from .core.tts import _wav_duration_seconds

                wav_ms = int(
                    sum(
                        _wav_duration_seconds(payload) * 1000.0
                        for kind, payload in cache_entry.plan
                        if kind == "wav" and isinstance(payload, (bytes, bytearray))
                    )
                    + sum(
                        float(payload)
                        for kind, payload in cache_entry.plan
                        if kind == "pause"
                    )
                )
                total_ms = min(120_000, max(1000, wav_ms + 5000))
            else:
                total_ms = min(120_000, estimate_duration_ms(parts, speech_rate) + 5000)
            self.display_timer.start(total_ms)
            self.display_content.set_display_duration_ms(total_ms)
        else:
            # Duration tokens use plain text so HTML tags do not inflate counts.
            configured_duration_ms = display_duration_ms(plain_for_tts or message.content, display_ratio)
            total_ms = min(120_000, max(1000, configured_duration_ms))
            self.display_timer.start(total_ms)
            # Drive marquee so one full pass (+ ≥0.5s holds) fits this window.
            self.display_content.set_display_duration_ms(total_ms)
        if message.tts_enabled and parts:
            # Ensure prefetch is running if receive-time start was skipped/missed.
            if cache_entry is None:
                self._start_tts_prefetch(message)
                cache_entry = self._tts_cache.get(message.message_id)
            self.current_speech = SpeechWorker(
                message.message_id,
                parts,
                self.data_dir,
                speech_rate=speech_rate,
                cache_entry=cache_entry,
            )
            self.current_speech.finished_playback.connect(self._speech_finished)
            # Do not connect text_part: speech text is sanitized (symbols like
            # <= stripped) while the popup must keep message.content (HTML) verbatim.
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

    def _speech_finished(self, message_id: str, success: bool, error: str) -> None:
        if self.current_message is None or self.current_message.message_id != message_id:
            return
        if not success and error == "cancelled":
            return
        self.current_speech_done = True
        self.current_speech_success = success
        self.current_error = error
        self._finish_current_if_ready()

    def _refresh_runtime_diagnostics(self) -> None:
        """Run / re-run the Windows TTS runtime probe on a background thread."""
        if platform.system() != "Windows":
            if hasattr(self, "runtime_summary_label"):
                self.runtime_summary_label.setText("非 Windows：无需 VC++ / piper-tts 检测。")
                self.runtime_detail_label.setText("")
                self.runtime_vc_download_button.setEnabled(False)
            return
        worker = getattr(self, "_runtime_probe_worker", None)
        if worker is not None and worker.isRunning():
            return
        if hasattr(self, "runtime_summary_label"):
            self.runtime_summary_label.setText("正在检测运行环境…")
            self.runtime_refresh_button.setEnabled(False)
        # Allow a fresh import probe when the user clicks 重新检测.
        try:
            from .core.tts import clear_subprocess_import_cache

            clear_subprocess_import_cache("piper", "onnxruntime")
        except Exception:
            pass
        worker = RuntimeProbeWorker(self.data_dir, self)
        self._runtime_probe_worker = worker
        worker.finished_probe.connect(self._on_runtime_probe_finished, QueuedConnection)
        worker.start()

    def _on_runtime_probe_finished(self, report: object) -> None:
        payload = report if isinstance(report, dict) else {}
        self._runtime_report = payload
        if hasattr(self, "runtime_refresh_button"):
            self.runtime_refresh_button.setEnabled(True)
        self._apply_runtime_diagnostics(payload)
        # One-time automatic prompt at startup (persisted under data/).
        if (
            platform.system() == "Windows"
            and payload.get("needs_vc_redist")
            and not payload.get("prompt_dismissed")
            and not getattr(self, "_vc_redist_auto_prompt_done", False)
        ):
            self._vc_redist_auto_prompt_done = True
            QTimer.singleShot(
                0,
                lambda: self._offer_vc_redist_download(
                    str(payload.get("summary") or payload.get("piper_detail") or ""),
                    automatic=True,
                ),
            )

    def _apply_runtime_diagnostics(self, report: dict[str, Any]) -> None:
        if not hasattr(self, "runtime_summary_label"):
            return
        summary = str(report.get("summary") or "—")
        self.runtime_summary_label.setText(summary)
        lines: list[str] = []
        if report.get("applicable"):
            lines.append(
                "piper-tts：{}".format("可用" if report.get("piper_ok") else "不可用")
            )
            if report.get("piper_detail") and not report.get("piper_ok"):
                lines.append("  {}".format(report.get("piper_detail")))
            lines.append(
                "onnxruntime：{}".format("可用" if report.get("onnxruntime_ok") else "不可用")
            )
            if report.get("onnxruntime_detail") and not report.get("onnxruntime_ok"):
                lines.append("  {}".format(report.get("onnxruntime_detail")))
            lines.append(
                "语音模型：{}".format(
                    report.get("model_path") or ("未找到" if not report.get("model_found") else "—")
                )
            )
            crt = report.get("crt_files") or {}
            if isinstance(crt, dict) and crt:
                lines.append("CRT 文件：")
                for name, info in crt.items():
                    if not isinstance(info, dict):
                        continue
                    if info.get("present"):
                        flag = "（过旧）" if info.get("outdated") else ""
                        lines.append("  {} v{}{}".format(name, info.get("version") or "?", flag))
                    else:
                        lines.append("  {}：不在包内（将使用系统运行库）".format(name))
            if report.get("prompt_dismissed"):
                lines.append("自动提示：已关闭（本机不再自动弹窗；可点下方按钮手动下载）")
        self.runtime_detail_label.setText("\n".join(lines))
        needs = bool(report.get("needs_vc_redist")) or not bool(report.get("piper_ok"))
        self.runtime_vc_download_button.setEnabled(platform.system() == "Windows")
        if needs and platform.system() == "Windows":
            self.runtime_vc_download_button.setText("打开 VC++ 运行库下载（建议）")
        else:
            self.runtime_vc_download_button.setText("打开 VC++ 运行库下载")

    def _open_vc_redist_download(self) -> None:
        """Settings-page action: always open the official VC++ x64 download."""
        self._offer_vc_redist_download(
            str((self._runtime_report or {}).get("summary") or ""),
            automatic=False,
        )

    def _offer_vc_redist_download(self, reason: str, *, automatic: bool = False) -> None:
        """Offer the VC++ 2015–2022 x64 redistributable download.

        automatic=True: startup one-shot; dismissed permanently after Yes or No.
        automatic=False: settings button; always opens (or confirms) without changing
        the dismissal flag unless the user confirms.
        """
        if platform.system() != "Windows":
            return
        if automatic and is_vc_redist_prompt_dismissed(self.data_dir):
            return
        detail = (reason or "").strip()
        if len(detail) > 280:
            detail = detail[:277] + "…"
        if automatic:
            message = (
                "高质量朗读（piper-tts）无法加载原生组件（常见原因：未安装或过旧的 "
                "Microsoft Visual C++ 2015–2022 x64 运行库）。\n\n"
                "是否打开微软官方下载页？\n"
                "（未安装前将回退到系统朗读 SAPI；安装完成后请重启客户端。"
                "此提示本机只弹出一次，之后可在「设置 → 运行环境」再次打开。）"
            )
            if detail:
                message += "\n\n诊断：{}".format(detail)
            answer = QMessageBox.warning(
                self,
                "需要安装 VC++ 运行库",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            opened = False
            if answer == QMessageBox.StandardButton.Yes:
                opened = self._launch_vc_redist_url()
            mark_vc_redist_prompt_dismissed(self.data_dir, opened_download=opened)
            # Refresh settings page label if visible.
            if self._runtime_report:
                self._runtime_report["prompt_dismissed"] = True
                self._apply_runtime_diagnostics(self._runtime_report)
            return
        # Manual: open immediately (settings already explained why).
        self._launch_vc_redist_url()

    def _launch_vc_redist_url(self) -> bool:
        try:
            opened = bool(QDesktopServices.openUrl(QUrl(VC_REDIST_X64_URL)))
        except Exception as exc:
            LOGGER.warning("open VC++ redistributable URL failed error={}", exc)
            opened = False
        if not opened:
            QMessageBox.information(
                self,
                "请手动下载",
                "无法自动打开浏览器。请访问：\n{}".format(VC_REDIST_X64_URL),
            )
        return opened

    def _finish_current_if_ready(self) -> None:
        message = self.current_message
        if message is None or not self.current_displayed or not self.current_speech_done:
            return
        finished_id = message.message_id
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
        # Consumed after display+TTS finish. Preempt path uses _stop_current_speech
        # (keeps cache) and never reaches here for the interrupted message.
        self._drop_tts_cache(finished_id)
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
        self._drop_tts_cache(message.message_id)
        self.current_displayed = True
        self._finish_current_if_ready()

    def _stop_current_speech(self) -> None:
        self.display_timer.stop()
        self._clear_processing_activity()
        if self.current_speech is not None and self.current_speech.isRunning():
            self.current_speech.stop()
            self.current_speech.wait(1500)
        self.current_speech = None
        self.display.hide()
        # Keep TTS cache for the interrupted message — it may be re-queued.
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
    def _format_withdrawal_text(withdrawal: IncomingWithdrawal) -> tuple[str, str]:
        """Build withdrawal notice for display + TTS.

        Returns ``(display_html_or_plain, tts_plain)``.
        Display keeps sanitized original HTML after an escaped title prefix;
        TTS always uses plain text.
        """
        import html as html_lib

        title = (withdrawal.message or "消息已撤回").strip() or "消息已撤回"
        raw_body = withdrawal.content or ""
        plain_body = html_to_plain_text(raw_body).replace("\r", " ").replace("\n", " ").strip()
        tts_plain = "{}：{}".format(title, plain_body) if plain_body else title
        if looks_like_html(raw_body):
            body_html = sanitize_html(raw_body)
            if body_html.strip():
                display = "{}：{}".format(html_lib.escape(title), body_html)
                return display, tts_plain
        if plain_body:
            return "{}：{}".format(title, plain_body), tts_plain
        return title, tts_plain

    def _message_withdrawn(self, endpoint_key: object, withdrawal: IncomingWithdrawal) -> None:
        self.withdrawn_ids.add(withdrawal.message_id)
        self.pending_messages.withdraw(withdrawal.message_id)
        self._drop_tts_cache(withdrawal.message_id)
        # Resolve display ratio from the withdrawn message when it is on screen
        # (or waiting for TTS before show); otherwise use payload / global default.
        ratio = self.display_duration_ratio
        if (
            self.current_message is not None
            and self.current_message.message_id == withdrawal.message_id
        ):
            message = self.current_message
            if message.as_default or message.display_duration_ratio is None:
                source_key = None
                if message.source_host and message.source_tcp_port:
                    source_key = (str(message.source_host), int(message.source_tcp_port))
                server_defaults = (
                    self._server_display_defaults.get(source_key) if source_key else None
                )
                ratio = server_defaults[1] if server_defaults else self.display_duration_ratio
            else:
                ratio = message.display_duration_ratio
            # Clears _awaiting_tts_ready when the message was deferred pending synth.
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
        display_text, tts_plain = self._format_withdrawal_text(withdrawal)
        self._show_withdrawal_notice(
            display_text,
            tts_plain,
            float(ratio) if ratio is not None else self.display_duration_ratio,
        )
        self._ack(withdrawal, "withdrawn")

    def _show_withdrawal_notice(self, display_text: str, tts_plain: str, ratio: float) -> None:
        """Show withdrawal notice with marquee/rich + TTS; ratio 0 keeps a manual close button."""
        self._stop_withdrawal_speech()
        self.withdrawal_notice_active = True
        self.withdrawal_displayed = False
        self.withdrawal_speech_done = False
        shown = str(display_text or "消息已撤回").strip() or "消息已撤回"
        spoken = str(tts_plain or "消息已撤回").replace("\r", " ").replace("\n", " ").strip() or "消息已撤回"
        self.display_content.setAlignment(AlignCenter)
        # Keep original formatting (b/i/u/br/img) on the notice even when inline-only.
        self.display_content.setText(shown, force_rich=looks_like_html(shown))
        self.display.adjustSize()
        self.display_close_button.setVisible(ratio <= 0)
        _play_notification_sound()
        self._show_display("center")

        parts = [SpeechPart("text", text=spoken)]
        speech_rate = normalize_speech_rate(self.speech_rate, default=DEFAULT_SPEECH_RATE)
        if ratio <= 0:
            self.withdrawal_timer.stop()
            self.display_content.set_display_duration_ms(0)
            # Manual close: display is "done" for finish gating; speech may still run.
            self.withdrawal_displayed = True
        else:
            # Withdrawal always speaks; window = TTS estimate + 5s (cap 120s).
            duration = min(120_000, estimate_duration_ms(parts, speech_rate) + 5000)
            self.withdrawal_timer.start(duration)
            self.display_content.set_display_duration_ms(duration)

        LOGGER.info(
            "withdrawal notice shown tts=True chars={} ratio={} speech_rate={} manual_close={}",
            len(spoken),
            ratio,
            speech_rate,
            ratio <= 0,
        )
        self.withdrawal_speech = SpeechWorker(
            self._withdrawal_speech_id, parts, self.data_dir, speech_rate=speech_rate
        )
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
        if getattr(self, "_shutting_down", False):
            return
        try:
            self._refresh_autostart_ui()
        except Exception:
            pass
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowState(self.windowState() & ~WindowMinimized | WindowActive)
        self.raise_()
        self.activateWindow()
        # Windows sometimes needs a brief topmost pulse to bring a hidden window forward.
        if sys.platform == "win32":
            self.setWindowFlag(WindowStaysOnTopHint, True)
            self.show()
            self.setWindowFlag(WindowStaysOnTopHint, False)
            self.show()
            self.raise_()
            self.activateWindow()
        LOGGER.info("settings window shown")

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()

    def quit_application(self, *, detail: str = "developer_exit") -> None:
        """Controlled exit: report user_exit then stop (人为终止 on server).

        Used by the developer panel and by SIGINT/SIGTERM during local ``uv run``.
        """
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True
        LOGGER.info("controlled shutdown requested reason=user_exit detail={}", detail)
        if getattr(self, "tray", None) is not None:
            try:
                self.tray.hide()
            except Exception:
                pass
        try:
            self._report_session_end_all("user_exit", detail)
        except Exception:
            LOGGER.exception("session-end report during quit failed")
        try:
            self.shutdown()
        except Exception:
            LOGGER.exception("shutdown during quit failed")
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def _queue_update_applied_report_all(
        self, digest: str, version: str = "", seq: int = 0
    ) -> None:
        """Queue UpdateApplied on every live ConnectionWorker (multi-server safe)."""
        digest = str(digest or "").strip().lower()
        if len(digest) != 64:
            return
        for worker in list(self.connection_workers.values()):
            if not worker.isRunning():
                continue
            try:
                worker.queue_update_applied_report(digest, version, seq)
            except Exception:
                pass

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
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self._shutting_down = True
        try:
            self.display_timer.stop()
        except Exception:
            pass
        for message_id in list(self._tts_cache.keys()):
            self._drop_tts_cache(message_id)
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
    """Point Qt at bundled PySide2/PySide6 plugins so the GUI can start.

    Windows packaged builds keep plugins under Lib/site-packages/PySide6/plugins.
    Linux/Debian 11 uses system or venv PySide2. Without QT_QPA_PLATFORM_PLUGIN_PATH,
    QApplication may start headless or fail silently (especially under pythonw.exe).
    """
    if os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH"):
        return
    candidates: list[Path] = []
    install_dir = os.environ.get("MKCB_INSTALL_DIR")
    binding = qt_binding() or "PySide6"
    package_names = (
        (binding, "PySide2", "PySide6") if binding == "PySide2" else (binding, "PySide6", "PySide2")
    )
    if install_dir:
        for name in package_names:
            root = Path(install_dir) / "Lib" / "site-packages" / name / "plugins"
            candidates.append(root)
            candidates.append(root / "platforms")
    # Source / editable / venv installs — prefer the binding already loaded by qtcompat.
    for module_name in package_names:
        try:
            module = __import__(module_name)
            package_dir = Path(module.__file__).resolve().parent
            candidates.append(package_dir / "plugins")
            candidates.append(package_dir / "plugins" / "platforms")
        except Exception:
            continue
    for path in candidates:
        is_platforms = path.name == "platforms" and path.is_dir()
        is_plugins = path.is_dir() and (path / "platforms").is_dir()
        if not (is_platforms or is_plugins):
            continue
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


def _install_posix_signal_quit(app: QApplication, window: "ClientWindow") -> None:
    """Make Ctrl+C / SIGTERM quit a tray app cleanly.

    Qt's event loop does not interrupt on SIGINT by itself. Install handlers that
    call ``quit_application``, plus a short QTimer so Python can run the handler
    while the GUI is idle (otherwise Ctrl+C only surfaces on the next Qt event,
    e.g. a tray click — which is what produced the KeyboardInterrupt traceback).
    """
    if not hasattr(signal, "SIGINT"):
        return

    def _request_quit(signum: int, _frame: object) -> None:
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        LOGGER.info("POSIX signal received; quitting signum={}", name)
        # Defer into the Qt event loop — signal handlers must stay short.
        QTimer.singleShot(0, lambda: window.quit_application(detail=f"signal:{name.lower()}"))

    try:
        signal.signal(signal.SIGINT, _request_quit)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _request_quit)
    except (ValueError, OSError) as exc:
        # Signals can only be set from the main thread; ignore in odd hosts.
        LOGGER.warning("could not install SIGINT/SIGTERM handlers error={}", exc)
        return
    # Wake the interpreter periodically so pending signal handlers run.
    wakeup = QTimer(app)
    wakeup.setInterval(200)
    wakeup.timeout.connect(lambda: None)
    wakeup.start()
    app._mkcb_signal_wakeup_timer = wakeup  # type: ignore[attr-defined]


def main() -> None:
    # Linux classrooms: prefer X11 so nine-grid popup positioning works.
    configure_platform_for_popup()
    # 在创建主窗口前取得安装目录锁，避免两个进程同时消费同一状态文件。
    _configure_qt_plugin_path()
    # Windows: register onnxruntime / openjtalk DLL dirs early (same set as launcher PATH).
    try:
        from .core.tts import ensure_windows_native_dll_dirs

        ensure_windows_native_dll_dirs()
    except Exception:
        pass
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
    window._shutting_down = False
    window._shutdown_done = False
    # Open settings when there is no data/state or no usable saved server.
    # Closing the window only hides it to the tray; it does not exit.
    if (
        (not had_existing_state)
        or window.needs_setup_ui()
        or not QSystemTrayIcon.isSystemTrayAvailable()
    ):
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
    _install_posix_signal_quit(app, window)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
