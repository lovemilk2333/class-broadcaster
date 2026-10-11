"""Qt binding compatibility for PySide2 (Debian 11 / Linux) and PySide6 (Windows).

Prefer PySide2 when available (Python 3.10 + system/Qt5 on Debian 11), else PySide6.
Normalizes a small set of Qt6-only enum/module differences so ``app.py`` can stay shared.
"""

from __future__ import annotations

import os
import sys
from typing import Any

_BINDING = ""
_QT_ERROR: Exception | None = None

try:
    from PySide2.QtCore import QPoint, Qt, QThread, QTimer, QUrl, Signal  # type: ignore
    from PySide2.QtGui import (  # type: ignore
        QColor,
        QCloseEvent,
        QDesktopServices,
        QFont,
        QFontDatabase,
        QIcon,
        QPainter,
        QPixmap,
        QTextDocument,
    )
    from PySide2.QtWidgets import (  # type: ignore
        QAbstractItemView,
        QAction,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSizePolicy,
        QSpinBox,
        QStackedWidget,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )

    _BINDING = "PySide2"
except Exception as exc:  # noqa: BLE001 — try PySide6 next
    _QT_ERROR = exc
    try:
        from PySide6.QtCore import QPoint, Qt, QThread, QTimer, QUrl, Signal  # type: ignore
        from PySide6.QtGui import (  # type: ignore
            QAction,
            QColor,
            QCloseEvent,
            QDesktopServices,
            QFont,
            QFontDatabase,
            QIcon,
            QPainter,
            QPixmap,
            QTextDocument,
        )
        from PySide6.QtWidgets import (  # type: ignore
            QAbstractItemView,
            QApplication,
            QCheckBox,
            QComboBox,
            QDialog,
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMenu,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QSizePolicy,
            QSpinBox,
            QStackedWidget,
            QSystemTrayIcon,
            QVBoxLayout,
            QWidget,
        )

        _BINDING = "PySide6"
        _QT_ERROR = None
    except Exception as exc2:  # noqa: BLE001
        _QT_ERROR = exc2
        raise ImportError(
            "Neither PySide2 nor PySide6 is available. "
            "Linux/Debian 11: install PySide2 (Python 3.10). "
            "Windows packaged client: PySide6-Essentials."
        ) from exc2


def qt_binding() -> str:
    return _BINDING


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


# Qt5 flat enums vs Qt6 nested enum namespaces.
AlignCenter = _attr(Qt, "AlignCenter", default=_attr(_attr(Qt, "AlignmentFlag", default=Qt), "AlignCenter"))
AlignVCenter = _attr(Qt, "AlignVCenter", default=_attr(_attr(Qt, "AlignmentFlag", default=Qt), "AlignVCenter"))
AlignLeft = _attr(Qt, "AlignLeft", default=_attr(_attr(Qt, "AlignmentFlag", default=Qt), "AlignLeft"))
AlignRight = _attr(Qt, "AlignRight", default=_attr(_attr(Qt, "AlignmentFlag", default=Qt), "AlignRight"))
Key_Escape = _attr(Qt, "Key_Escape", default=_attr(_attr(Qt, "Key", default=Qt), "Key_Escape"))
ScrollBarAlwaysOff = _attr(
    Qt, "ScrollBarAlwaysOff", default=_attr(_attr(Qt, "ScrollBarPolicy", default=Qt), "ScrollBarAlwaysOff")
)
UserRole = _attr(Qt, "UserRole", default=_attr(_attr(Qt, "ItemDataRole", default=Qt), "UserRole"))
Window = _attr(Qt, "Window", default=_attr(_attr(Qt, "WindowType", default=Qt), "Window"))
FramelessWindowHint = _attr(
    Qt, "FramelessWindowHint", default=_attr(_attr(Qt, "WindowType", default=Qt), "FramelessWindowHint")
)
WindowStaysOnTopHint = _attr(
    Qt, "WindowStaysOnTopHint", default=_attr(_attr(Qt, "WindowType", default=Qt), "WindowStaysOnTopHint")
)
Tool = _attr(Qt, "Tool", default=_attr(_attr(Qt, "WindowType", default=Qt), "Tool"))
QueuedConnection = _attr(
    Qt, "QueuedConnection", default=_attr(_attr(Qt, "ConnectionType", default=Qt), "QueuedConnection")
)
TextSelectableByMouse = _attr(
    Qt,
    "TextSelectableByMouse",
    default=_attr(_attr(Qt, "TextInteractionFlag", default=Qt), "TextSelectableByMouse"),
)
RichText = _attr(Qt, "RichText", default=_attr(_attr(Qt, "TextFormat", default=Qt), "RichText"))
PlainText = _attr(Qt, "PlainText", default=_attr(_attr(Qt, "TextFormat", default=Qt), "PlainText"))
AutoText = _attr(Qt, "AutoText", default=_attr(_attr(Qt, "TextFormat", default=Qt), "AutoText"))
WindowMinimized = _attr(
    Qt, "WindowMinimized", default=_attr(_attr(Qt, "WindowState", default=Qt), "WindowMinimized")
)
WindowActive = _attr(
    Qt, "WindowActive", default=_attr(_attr(Qt, "WindowState", default=Qt), "WindowActive")
)


