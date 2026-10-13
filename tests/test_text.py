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


if __name__ == "__main__":
    unittest.main()
