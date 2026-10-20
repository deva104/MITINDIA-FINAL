try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main

STATIC = Path(__file__).resolve().parents[1] / "static"


class ConsoleTests(unittest.TestCase):
    def test_console_files_are_revalidated_on_every_load(self):
        # A cached app.js once ran against new HTML and still called confirm().
        client = TestClient(main.app)
        for path in ("/", "/app.js", "/styles.css"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers.get("cache-control"), "no-cache", path)

    def test_no_blocking_dialogs(self):
        # Embedded browsers refuse confirm(), prompt() and alert() silently.
        source = STATIC.joinpath("app.js").read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
        for call in ("confirm(", "prompt(", "alert("):
            self.assertNotRegex(code, r"(?<![\w.])" + re.escape(call), call)
            self.assertNotIn("window." + call, code)

    def test_reset_needs_reset_typed_in_the_page(self):
        page = STATIC.joinpath("index.html").read_text(encoding="utf-8")
        source = STATIC.joinpath("app.js").read_text(encoding="utf-8")
        for element in ('id="resetModal"', 'id="resetInput"', 'id="resetConfirm" disabled'):
            self.assertIn(element, page)
        self.assertIn("value.trim() !== 'RESET'", source)


if __name__ == "__main__":
    unittest.main()