def configure_platform_for_popup() -> None:
    """Prefer X11 (xcb) on Linux so move()/always-on-top work for message popups.

    Wayland compositors often ignore client-side global positioning. Classroom
    Debian 11 boards should run an X11 session (or XWayland via QT_QPA_PLATFORM=xcb).

    Override: set ``MKCB_ALLOW_WAYLAND=1`` to keep the existing QT_QPA_PLATFORM
    (e.g. ``wayland;xcb`` from the desktop session).
    """
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get("MKCB_ALLOW_WAYLAND", "").strip() in ("1", "true", "yes"):
        return
    # Force xcb so nine-grid positioning works even when the DE exports wayland;xcb.
    os.environ["QT_QPA_PLATFORM"] = "xcb"


def default_message_font_family() -> str:
    """Platform default when admin/client selects「(默认)」."""
    if sys.platform == "win32":
        return "Microsoft YaHei"
    # Debian/classroom Linux: Noto Sans CJK when present, else generic sans.
    # QFontDatabase requires a QGuiApplication; fall back safely before main().
    families: set[str] = set()
    try:
        if QApplication.instance() is not None:
            families = set(QFontDatabase().families())
    except Exception:
        families = set()
    for candidate in (
        "Noto Sans CJK SC",
        "Noto Sans CJK",
        "Noto Sans SC",
        "Noto Sans",
        "WenQuanYi Micro Hei",
        "Source Han Sans SC",
        "DejaVu Sans",
    ):
        if candidate in families:
            return candidate
    try:
        if QApplication.instance() is not None:
            return QFont().defaultFamily() or "sans-serif"
    except Exception:
        pass
    return "sans-serif"


def list_system_font_families(*, limit: int = 300) -> list[str]:
    """Return sorted unique UI font families for handshake / settings combo."""
    try:
        if QApplication.instance() is None:
            return []
        db = QFontDatabase()
        families = [str(name).strip() for name in db.families() if str(name).strip()]
    except Exception:
        families = []
    # Prefer scalable / non-private faces; keep stable order.
    unique: list[str] = []
    seen: set[str] = set()
    for name in sorted(families, key=lambda item: item.casefold()):
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
        if len(unique) >= max(1, int(limit)):
            break
    return unique


def resolve_message_font_family(preferred: str | None) -> str:
    """Pick an installed family; fall back to platform default when missing."""
    preferred = (preferred or "").strip()
    if not preferred or preferred in ("(默认)", "(default)", "default"):
        return default_message_font_family()
    try:
        families = {name.casefold(): name for name in QFontDatabase().families()}
    except Exception:
        families = {}
    match = families.get(preferred.casefold())
    if match:
        return match
    return default_message_font_family()


def text_width(metrics: Any, text: str) -> int:
    """Qt5 ``QFontMetrics.width`` vs Qt6 ``horizontalAdvance``."""
    if hasattr(metrics, "horizontalAdvance"):
        return int(metrics.horizontalAdvance(text))
    if hasattr(metrics, "width"):
        return int(metrics.width(text))
    return 0


__all__ = [
    "AlignCenter",
    "AlignLeft",
    "AlignRight",
    "AlignVCenter",
    "FramelessWindowHint",
    "Key_Escape",
    "QAbstractItemView",
    "QAction",
    "QApplication",
    "QCheckBox",
    "QCloseEvent",
    "QColor",
    "QComboBox",
    "QDesktopServices",
    "QDialog",
    "QFont",
    "QFontDatabase",
    "QFormLayout",
    "QGroupBox",
    "QHBoxLayout",
    "QIcon",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QMainWindow",
    "QMenu",
    "QMessageBox",
    "QPainter",
    "QPixmap",
    "QPlainTextEdit",
    "QPoint",
    "QPushButton",
    "QSizePolicy",
    "QSpinBox",
    "QStackedWidget",
    "QSystemTrayIcon",
    "QTextDocument",
    "Qt",
    "QThread",
    "QTimer",
    "QUrl",
    "QueuedConnection",
    "QVBoxLayout",
    "QWidget",
    "RichText",
    "PlainText",
    "AutoText",
    "ScrollBarAlwaysOff",
    "Signal",
    "TextSelectableByMouse",
    "Tool",
    "UserRole",
    "Window",
    "WindowActive",
    "WindowMinimized",
    "WindowStaysOnTopHint",
    "configure_platform_for_popup",
    "default_message_font_family",
    "list_system_font_families",
    "qt_binding",
    "resolve_message_font_family",
    "text_width",
]
