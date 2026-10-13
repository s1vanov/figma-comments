"""The fixtures hold no value copied from the real sandbox file (global constraint: fabricated data only)."""
import json
import re
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "basic"

# Fabricated on 14 October 2026 in the shape of the real fields (a 19-digit version string,
# an ISO-8601 UTC time); the captured values of the sandbox file were removed.
FAKE_VERSION = "1000000000000000001"
FAKE_LAST_MODIFIED = "2026-01-01T00:00:00Z"


class FixtureHygieneTests(unittest.TestCase):
    def test_nodes_fixtures_carry_fabricated_version_and_last_modified(self):
        for name in ("nodes.json", "shape-nodes.json"):
            with self.subTest(name=name):
                data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
                self.assertEqual(data["version"], FAKE_VERSION)
                self.assertEqual(data["lastModified"], FAKE_LAST_MODIFIED)
                self.assertRegex(data["version"], r"^\d{19}$")
                self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data["lastModified"]))


if __name__ == "__main__":
    unittest.main()
