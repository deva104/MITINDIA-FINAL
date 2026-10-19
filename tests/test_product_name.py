try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import ast
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main

ROOT = Path(__file__).resolve().parents[1]


def string_literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            yield "".join(v.value for v in node.values if isinstance(v, ast.Constant))


class ProductNameTests(unittest.TestCase):
    def test_health_and_api_title_say_insureit(self):
        payload = TestClient(main.app).get("/api/health").json()
        self.assertEqual(payload["service"], "InsureIT")
        self.assertEqual(main.app.title, "InsureIT")

    def test_no_message_or_page_says_claimpilot(self):
        # Customer-facing text lives in these modules' string literals. Internal
        # keys keep the old name in lower case (claimpilot.db, claimpilot-media-url).
        for name in ("whatsapp.py", "main.py", "providers.py"):
            for text in string_literals(ROOT / name):
                self.assertNotIn("ClaimPilot", text, f"{name}: {text[:80]}")
        for name in ("index.html", "app.js"):
            self.assertNotIn("ClaimPilot", (ROOT / "static" / name).read_text(encoding="utf-8"))

    def test_internal_names_are_unchanged(self):
        import db

        self.assertTrue(db.DB_PATH.endswith("claimpilot.db"))
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("'claimpilot.adminToken'", source)
        self.assertIn('"CLM-"', (ROOT / "main.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
