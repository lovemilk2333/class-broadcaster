"""Whitelist HTML sanitizer for message content (display) and plain-text for TTS.

Allowed tags: ``b`` / ``i`` / ``u`` / ``span`` (font-family|font-size|color) /
``br`` / ``img`` (data URI ``src`` only) / ``svg`` (inline, no script/on*).
Banned: ``script`` / ``iframe`` / external links / event handlers.
"""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from typing import Optional

# Void / self-closing in our subset.
_VOID_TAGS = frozenset({"br", "img"})

# Tags that force static rich-text display (no marquee token scroll).
_STATIC_RICH_TAGS = frozenset({"img", "svg", "br"})

# SVG child tags commonly needed for simple inline icons/shapes.
_SVG_CHILD_TAGS = frozenset(
    {
        "svg",
        "g",
        "path",
        "circle",
        "rect",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "defs",
        "use",
        "title",
        "desc",
        "lineargradient",
        "radialgradient",
        "stop",
        "clippath",
        "mask",
    }
)

_ALLOWED_TAGS = frozenset({"b", "i", "u", "span", "br", "img"}) | _SVG_CHILD_TAGS

# Attribute names allowed on SVG elements (lowercase). No href/xlink:href to external.
_SVG_SAFE_ATTRS = frozenset(
    {
        "viewbox",
        "xmlns",
        "width",
        "height",
        "fill",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "d",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "points",
        "transform",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "font-size",
        "font-family",
        "text-anchor",
        "dominant-baseline",
        "gradientunits",
        "gradienttransform",
        "offset",
        "stop-color",
        "stop-opacity",
        "clip-path",
        "mask",
        "id",
        "class",
    }
)

# font-weight / font-style: browsers often emit <span style="font-weight: bold">
# instead of <b>/<i> from execCommand; keep them so compose preview and clients match.
_SPAN_STYLE_PROPS = frozenset({"font-family", "font-size", "color", "font-weight", "font-style"})
_DATA_IMAGE_RE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,", re.IGNORECASE)
_ON_ATTR_RE = re.compile(r"^on", re.IGNORECASE)


# Require a tag-like ``<name...>`` so plain ``a < b`` stays plain text.
_TAG_HINT_RE = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")


def looks_like_html(content: str) -> bool:
    """True when ``content`` likely contains markup (not bare ``<`` comparisons)."""
    return bool(_TAG_HINT_RE.search(content or ""))


def needs_static_rich_display(sanitized: str) -> bool:
    """True when marquee should be disabled in favour of QLabel rich text.

    Block-ish markup (``br`` / ``img`` / ``svg`` / real newlines) always forces
    static layout. Inline emphasis (``b`` / ``i`` / ``u`` / styled ``span``) also
    uses rich text so bold/italic remain visible — especially for CJK, where the
    plain marquee painter stripped tags and synthetic faces never appeared.
    Color/size-only spans without emphasis still take the marquee path.
    """
    if not sanitized:
        return False
    if "\n" in sanitized:
        return True
    lower = sanitized.lower()
    for tag in _STATIC_RICH_TAGS:
        if f"<{tag}" in lower or f"</{tag}" in lower:
            return True
    for tag in ("b", "i", "u", "strong", "em"):
        if f"<{tag}" in lower or f"</{tag}" in lower:
            return True
    if "font-weight:" in lower or "font-style:" in lower:
        return True
    return False


def _escape_attr(value: str) -> str:
    return (
        html_lib.escape(value, quote=True)
        .replace("`", "&#96;")
        .replace("'", "&#39;")
    )


def _sanitize_span_style(style: str) -> str:
    """Keep font-family / font-size / color / font-weight / font-style declarations."""
    kept: list[str] = []
    for part in (style or "").split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, value = part.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name not in _SPAN_STYLE_PROPS or not value:
            continue
        # Reject urls / expressions in style values.
        lowered = value.lower()
        if "url(" in lowered or "expression(" in lowered or "javascript:" in lowered:
            continue
        if name == "font-weight":
            # Normalize browser quirks; reject junk.
            if not re.fullmatch(
                r"(?:normal|bold|bolder|lighter|[1-9]00)",
                lowered,
            ):
                continue
        if name == "font-style":
            if lowered not in ("normal", "italic", "oblique"):
                continue
        kept.append(f"{name}: {html_lib.escape(value, quote=False)}")
    return "; ".join(kept)


