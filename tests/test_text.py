import unittest

import tests  # noqa: F401  (runs tests/__init__.py, which puts scripts/ on sys.path)
from fclib.text import normalize_outgoing


class NormalizeOutgoingTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_outgoing("**Done** and *ok* ~~x~~ __y__"), "Done and ok x y")
        self.assertEqual(normalize_outgoing("[spec](https://e.com)"), "spec (https://e.com)")
        # Deviation from brief: brief has "a b c" as input, which contradicts the line above
        # (spaces are kept there). Input restored to the evident intent: line breaks are kept.
        self.assertEqual(normalize_outgoing("a\nb\nc"), "a\nb\nc")
        self.assertEqual(normalize_outgoing("5 * 3 = 15"), "5 * 3 = 15")
        self.assertEqual(normalize_outgoing("- one\n- two"), "- one\n- two")
        with self.assertRaises(ValueError):
            normalize_outgoing("  ** **  ")


if __name__ == "__main__":
    unittest.main()
