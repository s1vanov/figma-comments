"""Every file under scripts/ is pure ASCII: non-ASCII characters are written as escapes.

A raw U+2028/U+2029, BOM or bidi character in source is invisible in most editors and
breaks some tools; the escape (for example "\\u2026") is what the reader can check.
"""
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class SourceIsAscii(unittest.TestCase):
    def test_no_non_ascii_bytes_in_scripts(self):
        bad = []
        files = [p for p in sorted(SCRIPTS.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
        self.assertTrue(files)
        for path in files:
            for number, line in enumerate(path.read_bytes().split(b"\n"), 1):
                if any(byte > 0x7F for byte in line):
                    bad.append("%s:%d" % (path.relative_to(SCRIPTS).as_posix(), number))
        self.assertEqual(bad, [], "non-ASCII bytes in scripts/ (write them as \\uXXXX escapes): %s" % ", ".join(bad))


if __name__ == "__main__":
    unittest.main()
