try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import db
import main

TOKEN = "test-admin-token"
OVERRIDE = {"actor": "a", "action": "set_decision", "target": "claim",
            "value": "approved", "reason": "checked"}


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "auth.db")
        db.prepare_test_db()
        self.client = TestClient(main.app)

    def tearDown(self):
        db.DB_PATH = self.original_db
        self.folder.cleanup()

    def calls(self):
        return [
            ("post", "/api/demo/reset", None),
            ("post", "/api/claims/CLM-NONE/override", OVERRIDE),
            ("post", "/api/whatsapp/subscribe", None),
            ("get", "/api/whatsapp/status", None),
        ]

    def request(self, method, path, body, headers):
        call = getattr(self.client, method)
        return call(path, json=body, headers=headers) if body else call(path, headers=headers)

    def test_missing_or_wrong_token_is_401(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}):
            for method, path, body in self.calls():
                for headers in ({}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": ""}):
                    response = self.request(method, path, body, headers)
                    self.assertEqual(response.status_code, 401, (path, headers))

    def test_unset_admin_token_refuses_everything(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": ""}):
            for method, path, body in self.calls():
                response = self.request(method, path, body, {"X-Admin-Token": "anything"})
                self.assertEqual(response.status_code, 401, path)

    def test_correct_token_reaches_the_endpoint(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}):
            headers = {"X-Admin-Token": TOKEN}
            reset = self.client.post("/api/demo/reset", headers=headers)
            override = self.client.post(
                "/api/claims/CLM-NONE/override", json=OVERRIDE, headers=headers
            )
            status = self.client.get("/api/whatsapp/status", headers=headers)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json(), {"cleared": True})
        self.assertEqual(override.status_code, 404)
        self.assertEqual(status.status_code, 200)

    def test_public_endpoints_need_no_token(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}):
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            self.assertEqual(self.client.get("/api/claims").status_code, 200)

    def test_console_sends_the_token_and_never_hardcodes_it(self):
        source = Path(main.STATIC_DIR, "app.js").read_text(encoding="utf-8")
        self.assertIn("'X-Admin-Token': adminToken", source)
        self.assertIn("adminFetch('api/demo/reset'", source)
        self.assertIn("await adminFetch(\n        'api/claims/'", source)
        self.assertNotIn(TOKEN, source)


if __name__ == "__main__":
    unittest.main()
