try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import os
import tempfile
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

    def test_health_reports_the_active_backend(self):
        payload = main.health()
        self.assertIn(payload["db"], ("sqlite", "postgres"))
        self.assertEqual(payload["db"], db.database_backend())
        self.assertTrue(payload["db_ok"])
        self.assertTrue(payload["ok"])

    def test_media_route_serves_stored_bytes(self):
        from fastapi.testclient import TestClient

        media_id = db.media_put(b"route-bytes", "image/jpeg", "photo", "CLM-ROUTE")
        with TestClient(main.app) as client:
            response = client.get(f"/media/{media_id}")
            health = client.get("/api/health")
            storage = client.get("/api/storage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"route-bytes")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(health.json()["db"], db.database_backend())
        self.assertGreaterEqual(storage.json()["media_bytes"], len(b"route-bytes"))


if __name__ == "__main__":
    unittest.main()
