try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import base64
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import whatsapp

PHONE = "919876543210"
# A real wamid is base64 that embeds the sender's number.
WAMID = "wamid." + base64.b64encode(f"\x0c{PHONE}\x15\x02\x00".encode()).decode()


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.text = json.dumps(body)
        self.is_success = 200 <= status < 300


class FakeClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return self.response


class LogPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, response):
        log = io.StringIO()
        with patch.object(whatsapp.httpx, "AsyncClient", lambda **kwargs: FakeClient(response)), \
                contextlib.redirect_stdout(log):
            ok = await whatsapp.send_text(PHONE, "hello")
        return ok, log.getvalue()

    async def test_send_log_never_contains_the_phone_number(self):
        delivered = FakeResponse(200, {
            "messaging_product": "whatsapp",
            "contacts": [{"input": PHONE, "wa_id": PHONE}],
            "messages": [{"id": WAMID}],
        })
        refused = FakeResponse(400, {"error": {
            "message": f"Recipient {PHONE} is not in the allowed list", "code": 131030,
        }})
        for response, expected in ((delivered, True), (refused, False)):
            ok, log = await self.send(response)
            self.assertEqual(ok, expected)
            self.assertNotIn(PHONE, log)
            self.assertNotIn(WAMID, log)
            self.assertIn("...3210", log)
        self.assertIn("131030", log)  # the error itself is still visible

    def test_helpers_remove_numbers_and_message_ids(self):
        text = whatsapp.log_safe(f"from +91 98765 43210 id {WAMID} code 131030")
        self.assertNotIn("98765", text)
        self.assertNotIn(WAMID, text)
        self.assertIn("code 131030", text)
        self.assertEqual(whatsapp.who(PHONE), "...3210")
        self.assertEqual(whatsapp.message_ref(WAMID), whatsapp.message_ref(WAMID))
        self.assertNotIn(PHONE, whatsapp.message_ref(WAMID))


if __name__ == "__main__":
    unittest.main()
