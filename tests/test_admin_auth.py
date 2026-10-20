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
            # Claim data and media references: personal documents.
            ("get", "/api/claims", None),
            ("get", "/api/claims/CLM-NONE", None),
            ("post", "/api/analyze", None),
            ("get", "/api/storage", None),
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
            listed = self.client.get("/api/claims", headers=headers)
            detail = self.client.get("/api/claims/CLM-NONE", headers=headers)
            storage = self.client.get("/api/storage", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json(), [])
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(storage.status_code, 200)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json(), {"cleared": True})
        self.assertEqual(override.status_code, 404)
        self.assertEqual(status.status_code, 200)

    def test_public_endpoints_need_no_token(self):
        empty = {"entry": [{"changes": [{"value": {}}]}]}
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN, "WHATSAPP_APP_SECRET": ""}):
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            # The webhook answers Meta without the admin token.
            self.assertEqual(self.client.post("/webhook", json=empty).status_code, 200)

    def test_claim_responses_carry_signed_media_urls(self):
        media = "/media/" + "a" * 64
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}):
            signed = main.sign_media_urls(
                {"photos": [{"url": media}], "note": f"see {media}", "n": 3}, now=1_000_000)
            again = main.sign_media_urls(signed, now=1_000_000)
        url = signed["photos"][0]["url"]
        self.assertRegex(url, r"^/media/a{64}\?exp=\d+&sig=[0-9a-f]{32}$")
        self.assertIn(url, signed["note"])
        self.assertEqual(signed["n"], 3)
        self.assertEqual(again, signed)  # never signed twice
        with patch.dict(os.environ, {"ADMIN_TOKEN": ""}):
            self.assertEqual(main.sign_media_urls({"url": media})["url"], media)

    def test_console_signs_in_by_session_not_token(self):
        # The console uses the session cookie; the token form and the
        # free-text assessor name are gone (the signed-in user is the actor).
        source = Path(main.STATIC_DIR, "app.js").read_text(encoding="utf-8")
        page = Path(main.STATIC_DIR, "index.html").read_text(encoding="utf-8")
        self.assertIn("const r = await api('api/claims')", source)
        self.assertIn("api('api/claims/' + encodeURIComponent(id))", source)
        self.assertIn("api('api/analyze'", source)
        self.assertIn("api('api/demo/reset'", source)
        self.assertIn("'X-Requested-With': 'InsureIT'", source)
        self.assertIn("credentials: 'same-origin'", source)
        for gone in ("X-Admin-Token", "tokenForm", "assessorName", "adminFetch"):
            self.assertNotIn(gone, source)
            self.assertNotIn(gone, page)
        self.assertIn('id="loginForm"', page)
        self.assertNotIn(TOKEN, source)

    def test_overrides_send_no_actor_from_the_page(self):
        source = Path(main.STATIC_DIR, "app.js").read_text(encoding="utf-8")
        body = source[source.index("'/override',"):source.index("reason\n", source.index("'/override',"))]
        self.assertNotIn("actor", body)


if __name__ == "__main__":
    unittest.main()
