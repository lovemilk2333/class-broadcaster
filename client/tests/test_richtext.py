"""Whitelist HTML sanitizer and plain-text extraction for display / TTS."""

from __future__ import annotations

import unittest

from client.core.richtext import (
    extract_data_image_uris,
    html_to_plain_text,
    looks_like_html,
    needs_static_rich_display,
    rewrite_data_images_for_qt,
    sanitize_html,
)


class RichTextSanitizeTests(unittest.TestCase):
    def test_strips_script(self) -> None:
        raw = 'hello <script>alert(1)</script><b>world</b>'
        out = sanitize_html(raw)
        self.assertNotIn("<script", out.lower())
        self.assertNotIn("alert", out)
        self.assertIn("<b>world</b>", out)
        self.assertTrue(out.startswith("hello "))

    def test_strips_iframe_and_on_handlers(self) -> None:
        raw = '<iframe src="http://evil"></iframe><b onclick="x()">ok</b>'
        out = sanitize_html(raw)
        self.assertNotIn("iframe", out.lower())
        self.assertNotIn("onclick", out.lower())
        self.assertIn("<b>", out)
        self.assertIn("ok", out)

    def test_img_data_uri_allowed(self) -> None:
        src = "data:image/png;base64,iVBORw0KGgo="
        raw = f'<img src="{src}" alt="x" width="120" height="80" onclick="evil()" />'
        out = sanitize_html(raw)
        self.assertIn('src="data:image/png;base64,iVBORw0KGgo="', out)
        self.assertIn('alt="x"', out)
        self.assertIn('width="120"', out)
        self.assertIn('height="80"', out)
        self.assertNotIn("onclick", out.lower())

    def test_img_width_height_must_be_digits(self) -> None:
        src = "data:image/png;base64,iVBORw0KGgo="
        out = sanitize_html(f'<img src="{src}" width="120px" height="evil" />')
        self.assertIn("<img", out.lower())
        self.assertNotIn("width=", out)
        self.assertNotIn("height=", out)

    def test_http_img_stripped(self) -> None:
        raw = '<img src="https://example.com/a.png" alt="nope" /><b>keep</b>'
        out = sanitize_html(raw)
        self.assertNotIn("example.com", out)
        self.assertNotIn("<img", out.lower())
        self.assertIn("<b>keep</b>", out)

    def test_file_and_javascript_img_stripped(self) -> None:
        self.assertNotIn("<img", sanitize_html('<img src="file:///etc/passwd" />').lower())
        self.assertNotIn("<img", sanitize_html('<img src="javascript:alert(1)" />').lower())

    def test_span_style_whitelist(self) -> None:
        raw = (
            '<span style="color: red; font-size: 20px; background: url(x); '
            'font-family: serif; position: absolute">t</span>'
        )
        out = sanitize_html(raw)
        self.assertIn("color:", out)
        self.assertIn("font-size:", out)
        self.assertIn("font-family:", out)
        self.assertNotIn("background", out.lower())
        self.assertNotIn("position", out.lower())
        self.assertNotIn("url(", out.lower())

    def test_span_font_weight_and_style_kept(self) -> None:
        # Browsers often emit these from execCommand('bold'/'italic') instead of <b>/<i>.
        out = sanitize_html('<span style="font-weight: bold; font-style: italic">x</span>')
        self.assertIn("font-weight:", out.lower())
        self.assertIn("font-style:", out.lower())
        self.assertIn("x", out)
        bad = sanitize_html('<span style="font-weight: expression(alert(1))">x</span>')
        self.assertNotIn("expression", bad.lower())

    def test_keeps_b_i_u_br(self) -> None:
        raw = "<b>b</b><i>i</i><u>u</u><br>next"
        out = sanitize_html(raw)
        self.assertIn("<b>b</b>", out)
        self.assertIn("<i>i</i>", out)
        self.assertIn("<u>u</u>", out)
        self.assertIn("<br", out.lower())

    def test_svg_inline_kept_script_banned(self) -> None:
        raw = (
            '<svg viewBox="0 0 10 10" onclick="x()">'
            '<circle cx="5" cy="5" r="4" fill="red" />'
            "<script>evil()</script>"
            "</svg>"
        )
        out = sanitize_html(raw)
        self.assertIn("<svg", out.lower())
        self.assertIn("circle", out.lower())
        self.assertNotIn("onclick", out.lower())
        self.assertNotIn("evil", out.lower())
        self.assertNotIn("<script", out.lower())

    def test_external_anchor_stripped(self) -> None:
        raw = 'go <a href="https://evil.example">here</a> now'
        out = sanitize_html(raw)
        self.assertNotIn("<a", out.lower())
        self.assertNotIn("href", out.lower())
        self.assertIn("go ", out)
        self.assertIn("here", out)
        self.assertIn(" now", out)

    def test_plain_text_unchanged_when_no_tags(self) -> None:
        self.assertEqual(sanitize_html("请到办公室"), "请到办公室")
        self.assertEqual(sanitize_html("a < b and c > d"), "a &lt; b and c &gt; d")

    def test_looks_like_html(self) -> None:
        self.assertTrue(looks_like_html("<b>x</b>"))
        self.assertTrue(looks_like_html('hi <img src="data:image/png;base64,AA==" />'))
        self.assertFalse(looks_like_html("a < b"))
        self.assertFalse(looks_like_html("plain text"))


