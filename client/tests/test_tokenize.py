import unittest

from client.core.tokenize import language_of_token, split_language_segments, tokenize, token_units


class TokenizeTest(unittest.TestCase):
    def test_tokenize_splits_chinese_and_ascii(self):
        tokens = tokenize("你好世界 hello")
        self.assertGreaterEqual(len(tokens), 2)
        self.assertTrue(any("你好" in token or token == "你好" or token == "世界" or "你" in token for token in tokens))
        self.assertTrue(any("hello" in token.lower() for token in tokens))

    def test_token_units_are_one_per_token(self):
        self.assertEqual(token_units(""), 0.0)
        self.assertEqual(token_units("中"), 1.0)
        self.assertEqual(token_units("ab"), 1.0)

    def test_language_segments_group_runs(self):
        segments = split_language_segments("你好 hello 世界")
        langs = [lang for lang, _ in segments]
        self.assertIn("zh", langs)
        self.assertIn("en", langs)
        for lang, text in segments:
            self.assertTrue(text.strip())
            if lang == "en":
                self.assertRegex(text, r"[A-Za-z]")
            if lang == "zh":
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in text))

    def test_language_of_token(self):
        self.assertEqual(language_of_token("你好"), "zh")
        self.assertEqual(language_of_token("hello"), "en")
        self.assertEqual(language_of_token("123"), "zh")
        self.assertEqual(language_of_token("..."), "other")

    def test_digits_split_from_latin_for_tts(self):
        # Bare digits → zh (Chinese digit reading). Digits glued to Latin → split.
        self.assertEqual(split_language_segments("123"), [("zh", "123")])
        self.assertEqual(
            split_language_segments("test123"),
            [("en", "test"), ("zh", "123")],
        )
        self.assertEqual(
            split_language_segments("abc456def"),
            [("en", "abc"), ("zh", "456"), ("en", "def")],
        )


if __name__ == "__main__":
    unittest.main()
