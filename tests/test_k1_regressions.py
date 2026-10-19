"""Regressions found in the K.1 review. Each drives the real WhatsApp intake and
pipeline with vision calls stubbed, so it exercises storage, sessions and the
duplicate gate exactly as production does."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import hashlib
import io
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

import db
import forensics
import main
import whatsapp


EXPIRED_PHOTOS = "Your earlier photos expired after 30 minutes. Please send them again."


def patterned_jpeg(color, size=(320, 240)):
    image = Image.new("RGB", size, color)
    for x in range(0, size[0], 16):
        for y in range(0, size[1], 16):
            if (x // 16 + y // 16 + sum(color)) % 3 == 0:
                image.putpixel((x, y), tuple(255 - value for value in color))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def noise_jpeg(size=(1800, 1350), quality=92, seed=1):
    pixels = random.Random(seed).randbytes(size[0] * size[1] * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, pixels).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def large_pdf():
    """A PDF over 2 MB whose rendered page is small: a big image drawn small."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "MOTOR CLAIM FORM - Insured: R K Sharma")
    page.insert_image(fitz.Rect(72, 100, 272, 250), stream=noise_jpeg(quality=95, seed=7))
    data = document.tobytes()
    document.close()
    return data


class K1RegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        self.original_upload = main.UPLOAD_DIR
        db.DB_PATH = os.path.join(self.temp.name, "k1.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "uploads")
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()
        whatsapp.LAST_INBOUND.clear()
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()
        self.sent = []
        self.assessed = []
        self.extracted = []
        self.env = patch.dict(os.environ, {
            "AI_DETECTION_ENABLED": "false",
            "CACHE_ENABLED": "false",
            "REQUIRED_DOCS": "",
            "STRICT_DOCS": "false",
        })
        self.env.start()

        async def send(to, body):
            await asyncio.sleep(0)
            self.sent.append((to, body))
            return True

        def classify(path):
            return {"doc_type": "vehicle_photo", "confidence": 0.95, "number_plate": None}

        def extract(documents):
            pages = [path for document in documents for path in document["paths"]]
            self.extracted.append([(path, Path(path).read_bytes()) for path in pages])
            return {"status": "ok", "fields": [], "documents": len(documents),
                    "read": [document["name"] for document in documents],
                    "failed": [], "error": None}

        def assess(paths):
            self.assessed.append([Path(path).read_bytes() for path in paths])
            photos = [{
                "source_photo": os.path.basename(path), "is_vehicle": True,
                "vehicle_group": 0, "colour": "white", "make_model": "Kia",
                "body_type": "sedan",
            } for path in paths]
            damage = [{
                "source_photo": os.path.basename(paths[0]), "part": "front_bumper",
                "damage_type": "dent", "severity": "moderate", "confidence": 0.9,
                "reasoning": "dent on the bumper",
            }] if paths else []
            return {"is_vehicle": True, "vehicle_description": "white Kia sedan",
                    "photos": photos, "damage": damage}

        self.stubs = [
            patch.object(main, "send_text", send),
            patch.object(whatsapp, "send_text", send),
            patch.object(main, "classify_image", classify),
            patch.object(whatsapp, "classify_image", classify),
            patch.object(main, "extract_fields", extract),
            patch.object(whatsapp, "extract_fields", extract),
            patch.object(main, "assess_damage", assess),
        ]
        for stub in self.stubs:
            stub.start()

    def tearDown(self):
        for stub in self.stubs:
            stub.stop()
        self.env.stop()
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()
        whatsapp.LAST_INBOUND.clear()
        db.DB_PATH = self.original_db
        main.UPLOAD_DIR = self.original_upload
        self.temp.cleanup()

    async def photo(self, wa_id, message_id, data, delay=0.0, mime="image/jpeg"):
        async def download(media_obj):
            if delay:
                await asyncio.sleep(delay)
            return data

        with patch.object(main, "download_media", download):
            await main.handle_wa_message({
                "from": wa_id, "id": message_id, "timestamp": str(int(time.time())),
                "type": "image", "image": {"id": message_id, "mime_type": mime},
            })

    async def document(self, wa_id, message_id, data, filename, mime):
        async def download(media_obj):
            return data

        with patch.object(main, "download_media", download):
            await main.handle_wa_message({
                "from": wa_id, "id": message_id, "timestamp": str(int(time.time())),
                "type": "document",
                "document": {"id": message_id, "filename": filename, "mime_type": mime},
            })

    async def text(self, wa_id, message_id, body):
        await main.handle_wa_message({
            "from": wa_id, "id": message_id, "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": body},
        })

    def claims_for(self, wa_id):
        conn = db.get_conn()
        try:
            rows = conn.execute("SELECT payload_json FROM claims").fetchall()
        finally:
            conn.close()
        import json
        claims = [json.loads(row["payload_json"]) for row in rows]
        claims = [claim for claim in claims if claim.get("from_number") == wa_id]
        return sorted(claims, key=lambda claim: claim.get("created_at") or "")

    def replies_to(self, wa_id):
        return [body for to, body in self.sent if to == wa_id]

    # Regression 1: the duplicate gate excluded rows by path, and WhatsApp temp
    # paths repeat for the same number, photo and position.
    async def test_same_number_resubmitting_a_photo_is_rejected_as_duplicate(self):
        photo = patterned_jpeg((200, 30, 30))
        await self.photo("910000000001", "a1", photo)
        await self.text("910000000001", "a2", "Done")
        await self.photo("910000000001", "a3", photo)
        await self.text("910000000001", "a4", "Done")

        claims = self.claims_for("910000000001")
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0]["status"], "analysed")
        self.assertEqual(claims[1]["status"], "rejected")
        self.assertEqual(claims[1]["stopped_at"], "duplicate")
        self.assertIn("Claim rejected - duplicate photo", self.replies_to("910000000001")[-1])

    def test_duplicate_gate_excludes_only_the_current_claim(self):
        path = os.path.join(self.temp.name, "same-name.jpg")
        with open(path, "wb") as handle:
            handle.write(patterned_jpeg((30, 200, 30)))
        conn = db.get_conn()
        try:
            for claim_id in ("CLM-FIRST", "CLM-SECOND"):
                db.save_file(claim_id, "same-name.jpg", "photo", path)
                token = db.current_claim_id.set(claim_id)
                try:
                    results = forensics.check_duplicates([path], conn)
                finally:
                    db.current_claim_id.reset(token)
        finally:
            conn.close()
        self.assertEqual(results[0]["verdict"], "suspicious")
        self.assertIn("CLM-FIRST", results[0]["detail"])

    # Regression 2: concurrent media arrivals overwrote each other's session copy.
    async def test_photos_arriving_together_are_all_kept(self):
        first, second = patterned_jpeg((10, 10, 200)), patterned_jpeg((10, 200, 200))
        await asyncio.gather(
            self.photo("910000000002", "b1", first, delay=0.05),
            self.photo("910000000002", "b2", second, delay=0.10),
        )
        receipts = sorted(
            body for body in self.replies_to("910000000002") if body.startswith("Photo received")
        )
        self.assertEqual(receipts, [
            "Photo received (1 total). Reply DONE when you are done.",
            "Photo received (2 total). Reply DONE when you are done.",
        ])
        await self.text("910000000002", "b3", "Done")
        self.assertEqual(len(self.assessed), 1)
        self.assertEqual(sorted(self.assessed[0]), sorted([first, second]))

    # Regression 3: the 2 MB media cap rejected large photos and PDFs outright.
    async def test_photo_over_two_megabytes_is_reencoded_not_rejected(self):
        photo = noise_jpeg()
        self.assertGreater(len(photo), 2 * 1024 * 1024)
        await self.photo("910000000003", "c1", photo)
        self.assertEqual(
            self.replies_to("910000000003"),
            ["Photo received (1 total). Reply DONE when you are done."],
        )
        await self.text("910000000003", "c2", "Done")
        self.assertEqual(len(self.assessed), 1)
        analysed = self.assessed[0][0]
        self.assertLessEqual(len(analysed), db.MAX_MEDIA_BYTES)
        with Image.open(io.BytesIO(analysed)) as opened:
            self.assertEqual(opened.format, "JPEG")
            self.assertLessEqual(max(opened.size), 2048)

    def test_reencoded_photo_keeps_its_exif(self):
        pixels = random.Random(3).randbytes(1800 * 1350 * 3)
        exif = Image.Exif()
        exif[0x010F] = "ProbeCam"
        exif[0x0132] = "2026:10:10 09:30:00"
        buffer = io.BytesIO()
        Image.frombytes("RGB", (1800, 1350), pixels).save(
            buffer, format="JPEG", quality=92, exif=exif.tobytes()
        )
        original = buffer.getvalue()
        self.assertGreater(len(original), db.MAX_MEDIA_BYTES)

        prepared = forensics.prepare_uploaded_image(original)

        self.assertLessEqual(len(prepared), db.MAX_MEDIA_BYTES)
        with Image.open(io.BytesIO(prepared)) as opened:
            kept = opened.getexif()
        self.assertEqual(kept.get(0x010F), "ProbeCam")
        self.assertEqual(kept.get(0x0132), "2026:10:10 09:30:00")

    async def test_pdf_over_two_megabytes_is_stored_as_rendered_pages(self):
        pdf = large_pdf()
        self.assertGreater(len(pdf), 2 * 1024 * 1024)
        await self.document("910000000004", "d1", pdf, "claim-form.pdf", "application/pdf")
        await self.document("910000000004", "d2", pdf, "claim-form.pdf", "application/pdf")
        self.assertEqual(self.replies_to("910000000004"), [
            "Document received (1 total). Reply DONE when you are done.",
            "Document received (1 total). Reply DONE when you are done.",
        ])
        await self.photo("910000000004", "d3", patterned_jpeg((90, 90, 30)))
        await self.text("910000000004", "d4", "Done")

        pipeline_pages = self.extracted[-1]
        self.assertEqual(len(pipeline_pages), 1)
        with Image.open(io.BytesIO(pipeline_pages[0][1])) as page:
            self.assertEqual(page.format, "JPEG")
            self.assertEqual(max(page.size), 1568)

        conn = db.get_conn()
        try:
            stored = conn.execute(
                "SELECT kind, LENGTH(bytes) AS size FROM media WHERE mime = ?",
                ("application/pdf",),
            ).fetchall()
            documents = conn.execute(
                "SELECT source_sha256, source_bytes FROM files WHERE kind = ?",
                ("document",),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(stored, [])
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["source_sha256"], hashlib.sha256(pdf).hexdigest())
        self.assertEqual(int(documents[0]["source_bytes"]), len(pdf))

    # Regression 4: an idle session vanished without telling the customer.
    async def test_expired_photos_are_reported_and_deleted(self):
        await self.photo("910000000005", "e1", patterned_jpeg((120, 60, 10)))
        later = time.time() + whatsapp.PENDING_CHOICE_TIMEOUT + 60
        with patch("time.time", return_value=later):
            await self.text("910000000005", "e2", "Done")

        self.assertEqual(self.replies_to("910000000005")[-1], EXPIRED_PHOTOS)
        self.assertEqual(self.assessed, [])
        conn = db.get_conn()
        try:
            orphans = conn.execute(
                "SELECT COUNT(*) AS n FROM media WHERE claim_id IS NULL"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(orphans, 0)

    async def test_demo_reset_clears_sessions_and_session_media(self):
        await self.photo("910000000006", "f1", patterned_jpeg((5, 150, 75)))
        main.demo_reset()

        def count(table):
            conn = db.get_conn()
            try:
                return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            finally:
                conn.close()

        self.assertEqual(count("wa_sessions"), 0)
        self.assertEqual(count("media"), 0)
        self.assertEqual(count("wa_session_media"), 0)
        await self.text("910000000006", "f2", "Done")
        self.assertEqual(self.assessed, [])
        self.assertIn("Send a photo of the damaged vehicle", self.replies_to("910000000006")[-1])


if __name__ == "__main__":
    unittest.main()
