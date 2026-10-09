"""Parse admin font-size expressions into pixel sizes for Qt rendering.

Supported (case-insensitive unit):
  "" / "0" / "(默认)"     → built-in default ``25vh`` (fallback px when no screen)
  "38" / "38px"          → absolute pixels (admin range 16–120)
  "1.2em"                → relative to ``EM_BASE_PX`` (38px; 1em = 38px)
  "5vw" / "8vh" / "25vh" → percent of the active screen width / height (0–100)

Admin validation ranges differ from the final render clamp: vw/vh may resolve
above 120px (e.g. 25vh on 1080p ≈ 270px), so rendering allows up to RENDER_MAX_PX.
"""

from __future__ import annotations

import re
from typing import Optional

# Built-in default when device/message size is empty.
DEFAULT_EXPR = "25vh"
# Used when screen metrics are unavailable, and as the 1em reference.
EM_BASE_PX = 38
DEFAULT_FALLBACK_PX = 38

# Admin-accepted absolute px range (expressions like "52px").
MIN_PX = 16
MAX_PX = 120

# Final Qt pixel clamp after vw/vh/em resolution (must fit large viewport sizes).
RENDER_MIN_PX = 16
RENDER_MAX_PX = 800

# Back-compat aliases used by older imports / StateStore helpers.
DEFAULT_PX = DEFAULT_FALLBACK_PX

_EXPR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(px|em|vw|vh)?\s*$", re.IGNORECASE)


def clamp_px(value: object) -> int:
    """Clamp a resolved pixel size for Qt rendering."""
    try:
        size = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_FALLBACK_PX
    return max(RENDER_MIN_PX, min(RENDER_MAX_PX, size))


def normalize_expr(raw: object) -> str:
    """Return a canonical expression or empty for default. Raises ValueError if invalid."""
    text = str(raw or "").strip()
    if not text or text == "0" or text in ("(默认)", "(default)", "default"):
        return ""
    # Legacy bare integer from older admin UI / ConnectResp.
    if text.isdigit():
        value = int(text)
        if value == 0:
            return ""
        if value < MIN_PX or value > MAX_PX:
            raise ValueError("px font size out of range")
        return f"{value}px"
    match = _EXPR.match(text)
    if not match:
        raise ValueError("invalid font size expression")
    value = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    if value == 0:
        return ""
    if unit == "px":
        if value < MIN_PX or value > MAX_PX:
            raise ValueError("px font size out of range")
        if value == int(value):
            return f"{int(value)}px"
        return f"{value:g}px"
    if unit == "em":
        if value < 0.25 or value > 4:
            raise ValueError("em font size out of range")
        return f"{value:g}em"
    if unit in ("vw", "vh"):
        if value < 0.0 or value > 100.0:
            raise ValueError(f"{unit} font size out of range")
        return f"{value:g}{unit}"
    raise ValueError("unsupported font size unit")


def resolve_px(
    expr: object,
    *,
    screen_width: int = 0,
    screen_height: int = 0,
    default_px: Optional[int] = None,
) -> int:
    """Resolve an expression to clamped pixels.

    Empty / invalid → built-in ``25vh`` when screen height is known, otherwise
    ``default_px`` / ``DEFAULT_FALLBACK_PX``.
    """
    width = max(0, int(screen_width or 0))
    height = max(0, int(screen_height or 0))
    fallback = (
        clamp_px(default_px)
        if default_px is not None
        else (
            clamp_px(height * 25.0 / 100.0)
            if height > 0
            else DEFAULT_FALLBACK_PX
        )
    )
    try:
        canonical = normalize_expr(expr)
    except ValueError:
        return fallback
    if not canonical:
        # Built-in default: 25vh (or fallback px without screen metrics).
        if height > 0:
            return clamp_px(height * 25.0 / 100.0)
        return fallback
    match = _EXPR.match(canonical)
    if not match:
        return fallback
    value = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    if unit == "px":
        return clamp_px(value)
    if unit == "em":
        return clamp_px(EM_BASE_PX * value)
    if unit == "vw":
        if width <= 0:
            return fallback
        return clamp_px(width * value / 100.0)
    if unit == "vh":
        if height <= 0:
            return fallback
        return clamp_px(height * value / 100.0)
    return fallback


def legacy_int_to_expr(value: object) -> str:
    """Convert old integer ConnectResp / PATCH values to an expression."""
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    # Keep legacy ints in the admin px band when possible.
    bounded = max(MIN_PX, min(MAX_PX, size))
    return f"{bounded}px"
