import unittest

import tests  # noqa: F401  (runs tests/__init__.py, which puts scripts/ on sys.path)
from fclib.urls import FileRef, comment_link, parse_file_ref


class ParseFileRefTests(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(parse_file_ref("Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"), FileRef("Zz9Yy8Xx7Ww6Vv5Uu4Tt3S", None))
        self.assertEqual(parse_file_ref("https://www.figma.com/design/AbC123def456ghi789jkl0/Name?node-id=1-3&t=x"), FileRef("AbC123def456ghi789jkl0", "1:3"))
        self.assertEqual(parse_file_ref("https://figma.com/file/AbC123def456ghi789jkl0/Name"), FileRef("AbC123def456ghi789jkl0", None))
        self.assertEqual(parse_file_ref("https://www.figma.com/design/AbC123def456ghi789jkl0/branch/BrN123def456ghi789jkl0/Name"), FileRef("BrN123def456ghi789jkl0", None))
        with self.assertRaises(ValueError):
            parse_file_ref("https://example.com/x")


class CommentLinkTests(unittest.TestCase):
    def test_link(self):
        self.assertEqual(comment_link("K", "1:3", "42"), "https://www.figma.com/design/K/?node-id=1-3#42")
        self.assertEqual(comment_link("K", None, "42"), "https://www.figma.com/design/K/#42")


class ErrorTextTests(unittest.TestCase):
    def test_long_argument_without_slash_is_cut(self):
        with self.assertRaises(ValueError) as cm:
            parse_file_ref("figd_" + "q" * 60)
        self.assertIn("figd_q", str(cm.exception))
        self.assertNotIn("q" * 10, str(cm.exception))

    def test_short_or_url_like_argument_is_shown(self):
        with self.assertRaises(ValueError) as cm:
            parse_file_ref("hello")
        self.assertIn("hello", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            parse_file_ref("https://example.com/" + "z" * 60)
        self.assertIn("z" * 60, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
