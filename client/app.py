"""PySide6 tray application for configuring and connecting the Windows client."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import queue
import platform
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from loguru import logger

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "client"

from PySide6.QtCore import QThread, QTimer, Qt, Signal, QPoint
from PySide6.QtGui import QAction, QCloseEvent, QPainter
from PySide6.QtWidgets import (
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
    QStyle,
    QMenu,
    QVBoxLayout,
    QWidget,
)
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from .version import VERSION_STR
from .core.connection import (
    CONFIG_UPDATE,
    MESSAGE,
    PING,
    PONG,
    SERVER_SHUTDOWN,
    Connection,
    ClientNotApprovedError,
    IncomingMessage,
    IncomingWithdrawal,
    decode_message,
    decode_withdrawal,
    connect,
    probe_server_fingerprint,
)
from .core.delivery import ConfigSnapshot, MessageDeduplicator, PriorityQueue, ReconnectBackoff
from .core.discovery import DiscoveryResponse, discover
from .core.identity import decrypt_private_key, encrypt_private_key, generate_identity
from .core.protocol.bson import decode
from .core.protocol.packet import Packet
from .core.store import StateStore
from .core.tts import (
    EnginePreference,
    PiperEngine,
    SAPIEngine,
    compile_speech,
    estimate_duration_ms,
    speak_with_fallback,
)


LOGGER = logger.bind(name="mkcb.client")


def _data_dir() -> Path:
    # Packaged builds keep state beside the installed executable. In a source
    # checkout this resolves to client/data, matching the install-directory policy.
    base = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    path = base / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(extra={"name": "mkcb.client"})
    logger.add(
        data_dir / "client.log",
        rotation="2 MB",
        retention=3,
        encoding="utf-8",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss,SSS} {level} {extra[name]}: {message}",
        backtrace=False,
        diagnose=False,
    )


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
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        windowed = executable.with_name("pythonw.exe")
        if windowed.exists():
            executable = windowed
    return '"{}" "{}"'.format(executable, Path(__file__).resolve())


def _autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MKCBClient")
            return value == _startup_command()
    except FileNotFoundError:
        return False


def _set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        raise OSError("开机自启动仅支持 Windows")
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        if enabled:
            winreg.SetValueEx(key, "MKCBClient", 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, "MKCBClient")
            except FileNotFoundError:
                pass


class ScanWorker(QThread):
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, destination: str) -> None:
        super().__init__()
        self.destination = destination

    def run(self) -> None:
        try:
            targets: list[tuple[str, int]] = []
            broadcast = False
            for item in (part.strip() for part in self.destination.split(",")):
                if not item:
                    continue
                if "/" in item:
                    network = ipaddress.ip_network(item, strict=False)
                    if network.version != 4 or network.num_addresses > 4096:
                        raise ValueError("IPv4 scan range must contain at most 4096 addresses")
                    targets.extend((str(host), 39001) for host in network.hosts())
                else:
                    address = ipaddress.ip_address(item)
                    if address.version != 4:
                        raise ValueError("scan address must be IPv4")
                    targets.append((str(address), 39001))
                    broadcast = broadcast or str(address) == "255.255.255.255"
            if not targets:
                raise ValueError("scan address is empty")
            results = discover(targets, timeout=2.0, broadcast=broadcast)
            self.found.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class ConnectionWorker(QThread):
    status = Signal(str)
    connection_state = Signal(object, bool)
    received = Signal(object)
    withdrawn = Signal(object)
    config_updated = Signal(object)

    def __init__(
        self,
        endpoint: dict[str, Any],
        private_key: Ed25519PrivateKey,
        certificate: bytes,
        state_store: StateStore,
    ) -> None:
        super().__init__()
        self.endpoint = endpoint
        self.private_key = private_key
        self.certificate = certificate
        self.state_store = state_store
        self.keep_running = True
        self.acks: queue.Queue = queue.Queue()
        self.dedup = MessageDeduplicator()
        self.completed_acks: dict[str, tuple[str, Optional[str]]] = {}
        self._active_connection: Optional[Connection] = None
        fingerprint = endpoint.get("fingerprint", b"")
        if isinstance(fingerprint, str):
            try:
                fingerprint = base64.b64decode(fingerprint)
            except (ValueError, TypeError):
                fingerprint = b""
        self.server_fingerprint = bytes(fingerprint)

    def acknowledge(self, message_id: str, status: str, error: Optional[str] = None) -> None:
        self.acks.put((message_id, status, error))

    def stop(self) -> None:
        self.keep_running = False
        if self._active_connection is not None:
            self._active_connection.close()

    def run(self) -> None:
        backoff = ReconnectBackoff()
        approval_deadline: Optional[float] = None
        while self.keep_running:
            connection: Optional[Connection] = None
            normal_shutdown = False
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
                    connection, response = connect(
                        self.endpoint["host"],
                        self.endpoint["tcp_port"],
                        str(cert_path),
                        str(key_path),
                        self.endpoint["fingerprint"],
                        config_id=self.state_store.config_id(),
                        timeout=3,
                    )
                actual_fingerprint = response.get("_server_fingerprint") if isinstance(response, dict) else None
                if isinstance(actual_fingerprint, bytes) and len(actual_fingerprint) == 32:
                    if not any(self.server_fingerprint) or self.server_fingerprint != actual_fingerprint:
                        self.server_fingerprint = actual_fingerprint
                        self.endpoint["fingerprint"] = actual_fingerprint
                        self.state_store.set_server(
                            host=str(self.endpoint["host"]),
                            tcp_port=int(self.endpoint["tcp_port"]),
                            http_port=int(self.endpoint.get("http_port", 39001)),
                            fingerprint=actual_fingerprint,
                        )
                self._active_connection = connection
                approval_deadline = None
                endpoint_key = (str(self.endpoint["host"]), int(self.endpoint["tcp_port"]))
                self.connection_state.emit(endpoint_key, True)
                config = response.get("config")
                if isinstance(config, dict):
                    snapshot = ConfigSnapshot.from_wire(config)
                    self.state_store.set_config_id(snapshot.config_id)
                else:
                    # Servers may omit an unchanged snapshot. Keep conservative defaults
                    # until an explicit snapshot is received on this connection.
                    snapshot = ConfigSnapshot(
                        config_id=self.state_store.config_id() or "0" * 13 + ".0",
                        issued_at=int(time.time() * 1000),
                        heartbeat_interval=15,
                        heartbeat_timeout=45,
                        message_ttl=86400,
                        max_speech_depth=8,
                        max_repeat_expansion=100,
                        display_position="center",
                        display_duration_ratio=0.2,
                    )
                connection.socket.settimeout(0.25)
                backoff.reset()
                self.status.emit(
                    "已连接：{}（配置 {}）".format(self.endpoint["host"], snapshot.config_id)
                )
                self.config_updated.emit(snapshot)
                LOGGER.info(
                    "connection established {}:{} config_id={}",
                    self.endpoint["host"],
                    self.endpoint["tcp_port"],
                    snapshot.config_id,
                )
                last_ping = time.monotonic()
                last_pong = last_ping
                while self.keep_running:
                    self._flush_acks(connection)
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
                            self.received.emit(message)
                        elif message.message_id in self.completed_acks:
                            status, error = self.completed_acks[message.message_id]
                            connection.acknowledge(message.message_id, status, error)
                    elif packet.packet_type == 0x1003:
                        withdrawal = decode_withdrawal(packet)
                        self.withdrawn.emit(withdrawal)
                    elif packet.packet_type == CONFIG_UPDATE:
                        document = decode(packet.payload)
                        candidate = document.get("config", document)
                        if not isinstance(candidate, dict):
                            raise ValueError("配置更新格式错误")
                        updated = ConfigSnapshot.from_wire(candidate)
                        # Apply after full validation and persist the ID atomically.
                        snapshot = updated
                        self.state_store.set_config_id(snapshot.config_id)
                        self.config_updated.emit(snapshot)
                        self.status.emit("配置已更新：{}".format(snapshot.config_id))
                    elif packet.packet_type == PONG:
                        last_pong = time.monotonic()
                    elif packet.packet_type == SERVER_SHUTDOWN:
                        normal_shutdown = True
                        self.status.emit("服务端正在维护，5 分钟后检测")
                        raise ConnectionError("server_shutdown")
            except Exception as exc:
                if isinstance(exc, ClientNotApprovedError):
                    if approval_deadline is None:
                        approval_deadline = time.monotonic() + 180
                    remaining = approval_deadline - time.monotonic()
                    if remaining <= 0:
                        LOGGER.error("server authorization timed out after 180 seconds")
                        self.status.emit("服务端授权超时，已取消此服务端信任")
                        self.state_store.remove_server(
                            host=str(self.endpoint["host"]),
                            tcp_port=int(self.endpoint["tcp_port"]),
                        )
                        self.keep_running = False
                        break
                    LOGGER.warning("client is waiting for server approval; retrying in 15 seconds")
                    self.status.emit("等待服务端批准；每 15 秒重试，最多 3 分钟")
                    self._wait(min(15, remaining))
                    continue
                if self.keep_running:
                    LOGGER.exception(
                        "connection failed {}:{}",
                        self.endpoint.get("host"),
                        self.endpoint.get("tcp_port"),
                    )
                    delay = 300 if normal_shutdown else backoff.next_delay()
                    self.status.emit("连接中断：{}；{} 秒后重试".format(exc, delay))
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
                self.connection_state.emit(
                    (str(self.endpoint["host"]), int(self.endpoint["tcp_port"])), False
                )

    def _flush_acks(self, connection: Connection) -> None:
        while True:
            try:
                message_id, status, error = self.acks.get_nowait()
            except queue.Empty:
                return
            self.completed_acks[message_id] = (status, error)
            connection.acknowledge(message_id, status, error)

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self.keep_running and time.monotonic() < deadline:
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
    """超长单行消息使用轻量级像素偏移滚动，撤回多行文本仍保持普通居中。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source_text = ""
        self._offset = 0
        self._text_width = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._source_text = str(text)
        self._offset = 0
        QLabel.setText(self, self._source_text)
        self._update_marquee()
        self.update()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._update_marquee()

    def _update_marquee(self) -> None:
        self._text_width = self.fontMetrics().horizontalAdvance(self._source_text)
        if "\n" in self._source_text or self._text_width <= self.width():
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()

    def _advance(self) -> None:
        gap = max(32, self.fontMetrics().horizontalAdvance("    "))
        self._offset = (self._offset + 2) % max(1, self._text_width + gap)
        self.update()

    def paintEvent(self, event: Any) -> None:
        if "\n" in self._source_text or self._text_width <= self.width():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        baseline = (self.height() + self.fontMetrics().ascent() - self.fontMetrics().descent()) // 2
        gap = max(32, self.fontMetrics().horizontalAdvance("    "))
        cycle = self._text_width + gap
        x = -self._offset
        while x < self.width():
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
        self.data_dir = _data_dir()
        _configure_logging(self.data_dir)
        LOGGER.info("client started")
        self.state_store = StateStore(self.data_dir / "state.json")
        self.private_key, self.certificate = _identity_files(self.data_dir)
        self.servers: list[DiscoveryResponse] = []
        self.scan_worker: Optional[ScanWorker] = None
        self.connection_worker: Optional[ConnectionWorker] = None
        self._connection_attempts: set[tuple[str, int]] = set()
        self._connection_endpoint_key: Optional[tuple[str, int]] = None
        self._connected_endpoint_keys: set[tuple[str, int]] = set()
        self._stopping_workers: list[ConnectionWorker] = []
        self.dedup = MessageDeduplicator()
        self.pending_messages = PriorityQueue()
        self.current_message: Optional[IncomingMessage] = None
        self.current_speech: Optional[SpeechWorker] = None
        self.current_displayed = False
        self.current_speech_done = False
        self.current_speech_success = False
        self.current_error = ""
        self.display_position = "center"
        self.display_duration_ratio = 0.2
        self.withdrawn_ids: set[str] = set()
        self.setWindowTitle("lovemilk class broadcaster")
        self.resize(620, 520)
        self._build_ui()
        self._build_display()
        self._build_tray()
        self._load_saved_server()
        self.autostart.blockSignals(True)
        self.autostart.setChecked(_autostart_enabled())
        self.autostart.blockSignals(False)
        self.autostart_label.setEnabled(sys.platform == "win32")
        self.autostart.setEnabled(sys.platform == "win32")
        saved = self.state_store.server()
        if saved:
            self._start_connection(saved)

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
        self.destination.setPlaceholderText("例如 192.168.1.0/24、192.168.1.10 或 255.255.255.255")
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
        self.manual_host.returnPressed.connect(self.add_manual_server)
        self.manual_port.lineEdit().returnPressed.connect(self.add_manual_server)
        self.add_server_button = QPushButton("保存服务器")
        self.add_server_button.clicked.connect(self.add_manual_server)
        manual_row.addWidget(self.add_server_button)
        connection_group_layout.addWidget(QLabel("手动添加服务器"))
        connection_group_layout.addLayout(manual_row)
        row = QHBoxLayout()
        self.connect_button = QPushButton("连接所选服务端")
        self.connect_button.clicked.connect(self.connect_selected)
        row.addWidget(self.connect_button)
        self.remove_server_button = QPushButton("删除所选服务端")
        self.remove_server_button.clicked.connect(self.remove_selected_server)
        self.remove_server_button.setEnabled(False)
        row.addWidget(self.remove_server_button)
        connection_group_layout.addLayout(row)
        self.server_list = QListWidget()
        self.server_list.currentRowChanged.connect(self._show_fingerprint)
        self.server_list.currentRowChanged.connect(lambda _row: self._update_remove_button())
        self.server_list.currentRowChanged.connect(lambda _row: self._update_connect_button())
        connection_group_layout.addWidget(self.server_list, 1)
        self.fingerprint_label = QLabel(
            "选择服务端后显示其 SSH 风格公钥指纹。首次连接即信任所选公钥。"
        )
        self.fingerprint_label.setWordWrap(True)
        connection_group_layout.addWidget(self.fingerprint_label)
        self.status_label = QLabel("未连接")
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
        self.autostart = QCheckBox("登录 Windows 时自动启动")
        self.autostart.toggled.connect(self._toggle_autostart)
        self.autostart_label = QLabel(
            "登录 Windows 时自动启动取决于 Windows 自启动配置是否存在，若重启后重置请检查 Windows 系统或注册表是否正常工作。"
        )
        system_group_layout.addWidget(self.autostart)
        system_group_layout.addWidget(self.autostart_label)
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
        self.about_version_button = QPushButton(f"版本 {VERSION_STR} (复制)")
        # self.about_version_button.setToolTip("连续点击 7 次打开开发者面板")
        self.about_version_button.clicked.connect(self.about_version_clicked)
        about_group_layout.addWidget(self.about_version_button)
        about_group_layout.addStretch(1)
        about_layout.addWidget(about_group, 1)
        self.settings_pages.addWidget(about_page)

        self.settings_categories.setCurrentRow(0)
        self.setCentralWidget(root)

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
            clipboard.setText(VERSION_STR)

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
        self.display_time = QLabel()
        self.display_time.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.display_time.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(self.display_time)
        self.display_content = MarqueeLabel()
        self.display_content.setAlignment(Qt.AlignCenter)
        self.display_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.display_content.setStyleSheet("font-size: 38px; font-weight: 600;")
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
        self.withdrawal_timer.timeout.connect(self._finish_withdrawal_notice)

    def _config_updated(self, snapshot: ConfigSnapshot) -> None:
        self.display_position = snapshot.display_position
        self.display_duration_ratio = snapshot.display_duration_ratio
        if self.display.isVisible():
            self._position_display()

    def _position_display(self, position: Optional[str] = None) -> None:
        screen = self.display.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        margin = 24
        self.display.adjustSize()
        width = min(self.display.width(), max(1, area.width() - margin * 2))
        height = min(self.display.height(), max(1, area.height() - margin * 2))
        if (width, height) != (self.display.width(), self.display.height()):
            self.display.resize(width, height)
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
        if self.connection_worker is not None and self.connection_worker.isRunning():
            return
        if any(worker.isRunning() for worker in self._stopping_workers):
            return
        self.connection_worker = ConnectionWorker(
            endpoint, self.private_key, self.certificate, self.state_store
        )
        LOGGER.info("starting connection {}:{}", endpoint.get("host"), endpoint.get("tcp_port"))
        self._connection_endpoint_key = (str(endpoint["host"]), int(endpoint["tcp_port"]))
        self._connection_attempts.add(self._connection_endpoint_key)
        self._update_remove_button()
        self.connection_worker.status.connect(self._connection_status_changed)
        self.connection_worker.connection_state.connect(self._connection_state_changed)
        self.connection_worker.config_updated.connect(self._config_updated)
        self.connection_worker.received.connect(self._message_received)
        self.connection_worker.withdrawn.connect(self._message_withdrawn)
        self.connection_worker.start()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        menu = QMenu()
        show_action = QAction("设置", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
            )
        )
        self.tray.show()

    def _load_saved_server(self) -> None:
        self.servers = []
        self.server_list.clear()
        for saved in self.state_store.servers():
            base = "{}:{}（已保存）".format(saved["host"], saved["tcp_port"])
            endpoint_key = self._endpoint_key(saved)
            state = "已连接" if endpoint_key in self._connected_endpoint_keys else (
                "连接中" if endpoint_key in self._connection_attempts else "未连接"
            )
            item = QListWidgetItem(base + "  [{}]".format(state))
            item.setData(Qt.UserRole, saved)
            item.setData(Qt.UserRole + 1, base)
            self.server_list.addItem(item)
        if self.server_list.count():
            self.server_list.setCurrentRow(0)

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
        self.servers = servers
        scanned_keys = {self._endpoint_key(server) for server in servers}
        self._connection_attempts.intersection_update(scanned_keys | set(self._connected_endpoint_keys))
        self.server_list.clear()
        for server in servers:
            item = "{}  {}:{}".format(server.service_name, server.address[0], server.tcp_port)
            list_item = QListWidgetItem(item)
            list_item.setData(Qt.UserRole, server)
            list_item.setData(Qt.UserRole + 1, item)
            list_item.setText(item + "  [未连接]")
            self.server_list.addItem(list_item)
        self.status_label.setText("找到 {} 个服务端".format(len(servers)))
        if servers:
            self.server_list.setCurrentRow(0)

    def _scan_failed(self, message: str) -> None:
        self.status_label.setText("扫描失败：{}".format(message))

    def remove_selected_server(self) -> None:
        item = self.server_list.currentItem()
        if item is None:
            return
        value = item.data(Qt.UserRole)
        endpoint_key = self._endpoint_key(value)
        if endpoint_key is None:
            self._update_remove_button()
            return
        if self.connection_worker is not None and self._connection_endpoint_key == endpoint_key:
            worker = self.connection_worker
            worker.stop()
            self._stopping_workers.append(worker)
            QTimer.singleShot(0, lambda worker=worker: self._finish_worker_stop(worker))
            self.connection_worker = None
            self._connection_endpoint_key = None
        self._connection_attempts.discard(endpoint_key)
        self._connected_endpoint_keys.discard(endpoint_key)
        if endpoint_key is not None:
            self.state_store.remove_server(host=endpoint_key[0], tcp_port=endpoint_key[1])
        self.server_list.takeItem(self.server_list.row(item))
        self.status_label.setText("已删除服务端配置")
        self._update_remove_button()
        self._update_connect_button()

    def _finish_worker_stop(self, worker: ConnectionWorker) -> None:
        if worker.isRunning():
            QTimer.singleShot(50, lambda: self._finish_worker_stop(worker))
            return
        if worker in self._stopping_workers:
            self._stopping_workers.remove(worker)
        self._update_connect_button()

    def add_manual_server(self) -> None:
        host = self.manual_host.text().strip()
        if not host:
            # 两个输入框都准备好前，Enter 只忽略，不弹出打断配置流程的提示。
            return
        port = int(self.manual_port.value())
        existing = next(
            (item for item in self.state_store.servers()
             if str(item.get("host")) == host and int(item.get("tcp_port", 0)) == port),
            None,
        )
        if existing is None:
            self.state_store.set_server(
                host=host, tcp_port=port, http_port=39001, fingerprint=b"\x00" * 32
            )
        self._load_saved_server()
        for row in range(self.server_list.count()):
            if self._endpoint_key(self.server_list.item(row).data(Qt.UserRole)) == (host, port):
                self.server_list.setCurrentRow(row)
                break
        self.status_label.setText("已保存服务器，请选择后连接")

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
        item = self.server_list.currentItem()
        self.remove_server_button.setEnabled(
            item is not None
            and self._endpoint_key(item.data(Qt.UserRole)) in self._connection_attempts
        )

    def _connection_status_changed(self, status: str) -> None:
        self.status_label.setText(status)
        key = self._connection_endpoint_key
        if key is None:
            return
        for row in range(self.server_list.count()):
            item = self.server_list.item(row)
            if self._endpoint_key(item.data(Qt.UserRole)) == key:
                base = item.data(Qt.UserRole + 1) or item.text()
                item.setText("{}  [{}]".format(base, status))
                break
        self._update_connect_button()

    def _connection_state_changed(self, endpoint_key: tuple[str, int], connected: bool) -> None:
        if connected:
            self._connected_endpoint_keys.add(endpoint_key)
        else:
            self._connected_endpoint_keys.discard(endpoint_key)
        self._update_connect_button()

    def _update_connect_button(self) -> None:
        item = self.server_list.currentItem()
        selected_key = self._endpoint_key(item.data(Qt.UserRole)) if item is not None else None
        self.connect_button.setEnabled(
            selected_key is not None and selected_key not in self._connected_endpoint_keys
        )

    def _show_fingerprint(self, row: int) -> None:
        if row < 0 or row >= self.server_list.count():
            return
        server = self.server_list.item(row).data(Qt.UserRole)
        fingerprint = (
            server.fingerprint
            if isinstance(server, DiscoveryResponse)
            else base64.b64decode(server["fingerprint"])
        )
        if not fingerprint or not any(fingerprint):
            self.fingerprint_label.setText("服务端公钥 SHA-256：\n尚未获取，连接时将先确认服务端证书。")
            return
        self.fingerprint_label.setText(
            "服务端公钥 SHA-256：\n{}".format(
                "SHA256:" + base64.b64encode(fingerprint).decode("ascii").rstrip("=")
            )
        )

    def connect_selected(self) -> None:
        item = self.server_list.currentItem()
        if item is None:
            QMessageBox.information(self, "选择服务端", "请先扫描并选择一个服务端。")
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
        endpoint_key = (str(host), int(tcp_port))
        if endpoint_key in self._connected_endpoint_keys:
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
        if self.connection_worker is not None and self.connection_worker.isRunning():
            self.status_label.setText("正在重新连接…")
            self.connection_worker.stop()
            self.connection_worker.wait(5000)
        self.state_store.set_server(
            host=host, tcp_port=tcp_port, http_port=http_port, fingerprint=fingerprint
        )
        self._start_connection(
            {
                "host": host,
                "tcp_port": tcp_port,
                "http_port": http_port,
                "fingerprint": fingerprint,
            }
        )

    def _message_received(self, message: IncomingMessage) -> None:
        saved = self.state_store.server()
        server_id = base64.b64decode(saved["fingerprint"]) if saved else b""
        if not self.dedup.observe(server_id, message.message_id):
            self._ack(message.message_id, "received")
            return
        self.pending_messages.put(message)
        if self.current_message is None:
            self._play_next()
        elif message.priority < self.current_message.priority:
            self.pending_messages.put(self.current_message)
            self._stop_current_speech()
            self._play_next()

    def _format_sent_time(self, milliseconds: Optional[int]) -> str:
        if milliseconds is None:
            return "发送时间未知"
        value = datetime.fromtimestamp(milliseconds / 1000, timezone(timedelta(hours=8)))
        return value.strftime("%Y-%m-%d %H:%M:%S+08:00")

    def _play_next(self) -> None:
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
        if message.as_default:
            if message.default_display_position:
                self.display_position = message.default_display_position
            if message.default_display_duration_ratio is not None:
                self.display_duration_ratio = message.default_display_duration_ratio
        sent_time = self._format_sent_time(message.created_at_ms)
        self.display.setWindowTitle("{} | lovemilk class broadcaster".format(sent_time))
        self.display_content.setText(message.content)
        self.display_time.setText(sent_time)
        position = message.display_position or self.display_position
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
        display_ratio = (
            self.display_duration_ratio
            if message.display_duration_ratio is None
            else message.display_duration_ratio
        )
        self.display_close_button.setVisible(display_ratio == 0)
        if display_ratio == 0:
            self.display_timer.stop()
        else:
            configured_duration_ms = self._text_duration_ms(message.content, display_ratio)
            self.display_timer.start(max(minimum_duration_ms, configured_duration_ms))
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

    @staticmethod
    def _text_duration_ms(text: str, ratio: float) -> int:
        """按 ASCII/标点 1 单位、中文 1.5 单位计算默认显示时长。"""
        if ratio <= 0:
            return 0
        units = 0.0
        for character in text:
            codepoint = ord(character)
            is_cjk = (
                0x3400 <= codepoint <= 0x4DBF
                or 0x4E00 <= codepoint <= 0x9FFF
                or 0xF900 <= codepoint <= 0xFAFF
            )
            units += 1.5 if is_cjk else 1.0
        return max(1000, int(units * ratio * 1000))

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
                message.message_id,
                "spoken" if self.current_speech_success else "failed",
                None if self.current_speech_success else (self.current_error or "tts_unavailable"),
            )
        else:
            self._ack(message.message_id, "displayed")
        self.display.hide()
        self.display_close_button.hide()
        self.current_message = None
        self.current_speech = None
        self._play_next()

    def _dismiss_current(self) -> None:
        if self.withdrawal_timer.isActive():
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

    def _ack(self, message_id: str, status: str, error: Optional[str] = None) -> None:
        if self.connection_worker is not None:
            self.connection_worker.acknowledge(message_id, status, error)

    def _message_withdrawn(self, withdrawal: IncomingWithdrawal) -> None:
        self.withdrawn_ids.add(withdrawal.message_id)
        self.pending_messages.withdraw(withdrawal.message_id)
        if self.current_message and self.current_message.message_id == withdrawal.message_id:
            self._stop_current_speech()
            withdrawn_text = withdrawal.message or "消息已撤回"
            if withdrawal.content:
                withdrawn_text = "{}\n{}".format(withdrawn_text, withdrawal.content)
            self.display_content.setAlignment(Qt.AlignCenter)
            self.display_content.setText(withdrawn_text)
            self.display.adjustSize()
            self.display_close_button.hide()
            self._show_display("center")
            self.withdrawal_timer.start(4000)
        else:
            # A withdrawal is a business message too: show its original content
            # in the same non-dismissible window instead of only a tray toast.
            sent_time = self._format_sent_time(int(time.time() * 1000))
            self.display.setWindowTitle("{} | lovemilk class broadcaster".format(sent_time))
            self.display_time.setText(sent_time)
            withdrawn_text = withdrawal.message or "消息已撤回"
            if withdrawal.content:
                withdrawn_text = "{}\n{}".format(withdrawn_text, withdrawal.content)
            self.display_content.setAlignment(Qt.AlignCenter)
            self.display_content.setText(withdrawn_text)
            self.display.adjustSize()
            self.display_close_button.hide()
            self._show_display("center")
            self.withdrawal_timer.start(4000)
        self._ack(withdrawal.message_id, "withdrawn")

    def _finish_withdrawal_notice(self) -> None:
        self.display.hide()
        self._play_next()

    def _toggle_autostart(self, enabled: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            _set_autostart(enabled)
            # Reflect the actual OS state instead of persisting a UI preference.
            self.autostart.blockSignals(True)
            self.autostart.setChecked(_autostart_enabled())
            self.autostart.blockSignals(False)
        except OSError as exc:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(_autostart_enabled())
            self.autostart.blockSignals(False)
            QMessageBox.warning(self, "启动项设置失败", str(exc))

    def show_window(self) -> None:
        self.autostart.setChecked(_autostart_enabled())
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()

    def quit_application(self) -> None:
        LOGGER.info("controlled shutdown requested")
        self.tray.hide()
        self.shutdown()
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def shutdown(self) -> None:
        self.display_timer.stop()
        self._stop_current_speech()
        if self.connection_worker is not None and self.connection_worker.isRunning():
            self.connection_worker.stop()
            self.connection_worker.wait(5000)
        for worker in list(self._stopping_workers):
            if worker.isRunning():
                worker.stop()
                worker.wait(5000)
        self._stopping_workers.clear()


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("lovemilk class broadcaster")
    try:
        window = ClientWindow()
    except Exception as exc:
        QMessageBox.critical(None, "客户端启动失败", str(exc))
        return
    if not window.state_store.server():
        window.show()
    app.aboutToQuit.connect(window.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
