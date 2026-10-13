import unittest

import tests  # noqa: F401  (runs tests/__init__.py, which puts scripts/ on sys.path)
from fclib.text import normalize_outgoing


class NormalizeOutgoingTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_outgoing("**Done** and *ok* ~~x~~ __y__"), "Done and ok x y")
        self.assertEqual(normalize_outgoing("[spec](https://e.com)"), "spec (https://e.com)")
        self.assertEqual(normalize_outgoing("a\u2028b\u2029c"), "a\nb\nc")
        self.assertEqual(normalize_outgoing("5 * 3 = 15"), "5 * 3 = 15")
        self.assertEqual(normalize_outgoing("- one\n- two"), "- one\n- two")
        with self.assertRaises(ValueError):
            normalize_outgoing("  ** **  ")

    def test_line_break_variants(self):
        self.assertEqual(normalize_outgoing("a\r\nb"), "a\nb")
        self.assertEqual(normalize_outgoing("a\rb"), "a\nb")

    def test_trailing_whitespace_per_line(self):
        self.assertEqual(normalize_outgoing("a  \nb \t"), "a\nb")

    def test_surrounding_blank_lines_stripped(self):
        self.assertEqual(normalize_outgoing("\n\na\nb\n\n"), "a\nb")

    def test_only_symbols_raises(self):
        with self.assertRaises(ValueError):
            normalize_outgoing("***")

    def test_underscores_and_punctuation_are_empty(self):
        # "has text" = a letter (L*), a digit (N*) or a symbol emoji (So); "_" is punctuation (Pc)
        for empty in ("_", "* __ *", "___", "- . ,", "\u200b"):
            with self.assertRaises(ValueError, msg=repr(empty)):
                normalize_outgoing(empty)

    def test_invisible_and_bidi_characters_are_refused_naming_the_code_point(self):
        cases = {
            "\U000e0041": "U+E0041",  # tag character
            "‮": "U+202E", "‪": "U+202A", "⁦": "U+2066", "⁩": "U+2069",
            "‎": "U+200E", "‏": "U+200F", "؜": "U+061C",
            "​": "U+200B", "⁠": "U+2060", "﻿": "U+FEFF",
        }
        for ch, name in cases.items():
            with self.assertRaises(ValueError, msg=name) as cm:
                normalize_outgoing("Fixed the title" + ch + " as asked")
            self.assertIn(name, str(cm.exception))
            self.assertNotIn(ch, str(cm.exception))

    def test_variation_selectors_are_refused_unless_one_emoji_selector_follows_a_visible_character(self):
        for text, name in (("ok\ufe00", "U+FE00"), ("ok\ufe0d", "U+FE0D"), ("ok \ufe0f", "U+FE0F"),
                           ("\ufe0fok", "U+FE0F"), ("ok\u2764\ufe0f\ufe0f", "U+FE0F"),
                           ("ok\U000e0100", "U+E0100"), ("ok\U000e01ef", "U+E01EF")):
            with self.assertRaises(ValueError, msg=name) as cm:
                normalize_outgoing(text)
            self.assertIn(name, str(cm.exception))
            self.assertIn("variation selector", str(cm.exception))
        for kept in ("Done \u2764\ufe0f", "\u00a9\ufe0e 2026", "1\ufe0f\u20e3",
                     "\U0001F441\ufe0f\u200d\U0001F5E8\ufe0f"):
            self.assertEqual(normalize_outgoing(kept), kept)

    def test_zwj_and_zwnj_are_allowed(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        self.assertEqual(normalize_outgoing("Done " + family), "Done " + family)
        self.assertEqual(normalize_outgoing("می‌خ"), "می‌خ")

    def test_emoji_alone_is_text(self):
        self.assertEqual(normalize_outgoing("\U0001F44D"), "\U0001F44D")
        self.assertEqual(normalize_outgoing("\u2705"), "\u2705")
        self.assertEqual(normalize_outgoing("\u0662"), "\u0662")  # a non-ASCII digit


if __name__ == "__main__":
    unittest.main()