def _img_src_allowed(src: str) -> bool:
    raw = (src or "").strip()
    if not raw:
        return False
    # data:image/...;base64,... only — no http(s), no file:, no svg+xml with script risk via remote.
    return bool(_DATA_IMAGE_RE.match(raw)) and "," in raw


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._svg_depth = 0
        self._skip_depth = 0  # inside script/iframe/etc.: drop content

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if self._skip_depth:
            if name in ("script", "iframe", "object", "embed", "link", "meta", "style"):
                self._skip_depth += 1
            return
        # Drop dangerous containers (and their contents). ``<a>`` is stripped as a
        # tag but child text is kept so "click <a>here</a>" → "click here".
        if name in ("script", "iframe", "object", "embed", "link", "meta", "style"):
            self._skip_depth += 1
            return
        if name == "a":
            return
        if name not in _ALLOWED_TAGS:
            return
        if name == "svg" or self._svg_depth:
            self._emit_svg_start(name, attrs)
            return
        if name == "br":
            self._out.append("<br />")
            return
        if name == "img":
            self._emit_img(attrs)
            return
        if name == "span":
            self._emit_span(attrs)
            return
        if name in ("b", "i", "u"):
            self._out.append(f"<{name}>")
            return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if self._skip_depth:
            return
        if name in ("script", "iframe", "object", "embed", "link", "meta", "style", "a"):
            return
        if name == "br":
            self._out.append("<br />")
            return
        if name == "img":
            self._emit_img(attrs)
            return
        if name in _SVG_CHILD_TAGS and (name == "svg" or self._svg_depth):
            # Self-closing SVG leaf (e.g. <path ... />).
            self._emit_svg_start(name, attrs, self_closing=True)
            return

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if self._skip_depth:
            if name in ("script", "iframe", "object", "embed", "link", "meta", "style"):
                self._skip_depth = max(0, self._skip_depth - 1)
            return
        if name == "a":
            return
        if name == "svg" or (self._svg_depth and name in _SVG_CHILD_TAGS):
            if self._svg_depth:
                self._out.append(f"</{name}>")
                if name == "svg":
                    self._svg_depth = max(0, self._svg_depth - 1)
            return
        if name in ("b", "i", "u", "span"):
            self._out.append(f"</{name}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data:
            self._out.append(html_lib.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth:
            return
        self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth:
            return
        self._out.append(f"&#{name};")

    def _emit_img(self, attrs: list[tuple[str, Optional[str]]]) -> None:
        src = ""
        alt = ""
        width = ""
        height = ""
        for key, value in attrs:
            key_l = (key or "").lower()
            if _ON_ATTR_RE.match(key_l):
                continue
            if key_l == "src" and value:
                src = value.strip()
            elif key_l == "alt" and value is not None:
                alt = value
            elif key_l == "width" and value and re.fullmatch(r"\d{1,4}", value.strip()):
                width = value.strip()
            elif key_l == "height" and value and re.fullmatch(r"\d{1,4}", value.strip()):
                height = value.strip()
        if not _img_src_allowed(src):
            return
        parts = [f'src="{_escape_attr(src)}"', f'alt="{_escape_attr(alt)}"']
        if width:
            parts.append(f'width="{width}"')
        if height:
            parts.append(f'height="{height}"')
        self._out.append(f'<img {" ".join(parts)} />')

    def _emit_span(self, attrs: list[tuple[str, Optional[str]]]) -> None:
        style = ""
        # Also accept discrete font-family / size / color attributes if present.
        pieces: list[str] = []
        for key, value in attrs:
            key_l = (key or "").lower()
            if _ON_ATTR_RE.match(key_l) or value is None:
                continue
            if key_l == "style":
                style = _sanitize_span_style(value)
            elif key_l == "font-family":
                pieces.append(f"font-family: {html_lib.escape(value.strip(), quote=False)}")
            elif key_l in ("font-size", "size"):
                pieces.append(f"font-size: {html_lib.escape(value.strip(), quote=False)}")
            elif key_l == "color":
                pieces.append(f"color: {html_lib.escape(value.strip(), quote=False)}")
        if pieces:
            extra = "; ".join(pieces)
            style = f"{style}; {extra}" if style else extra
        if style:
            self._out.append(f'<span style="{_escape_attr(style)}">')
        else:
            self._out.append("<span>")

    def _emit_svg_start(
        self,
        name: str,
        attrs: list[tuple[str, Optional[str]]],
        *,
        self_closing: bool = False,
    ) -> None:
        if name == "svg":
            self._svg_depth += 1
        elif self._svg_depth <= 0:
            return
        safe: list[str] = []
        for key, value in attrs:
            key_l = (key or "").lower()
            if not key_l or _ON_ATTR_RE.match(key_l):
                continue
            if key_l in ("href", "xlink:href", "src"):
                # Ban external / script URLs inside SVG.
                continue
            if key_l not in _SVG_SAFE_ATTRS and not key_l.startswith("data-"):
                continue
            if value is None:
                continue
            lowered = value.lower().strip()
            if "javascript:" in lowered or "data:text/html" in lowered:
                continue
            safe.append(f'{key_l}="{_escape_attr(value)}"')
        attr_s = (" " + " ".join(safe)) if safe else ""
        if self_closing or name in ("path", "circle", "rect", "ellipse", "line", "polyline", "polygon", "stop", "use"):
            self._out.append(f"<{name}{attr_s} />")
            if name == "svg" and self_closing:
                self._svg_depth = max(0, self._svg_depth - 1)
        else:
            self._out.append(f"<{name}{attr_s}>")

    def result(self) -> str:
        return "".join(self._out)


def sanitize_html(content: str) -> str:
    """Return whitelist-sanitized HTML suitable for QLabel rich text.

    Non-HTML (no ``<``) is returned escaped so literal ``<`` in plain text
    cannot become markup. When ``content`` contains ``<``, it is parsed and
    only allowed tags/attrs are kept.
    """
    raw = str(content or "")
    if not raw:
        return ""
    if not looks_like_html(raw):
        return html_lib.escape(raw, quote=False)
    parser = _Sanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # Malformed markup → treat as plain text.
        return html_lib.escape(raw, quote=False)
    return parser.result()


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if self._skip_depth:
            if name in ("script", "iframe", "style"):
                self._skip_depth += 1
            return
        if name in ("script", "iframe", "style"):
            self._skip_depth += 1
            return
        if name == "br":
            self._parts.append("\n")
        elif name == "img":
            alt = ""
            for key, value in attrs:
                if (key or "").lower() == "alt" and value:
                    alt = value
                    break
            if alt:
                self._parts.append(alt)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if self._skip_depth and name in ("script", "iframe", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._parts.append(data)

    def result(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


def html_to_plain_text(content: str) -> str:
    """Strip tags and decode entities for TTS / duration tokenisation."""
    raw = str(content or "")
    if not raw:
        return ""
    if not looks_like_html(raw):
        return html_lib.unescape(raw)
    parser = _PlainTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # Fallback: crude tag strip.
        stripped = re.sub(r"<[^>]+>", " ", raw)
        return html_lib.unescape(stripped).strip()
    return parser.result()


# Captures data:image/...;base64,... inside img src (single or double quoted).
_IMG_DATA_URI_RE = re.compile(
    r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+)\2',
    re.IGNORECASE | re.DOTALL,
)


def extract_data_image_uris(sanitized_html: str) -> list[str]:
    """Return unique data-URI ``src`` values from sanitized ``<img>`` tags (order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _IMG_DATA_URI_RE.finditer(sanitized_html or ""):
        uri = re.sub(r"\s+", "", match.group(3))
        if uri not in seen and _img_src_allowed(uri):
            seen.add(uri)
            out.append(uri)
    return out


def rewrite_data_images_for_qt(
    sanitized_html: str, *, name_prefix: str = "mkcbimg"
) -> tuple[str, list[tuple[str, bytes]]]:
    """Replace data-URI ``img src`` with ``mkcb://{name_prefix}/{n}`` resource URLs.

    QLabel / QTextDocument on some Qt builds fail to decode long ``data:image/...;base64``
    URLs. Callers should register the returned ``(name, raw_bytes)`` pairs via
    ``QTextDocument.addResource(ImageResource, QUrl(name), QPixmap)`` (or
    ``setResourceProvider``) before ``setText``.

    Returns ``(rewritten_html, [(resource_url, image_bytes), ...])``.
    """
    import base64

    resources: list[tuple[str, bytes]] = []
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        prefix, quote, uri = match.group(1), match.group(2), match.group(3)
        uri_compact = re.sub(r"\s+", "", uri)
        if not _img_src_allowed(uri_compact):
            return match.group(0)
        try:
            header, _, payload = uri_compact.partition(",")
            raw = base64.b64decode(payload, validate=False)
        except Exception:
            return match.group(0)
        if not raw:
            return match.group(0)
        name = f"mkcb://{name_prefix}/{counter}"
        counter += 1
        resources.append((name, raw))
        return f"{prefix}{quote}{name}{quote}"

    rewritten = _IMG_DATA_URI_RE.sub(_replace, sanitized_html or "")
    return rewritten, resources
