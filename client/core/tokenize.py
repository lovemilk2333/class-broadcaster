"""jieba-based tokenization for display duration and marquee pacing.

Each non-empty token counts as 1.0 unit toward ``display_duration_ms``.
Whitespace-only jieba pieces are dropped so duration stays stable across
punctuation-adjacent splits.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import List

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@lru_cache(maxsize=1)
def _jieba():
    try:
        import jieba  # type: ignore[import-not-found]

        # Silence jieba's default stderr logging noise in classroom builds.
        try:
            jieba.setLogLevel(20)  # INFO
        except Exception:
            pass
        return jieba
    except Exception:
        return None


def tokenize(text: str) -> List[str]:
    """Split ``text`` into display/TTS tokens.

    Prefer jieba when installed; otherwise fall back to a lightweight rule that
    keeps CJK runs and ASCII words together. Letter/digit runs are always split
    afterward so ``test123`` becomes ``test`` + ``123`` for TTS routing.
    """
    value = str(text or "")
    if not value:
        return []
    jieba = _jieba()
    if jieba is not None:
        try:
            parts = [str(part) for part in jieba.lcut(value, cut_all=False) if str(part).strip()]
            if parts:
                return _split_alnum_runs(parts)
        except Exception:
            pass
    return _fallback_tokenize(value)


_LETTER_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")


def _split_alnum_runs(parts: List[str]) -> List[str]:
    """Split glued letter+digit tokens (``test123`` → ``test``, ``123``).

    Needed for Piper: bare digits go to the zh voice (一二三); digits glued to
    Latin letters would otherwise take the en path and expand via g2p/inflect.
    """
    out: List[str] = []
    for part in parts:
        if not part:
            continue
        if not (_LETTER_RE.search(part) and _DIGIT_RE.search(part)):
            out.append(part)
            continue
        buf: List[str] = []
        mode = ""  # "" | "letter" | "digit" | "other"

        def flush() -> None:
            nonlocal buf, mode
            if buf:
                out.append("".join(buf))
                buf = []
            mode = ""

        for ch in part:
            if ch.isalpha():
                if mode != "letter":
                    flush()
                    mode = "letter"
                buf.append(ch)
            elif ch.isdigit():
                if mode != "digit":
                    flush()
                    mode = "digit"
                buf.append(ch)
            else:
                if mode != "other":
                    flush()
                    mode = "other"
                buf.append(ch)
        flush()
    return out


def _fallback_tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    buf: List[str] = []
    mode = ""  # "" | "cjk" | "letter" | "digit" | "ascii"

    def flush() -> None:
        nonlocal buf, mode
        if buf:
            tokens.append("".join(buf))
            buf = []
        mode = ""

    for ch in text:
        if ch.isspace():
            flush()
            continue
        if _CJK_RE.fullmatch(ch):
            if mode != "cjk":
                flush()
                mode = "cjk"
            buf.append(ch)
            continue
        if ch.isalpha() or ch in {"'", "-"}:
            if mode != "letter":
                flush()
                mode = "letter"
            buf.append(ch)
            continue
        if ch.isdigit():
            if mode != "digit":
                flush()
                mode = "digit"
            buf.append(ch)
            continue
        flush()
        tokens.append(ch)
    flush()
    return tokens


def token_units(text: str) -> float:
    """Each non-empty token contributes 1.0 unit."""
    return float(len(tokenize(text)))


def language_of_token(token: str) -> str:
    """Classify a token as ``zh``, ``en``, or ``other`` for Piper voice routing.

    Pure digit runs are ``zh`` so Piper reads them as Chinese digits (一二三)
    instead of English number expansion via the en g2p path.
    """
    value = str(token or "").strip()
    if not value:
        return "other"
    if _CJK_RE.search(value):
        return "zh"
    if value.isdigit():
        return "zh"
    if re.search(r"[A-Za-z]", value):
        return "en"
    return "other"


def split_language_segments(text: str) -> List[tuple[str, str]]:
    """Group consecutive same-language tokens into ``(lang, text)`` segments.

    Used by Piper TTS so zh-CN / en-US voices receive contiguous runs rather
    than one model call per token. Digit-only tokens are zh and must not attach
    onto a preceding English run (``test123`` → en ``test`` + zh ``123``).
    """
    segments: List[tuple[str, str]] = []
    for token in tokenize(text):
        lang = language_of_token(token)
        if lang == "other":
            # Attach punctuation / symbols to the previous segment when possible.
            if segments:
                prev_lang, prev_text = segments[-1]
                segments[-1] = (prev_lang, prev_text + token)
            else:
                segments.append(("zh", token))
            continue
        if segments and segments[-1][0] == lang:
            prev_lang, prev_text = segments[-1]
            # Re-insert a space between English words that jieba split apart.
            # Digits already classified as zh join without a space.
            joiner = " " if lang == "en" and prev_text and not prev_text[-1].isspace() else ""
            segments[-1] = (lang, prev_text + joiner + token)
        else:
            segments.append((lang, token))
    return [(lang, part) for lang, part in segments if part.strip()]