class RichTextPlainTextTests(unittest.TestCase):
    def test_html_to_plain_text_strips_tags(self) -> None:
        self.assertEqual(html_to_plain_text("<b>你好</b> <i>世界</i>"), "你好 世界")
        self.assertEqual(html_to_plain_text("a<br/>b"), "a\nb")
        self.assertEqual(
            html_to_plain_text('<img src="data:image/png;base64,AA==" alt="图标" />后'),
            "图标后",
        )

    def test_html_to_plain_text_decodes_entities(self) -> None:
        self.assertEqual(html_to_plain_text("a &amp; b &lt; c"), "a & b < c")
        self.assertEqual(html_to_plain_text("纯文本"), "纯文本")

    def test_html_to_plain_text_drops_script(self) -> None:
        self.assertEqual(html_to_plain_text("x<script>alert(1)</script>y"), "xy")

    def test_needs_static_rich_display(self) -> None:
        # Emphasis / underline must keep rich text so CJK bold/italic stay visible.
        self.assertTrue(needs_static_rich_display("<b>x</b>"))
        self.assertTrue(needs_static_rich_display("<i>y</i><u>z</u>"))
        self.assertTrue(needs_static_rich_display('<span style="font-weight: bold">c</span>'))
        # Color-only span can still marquee (no emphasis to preserve).
        self.assertFalse(needs_static_rich_display('<span style="color: #f00">c</span>'))
        self.assertTrue(needs_static_rich_display('<img src="data:image/png;base64,AA==" />'))
        self.assertTrue(needs_static_rich_display("line1<br />line2"))
        self.assertFalse(needs_static_rich_display("plain"))
        self.assertFalse(needs_static_rich_display(""))

    def test_rewrite_data_images_for_qt(self) -> None:
        # Minimal 1x1 PNG
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
            "AAAABJRU5ErkJggg=="
        )
        src = f"data:image/png;base64,{png_b64}"
        html = sanitize_html(f'hello <img src="{src}" alt="dot" /> world')
        uris = extract_data_image_uris(html)
        self.assertEqual(len(uris), 1)
        rewritten, resources = rewrite_data_images_for_qt(html)
        self.assertEqual(len(resources), 1)
        self.assertTrue(resources[0][0].startswith("mkcb://"))
        self.assertIn(resources[0][0], rewritten)
        self.assertNotIn("data:image/", rewritten)
        self.assertTrue(resources[0][1].startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
