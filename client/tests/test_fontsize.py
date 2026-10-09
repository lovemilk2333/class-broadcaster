"""Font-size expression parsing for admin/device/message overrides."""

from __future__ import annotations

import unittest

from client.core.fontsize import (
    DEFAULT_EXPR,
    EM_BASE_PX,
    clamp_px,
    normalize_expr,
    resolve_px,
)


class FontSizeTests(unittest.TestCase):
    def test_normalize_expr(self) -> None:
        self.assertEqual(normalize_expr(""), "")
        self.assertEqual(normalize_expr("0"), "")
        self.assertEqual(normalize_expr("(默认)"), "")
        self.assertEqual(normalize_expr("38"), "38px")
        self.assertEqual(normalize_expr("38px"), "38px")
        self.assertEqual(normalize_expr(" 1.2 EM "), "1.2em")
        self.assertEqual(normalize_expr("5vw"), "5vw")
        self.assertEqual(normalize_expr("8vh"), "8vh")
        self.assertEqual(normalize_expr("0vw"), "")
        self.assertEqual(normalize_expr("25vh"), "25vh")
        self.assertEqual(normalize_expr("100vw"), "100vw")
        with self.assertRaises(ValueError):
            normalize_expr("10px")
        with self.assertRaises(ValueError):
            normalize_expr("100.1vh")
        with self.assertRaises(ValueError):
            normalize_expr("abc")

    def test_resolve_px(self) -> None:
        # Empty → built-in 25vh when screen height is known.
        self.assertEqual(resolve_px("", screen_height=1080), clamp_px(1080 * 0.25))
        self.assertEqual(DEFAULT_EXPR, "25vh")
        # Without screen metrics, fall back to absolute px.
        self.assertEqual(resolve_px(""), 38)
        self.assertEqual(resolve_px("52px"), 52)
        self.assertEqual(resolve_px("1em"), EM_BASE_PX)
        self.assertEqual(resolve_px("2em"), EM_BASE_PX * 2)
        self.assertEqual(resolve_px("10vw", screen_width=1920), clamp_px(192))
        self.assertEqual(resolve_px("10vh", screen_height=1080), clamp_px(108))
        self.assertEqual(resolve_px("25vh", screen_height=1080), clamp_px(270))
        self.assertEqual(resolve_px("100vh", screen_height=1080), clamp_px(1080))
        # Missing screen metrics fall back for vw/vh.
        self.assertEqual(resolve_px("10vw", screen_width=0), 38)


if __name__ == "__main__":
    unittest.main()
