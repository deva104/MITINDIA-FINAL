try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import db
import main

SECRET = "test-app-secret"
BODY = json.dumps({"entry": [{"changes": [{"value": {"messages": [
    {"id": "wamid.TEST", "from": "910000000000", "type": "text", "text": {"body": "hi"}},
]}}]}]}).encode("utf-8")


def signature(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookSignatureTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "webhook.db")
        db.prepare_test_db()
        self.client = TestClient(main.app)

    def tearDown(self):
        db.DB_PATH = self.original_db
        self.folder.cleanup()

    def post(self, headers, secret):
        handled = AsyncMock()
        log = io.StringIO()
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": secret}),                 patch.object(main, "handle_wa_message", handled),                 patch.object(main, "remember_message", return_value=True),                 contextlib.redirect_stdout(log):
            response = self.client.post(
                "/webhook", content=BODY,
                headers={"Content-Type": "application/json", **headers},
            )
        return response, handled, log.getvalue()

    def test_valid_signature_is_processed(self):
        response, handled, _ = self.post({"X-Hub-Signature-256": signature(BODY)}, SECRET)
        self.assertEqual(response.status_code, 200)
        handled.assert_awaited_once()

    def test_missing_or_wrong_signature_is_403_and_not_processed(self):
        for headers in (
            {},
            {"X-Hub-Signature-256": signature(BODY, "another-secret")},
            {"X-Hub-Signature-256": "sha256=" + "0" * 64},
            {"X-Hub-Signature-256": signature(BODY + b" ")},
        ):
            response, handled, log = self.post(headers, SECRET)
            self.assertEqual(response.status_code, 403, headers)
            handled.assert_not_awaited()
            self.assertIn("rejected a webhook", log)

    def test_unset_secret_accepts_with_a_warning(self):
        response, handled, log = self.post({}, "")
        self.assertEqual(response.status_code, 200)
        handled.assert_awaited_once()
        self.assertIn("WARNING: WHATSAPP_APP_SECRET is not set", log)

    def test_health_reports_whether_signatures_are_enforced(self):
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": SECRET}):
            self.assertEqual(main.health()["webhook_signature"], "enforced")
        with patch.dict(os.environ, {"WHATSAPP_APP_SECRET": ""}):
            self.assertEqual(main.health()["webhook_signature"], "not_configured")
        self.assertNotIn(SECRET, json.dumps(main.health()))


if __name__ == "__main__":
    unittest.main()
