try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

import db
import main
import whatsapp

try:
    from helpers import seed_session
except ImportError:
    from tests.helpers import seed_session


class FakePool:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def getconn(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise PoolTimeout("couldn't get a connection after 10.00 sec")
        return "connection"


class PoolCheckoutTests(unittest.TestCase):
    def test_a_pool_timeout_is_retried_once(self):
        pool = FakePool(failures=1)
        self.assertEqual(db._checkout(pool), "connection")
        self.assertEqual(pool.calls, 2)

    def test_a_second_timeout_is_raised(self):
        pool = FakePool(failures=2)
        with self.assertRaises(PoolTimeout):
            db._checkout(pool)
        self.assertEqual(pool.calls, 2)

    def test_the_pool_is_sized_for_production(self):
        if not os.getenv("DB_POOL_MAX_SIZE"):
            self.assertEqual(db.POOL_MAX_SIZE, 20)
        self.assertGreaterEqual(db.POOL_MAX_SIZE, 1)


class PoolTimeoutResponseTests(unittest.TestCase):
    def test_console_gets_a_503_not_a_crash(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "pool-test"}), \
                patch.object(main, "list_claims", side_effect=PoolTimeout("busy")):
            response = TestClient(main.app).get(
                "/api/claims", headers={"X-Admin-Token": "pool-test"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "3")
        self.assertIn("busy", response.json()["detail"])


class PoolTimeoutWhatsAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = (db.DB_PATH, main.UPLOAD_DIR)
        db.DB_PATH = os.path.join(self.temp.name, "pool.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "uploads")
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()

    def tearDown(self):
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()
        db.DB_PATH, main.UPLOAD_DIR = self.original
        self.temp.cleanup()

    async def test_customer_is_asked_for_done_when_the_database_stays_busy(self):
        seed_session("user", photos=1, updated=time.time())
        sent = AsyncMock()

        def ready(view):
            return {"classifications": [], "missing": [],
                    "documents_received": {"rc": 1, "driving_licence": 1, "claim_form": 1},
                    "vehicle_count": len(view.get("photos") or [])}

        async def busy(*args, **kwargs):
            raise PoolTimeout("couldn't get a connection after 10.00 sec")

        with patch.object(whatsapp, "classify_session_intake", ready), \
                patch.object(main, "analyse_claim", busy), \
                patch.object(whatsapp.db, "get_claim", side_effect=PoolTimeout("busy")), \
                patch.object(whatsapp, "send_text", sent):
            await whatsapp.process_session("user")

        texts = [call.args[1] for call in sent.await_args_list]
        self.assertEqual(
            texts[-1], "Something went wrong processing your claim. Reply DONE to try again.")
        self.assertFalse(any("PoolTimeout" in text or "connection" in text for text in texts))
        # The media is kept for the retry.
        self.assertEqual(len(db.list_session_media("user")), 1)
        self.assertNotIn("user", whatsapp.ANALYSING_IDS)


if __name__ == "__main__":
    unittest.main()
