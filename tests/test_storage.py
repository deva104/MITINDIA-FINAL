try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import os
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

import db
import forensics
import main


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        self.original_upload = main.UPLOAD_DIR
        db.DB_PATH = os.path.join(self.folder.name, "storage.db")
        main.UPLOAD_DIR = os.path.join(self.folder.name, "incoming")
        os.makedirs(main.UPLOAD_DIR, exist_ok=True)
        db.prepare_test_db()

    def tearDown(self):
        db.DB_PATH = self.original_db
        main.UPLOAD_DIR = self.original_upload
        self.folder.cleanup()

    def test_media_roundtrip_is_idempotent_and_rejects_oversized_blobs(self):
        stored = db.media_put(b"processed-jpeg", "image/jpeg", "photo", "CLM-1")
        again = db.media_put(b"processed-jpeg", "image/jpeg", "original", "CLM-2")
        self.assertEqual(stored, again)
        self.assertEqual(db.media_get(stored), (b"processed-jpeg", "image/jpeg"))
        self.assertIsNone(db.media_put(b"x" * (db.MAX_MEDIA_BYTES + 1), "image/jpeg", "photo", "CLM-1"))
        stats = db.storage_stats()
        self.assertEqual(stats["media_bytes"], len(b"processed-jpeg"))
        self.assertEqual(stats["claim_count"], 0)

    def test_processed_preview_is_served_from_the_database(self):
        path = Path(self.folder.name) / "damage.jpg"
        Image.new("RGB", (1800, 1200), (20, 40, 60)).save(path, "JPEG")
        before = set(Path(forensics.OUTPUT_DIR).glob("*"))
        urls = forensics.build_photo_url_map([str(path)])
        url = urls["damage.jpg"]
        self.assertTrue(url.startswith("/media/"))
        self.assertEqual(set(Path(forensics.OUTPUT_DIR).glob("*")), before)
        media_id = url.rsplit("/", 1)[-1]
        data, mime = db.media_get(media_id)
        self.assertEqual(mime, "image/jpeg")
        self.assertLessEqual(len(data), db.MAX_MEDIA_BYTES)
        with Image.open(__import__("io").BytesIO(data)) as image:
            self.assertLessEqual(max(image.size), forensics.PROCESSED_EDGE)

    def test_temp_cleanup_stays_inside_upload_directory(self):
        inside = os.path.join(main.UPLOAD_DIR, "CLM-1", "photo.jpg")
        os.makedirs(os.path.dirname(inside), exist_ok=True)
        with open(inside, "wb") as handle:
            handle.write(b"temp")
        outside = os.path.join(self.folder.name, "fixture.pdf")
        with open(outside, "wb") as handle:
            handle.write(b"keep")
        main._release_temp_uploads([inside, outside])
        self.assertFalse(os.path.exists(inside))
        self.assertTrue(os.path.exists(outside))

    def test_whatsapp_session_and_inbound_roundtrip(self):
        db.put_session("user", '{"photos": ["abc"], "updated": 1}', "1")
        self.assertEqual(db.get_session("user"), '{"photos": ["abc"], "updated": 1}')
        db.put_session("user", '{"photos": []}', "2")
        self.assertEqual(db.get_session("user"), '{"photos": []}')
        self.assertEqual(db.list_sessions(), [("user", '{"photos": []}')])
        db.delete_session("user")
        self.assertIsNone(db.get_session("user"))
        db.touch_last_inbound("user", "1700000000.5")
        self.assertEqual(db.get_last_inbound("user"), "1700000000.5")
        db.touch_last_inbound("user", "1700000001")
        self.assertEqual(db.get_last_inbound("user"), "1700000001")
        db.clear_last_inbound()
        self.assertIsNone(db.get_last_inbound("user"))

    def test_health_reports_the_active_backend(self):
        payload = main.health()
        self.assertIn(payload["db"], ("sqlite", "postgres"))
        self.assertEqual(payload["db"], db.database_backend())
        self.assertTrue(payload["db_ok"])
        self.assertTrue(payload["ok"])

    def test_media_route_serves_stored_bytes(self):
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        media_id = db.media_put(b"route-bytes", "image/jpeg", "photo", "CLM-ROUTE")
        with patch.dict(os.environ, {"ADMIN_TOKEN": "media-test-token"}),                 TestClient(main.app) as client:
            signed = main.sign_media_urls({"url": f"/media/{media_id}"})["url"]
            response = client.get(signed)
            unsigned = client.get(f"/media/{media_id}")
            tampered = client.get(signed[:-1] + ("0" if signed[-1] != "0" else "1"))
            expired = client.get(main.sign_media_urls(
                {"url": f"/media/{media_id}"}, now=time.time() - 8 * 3600)["url"])
            health = client.get("/api/health")
            storage = client.get("/api/storage", headers={"X-Admin-Token": "media-test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"route-bytes")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertTrue(response.headers["cache-control"].startswith("private, max-age="))
        for refused in (unsigned, tampered, expired):
            self.assertEqual(refused.status_code, 403)
            self.assertNotIn(b"route-bytes", refused.content)
        self.assertEqual(health.json()["db"], db.database_backend())
        self.assertGreaterEqual(storage.json()["media_bytes"], len(b"route-bytes"))



class WarmUpTests(unittest.TestCase):
    def test_startup_warms_phash_so_the_first_claim_does_not_pay_for_scipy(self):
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        code = "\n".join([
            'import sys, tempfile, os',
            "sys.path.insert(0, 'tests')",
            'import bootstrap, db',
            "db.DB_PATH = os.path.join(tempfile.mkdtemp(), 'warm.db')",
            'import main',
            'from fastapi.testclient import TestClient',
            "print('before', 'scipy.fftpack' in sys.modules)",
            'with TestClient(main.app):',
            "    main.WARM_UP['thread'].join(60)",
            "print('after', 'scipy.fftpack' in sys.modules)",
        ])
        env = {key: value for key, value in os.environ.items()
               if key not in ("CLAIMPILOT_USE_TEST_DB", "DATABASE_URL")}
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env=env,
            capture_output=True, text=True, timeout=180,
        )
        self.assertIn("before False", result.stdout, result.stderr[-800:])
        self.assertIn("after True", result.stdout, result.stderr[-800:])
        self.assertIn("[forensics] warm-up: phash ready in", result.stdout)


if __name__ == "__main__":
    unittest.main()
