try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

import db
import forensics
import main
import vision
import whatsapp
from forensics import DUPLICATE_DISTANCE, prepare_uploaded_image

try:
    from helpers import read_all, seed_session
except ImportError:
    from tests.helpers import read_all, seed_session


ROOT = Path(__file__).resolve().parents[1]
FRESH_INSTRUCTIONS = (
    "Send a photo of the damaged vehicle to start a claim. "
    "You can also attach the claim form, RC or driving licence. "
    "Send any message when you are done and I will analyse it."
)


def jpeg_bytes(color=(20, 40, 60), size=(24, 16)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def analysed_payload(claim_id="CLM-EDGE"):
    return {
        "claim_id": claim_id,
        "status": "analysed",
        "stopped_at": None,
        "vehicles": [{
            "group": 0,
            "label": "White Kia Sedan",
            "status": "claimable",
            "photos": ["damage.jpg"],
            "excluded_photos": [],
            "damage": [{
                "part": "front_bumper",
                "damage_type": "dent",
                "severity": "moderate",
            }],
            "estimate": {
                "line_items": [{
                    "part": "front_bumper",
                    "damage_type": "dent",
                    "severity": "moderate",
                    "low": 4000,
                    "high": 8000,
                }],
                "unpriced": [],
                "unconfirmed": [],
                "total_low": 4000,
                "total_high": 8000,
            },
        }],
        "claimable_count": 1,
        "excluded_photos": [],
        "photos": [],
        "damage": [],
        "estimate": {"total_low": 4000, "total_high": 8000},
        "flags": [],
        "decision": "auto_assess",
        "decision_reasons": [],
        "undetermined": [],
    }


def ready_intake(session):
    return {
        "classifications": [],
        "documents_received": {
            "rc": 1, "driving_licence": 1, "claim_form": 1, "policy": 0,
        },
        "vehicle_count": len(session.get("photos") or []),
        "missing": [],
    }


class ConversationEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        self.original_upload = main.UPLOAD_DIR
        db.DB_PATH = os.path.join(self.temp.name, "edges.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "incoming")
        os.makedirs(main.UPLOAD_DIR, exist_ok=True)
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()
        whatsapp.ANALYSING_IDS.clear()
        whatsapp.LAST_INBOUND.clear()

    def tearDown(self):
        whatsapp.SESSIONS.clear()
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()
        whatsapp.ANALYSING_IDS.clear()
        whatsapp.LAST_INBOUND.clear()
        db.DB_PATH = self.original_db
        main.UPLOAD_DIR = self.original_upload
        self.temp.cleanup()

    def _pending(self, vehicles, prompt=None):
        whatsapp.SESSIONS["user"] = {
            "photos": [],
            "docs": [],
            "pending_choice": {"claim_id": "CLM-CHOICE", "vehicles": vehicles},
            "pending_prompt": prompt,
            "awaiting_docs": False,
            "updated": time.time(),
        }

    async def test_out_of_range_choice_reprompts(self):
        vehicles = [
            {"label": "White Kia Sedan", "status": "claimable"},
            {"label": "Black SUV", "status": "claimable"},
        ]
        prompt = "Reply *1* or *2* to continue with that vehicle."
        self._pending(vehicles, prompt)
        sent = AsyncMock()
        with patch.object(whatsapp, "send_text", sent):
            handled = await whatsapp.handle_pending_choice("user", "3")

        self.assertTrue(handled)
        sent.assert_awaited_once_with("user", prompt)
        self.assertIsNotNone(whatsapp.SESSIONS["user"]["pending_choice"])

    async def test_choice_punctuation_and_confirmation_are_accepted(self):
        vehicles = [
            {"label": "White Kia Sedan", "status": "claimable", "estimate": {}},
            {"label": "Black SUV", "status": "claimable", "estimate": {}},
        ]
        sent = AsyncMock()
        for text in (" 1 ", "1."):
            self._pending(vehicles, "choose")
            with patch.object(whatsapp, "send_text", sent):
                handled = await whatsapp.handle_pending_choice("user", text)
            self.assertTrue(handled, text)
            self.assertNotIn("user", whatsapp.SESSIONS)
            self.assertIn("White Kia Sedan", sent.await_args.args[1])

        one = [vehicles[0]]
        for text in ("yes", "Yes", "y"):
            self._pending(one, "Reply *YES*")
            with patch.object(whatsapp, "send_text", sent):
                handled = await whatsapp.handle_pending_choice("user", text)
            self.assertTrue(handled, text)
            self.assertNotIn("user", whatsapp.SESSIONS)
            self.assertIn("White Kia Sedan", sent.await_args.args[1])

        self._pending(vehicles, "choose")
        with patch.object(whatsapp, "send_text", sent):
            await whatsapp.handle_pending_choice("user", "yes")
        self.assertIn("user", whatsapp.SESSIONS)

    async def test_double_done_runs_one_analysis(self):
        seed_session("user", photos=1, updated=time.time())
        sent = AsyncMock()
        first_started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def slow_analyse(*args, **kwargs):
            calls.append(kwargs.get("from_number"))
            first_started.set()
            await release.wait()
            return analysed_payload()

        with patch.object(whatsapp, "classify_session_intake", ready_intake), \
                patch.object(main, "analyse_claim", slow_analyse), \
                patch.object(whatsapp, "send_text", sent), \
                patch("db.save_file"):
            first = asyncio.create_task(whatsapp.process_session("user"))
            await first_started.wait()
            await whatsapp.process_session("user")
            release.set()
            await first

        self.assertEqual(calls, ["user"])
        self.assertIn(
            whatsapp.ANALYSIS_BUSY_REPLY,
            [call.args[1] for call in sent.await_args_list],
        )

    async def test_photo_during_analysis_starts_a_new_claim(self):
        (old,), _ = seed_session("user", photos=[jpeg_bytes(color=(200, 1, 1))],
                                 updated=time.time())
        old_id = whatsapp.SESSIONS["user"]["photos"][0]
        started = asyncio.Event()
        release = asyncio.Event()
        seen = {}

        async def slow_analyse(*args, **kwargs):
            seen["photos"] = read_all(args[2] if len(args) > 2 else kwargs.get("photo_paths"))
            started.set()
            await release.wait()
            return analysed_payload()

        sent = AsyncMock()
        with patch.object(whatsapp, "classify_session_intake", ready_intake), \
                patch.object(main, "analyse_claim", slow_analyse), \
                patch.object(whatsapp, "send_text", sent), \
                patch("db.save_file"):
            task = asyncio.create_task(whatsapp.process_session("user"))
            await started.wait()
            with patch.object(main, "download_media", AsyncMock(return_value=jpeg_bytes())), \
                    patch.object(main, "send_text", sent):
                await main._store_wa_media("user", "new-photo", {}, "photos")
            release.set()
            await task

        self.assertEqual(seen["photos"], [old])
        self.assertIn(
            whatsapp.NEW_CLAIM_WHILE_BUSY,
            [call.args[1] for call in sent.await_args_list],
        )
        session = whatsapp.SESSIONS["user"]
        self.assertEqual(len(session["photos"]), 1)
        self.assertNotIn(old_id, session["photos"])
        self.assertFalse(session["analysing"])

    async def test_photo_during_pending_choice_starts_over(self):
        self._pending([
            {"label": "White Kia Sedan", "status": "claimable"},
            {"label": "Black SUV", "status": "claimable"},
        ], "choose")
        sent = AsyncMock()
        with patch.object(main, "download_media", AsyncMock(return_value=jpeg_bytes())), \
                patch.object(main, "send_text", sent):
            await main.handle_wa_message({
                "from": "user",
                "id": "fresh-photo",
                "type": "image",
                "image": {"id": "media"},
            })

        sent.assert_awaited_once_with("user", whatsapp.CHOICE_CLEARED_REPLY)
        session = whatsapp.SESSIONS["user"]
        self.assertIsNone(session["pending_choice"])
        self.assertEqual(len(session["photos"]), 1)

    async def test_stale_awaiting_docs_text_reports_the_expiry(self):
        seed_session(
            "user", photos=1, documents=1, awaiting_docs=True, missing_docs=["rc"],
            updated=time.time() - whatsapp.PENDING_CHOICE_TIMEOUT - 5,
        )
        sent = AsyncMock()
        process = AsyncMock()
        with patch.object(main, "send_text", sent), \
                patch.object(main, "process_session", process):
            await main.handle_wa_message({
                "from": "user",
                "id": "late",
                "type": "text",
                "text": {"body": "hello"},
            })

        process.assert_not_awaited()
        sent.assert_awaited_once_with(
            "user",
            "Your earlier photos and documents expired after 30 minutes. "
            "Please send them again.",
        )
        self.assertNotIn("user", whatsapp.SESSIONS)

    async def test_same_photo_twice_is_counted_once_without_a_fraud_flag(self):
        payload = jpeg_bytes()
        sent = AsyncMock()
        download = AsyncMock(return_value=payload)
        with patch.object(main, "download_media", download), \
                patch.object(main, "send_text", sent):
            await main.handle_wa_message({
                "from": "user", "id": "tap-1", "type": "image", "image": {},
            })
            await main.handle_wa_message({
                "from": "user", "id": "tap-2", "type": "image", "image": {},
            })

        session = whatsapp.SESSIONS["user"]
        self.assertEqual(len(session["photos"]), 1)
        self.assertEqual(len(db.list_session_media("user")), 1)
        self.assertEqual(len(session["photos"][0]), 64)
        self.assertEqual(sent.await_count, 2)
        for call in sent.await_args_list:
            self.assertNotIn("duplicate", call.args[1].lower())
            self.assertIn("Photo received (1 total)", call.args[1])

        stored = db.media_get(session["photos"][0])
        self.assertIsNotNone(stored)
        path = os.path.join(self.temp.name, "tap-1.jpg")
        with open(path, "wb") as handle:
            handle.write(stored[0])
        conn = db.get_conn()
        try:
            db.save_file("CLM-ONCE", "tap-1.jpg", "photo", path)
            results = forensics.check_duplicates([path], conn)
        finally:
            conn.close()
        self.assertEqual(results[0]["verdict"], "clean")

    async def test_done_and_skip_phrases(self):
        process = AsyncMock()
        for text, skip in (
            ("skip", True),
            ("Skip", True),
            (" SKIP ", True),
            ("हो गया", False),
            ("ho gaya", False),
            ("Done", False),
        ):
            seed_session("user", photos=1, updated=time.time())
            with patch.object(main, "process_session", process):
                await main.handle_wa_message({
                    "from": "user",
                    "id": f"text-{text}",
                    "type": "text",
                    "text": {"body": text},
                })
            process.assert_awaited_with("user", skip_missing=skip)
            process.reset_mock()

    async def test_two_senders_stay_isolated(self):
        (a,), _ = seed_session("aaa", photos=[jpeg_bytes(color=(1, 99, 1))], updated=time.time())
        (b,), _ = seed_session("bbb", photos=[jpeg_bytes(color=(99, 1, 1))], updated=time.time())
        seen = {}

        async def fake_analyse(*args, **kwargs):
            seen[kwargs.get("from_number")] = read_all(args[2])
            if kwargs.get("from_number") == "aaa":
                await asyncio.sleep(0.02)
            return analysed_payload(f"CLM-{kwargs.get('from_number')}")

        with patch.object(whatsapp, "classify_session_intake", ready_intake), \
                patch.object(main, "analyse_claim", fake_analyse), \
                patch.object(whatsapp, "send_text", AsyncMock()), \
                patch("db.save_file"):
            await asyncio.gather(
                whatsapp.process_session("aaa"),
                whatsapp.process_session("bbb"),
            )

        self.assertEqual(seen["aaa"], [a])
        self.assertEqual(seen["bbb"], [b])

    async def test_analysis_materialises_stored_media_and_deletes_temp_files(self):
        first = jpeg_bytes(color=(9, 9, 9))
        second = jpeg_bytes(color=(8, 8, 8))
        seed_session("user", photos=[first, second], updated=time.time())
        media_ids = list(whatsapp.SESSIONS["user"]["photos"])
        seen = {}

        async def fake_analyse(*args, **kwargs):
            paths = list(args[2])
            seen["paths"] = paths
            seen["bytes"] = read_all(paths)
            state = db.get_session("user")
            for path in paths:
                self.assertNotIn(os.path.basename(path), state)
            self.assertEqual(
                [row["media_id"] for row in db.list_session_media("user")], media_ids
            )
            return analysed_payload()

        with patch.object(whatsapp, "classify_session_intake", ready_intake), \
                patch.object(main, "analyse_claim", fake_analyse), \
                patch.object(whatsapp, "send_text", AsyncMock()), \
                patch("db.save_file"):
            await whatsapp.process_session("user")

        self.assertEqual(seen["bytes"], [first, second])
        for path in seen["paths"]:
            self.assertFalse(os.path.exists(path))
        self.assertNotIn("user", whatsapp.SESSIONS)
        self.assertEqual(db.list_session_media("user"), [])
        for media_id in media_ids:
            self.assertIsNone(db.media_get(media_id))


class HostileInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        self.original_upload = main.UPLOAD_DIR
        db.DB_PATH = os.path.join(self.temp.name, "hostile.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "incoming")
        os.makedirs(main.UPLOAD_DIR, exist_ok=True)
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()
        whatsapp.ANALYSING_IDS.clear()

    def tearDown(self):
        whatsapp.SESSIONS.clear()
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()
        whatsapp.ANALYSING_IDS.clear()
        db.DB_PATH = self.original_db
        main.UPLOAD_DIR = self.original_upload
        self.temp.cleanup()

    async def test_empty_and_corrupt_images_are_excluded(self):
        sent = AsyncMock()
        download = AsyncMock(side_effect=[b"", b"\xff\xd8\xffnot-a-jpeg"])
        with patch.object(main, "download_media", download), \
                patch.object(main, "send_text", sent):
            await main.handle_wa_message({
                "from": "user", "id": "empty", "type": "image", "image": {},
            })
            await main.handle_wa_message({
                "from": "user", "id": "corrupt", "type": "image", "image": {},
            })

        self.assertEqual(whatsapp.get_session("user")["photos"], [])
        self.assertEqual(sent.await_count, 2)
        for call in sent.await_args_list:
            self.assertEqual(call.args[1], whatsapp.UNREADABLE_IMAGE_REPLY)

    def test_twenty_megabyte_image_is_downscaled(self):
        image = Image.new("RGB", (2700, 2600), (12, 34, 56))
        buffer = io.BytesIO()
        image.save(buffer, format="BMP")
        data = buffer.getvalue()
        self.assertGreaterEqual(len(data), 20 * 1024 * 1024)
        started = time.perf_counter()
        prepared = prepare_uploaded_image(data)
        elapsed = time.perf_counter() - started
        self.assertIsNotNone(prepared)
        self.assertLess(len(prepared), 2 * 1024 * 1024)
        self.assertLess(elapsed, 15)
        with Image.open(io.BytesIO(prepared)) as opened:
            opened.load()
            self.assertLessEqual(max(opened.size), 2048)

    async def test_heic_document_is_processed_as_a_photo(self):
        image = Image.new("RGB", (32, 32), (180, 20, 20))
        buffer = io.BytesIO()
        image.save(buffer, format="HEIF")
        sent = AsyncMock()
        with patch.object(main, "download_media", AsyncMock(return_value=buffer.getvalue())), \
                patch.object(main, "send_text", sent):
            await main.handle_wa_message({
                "from": "user",
                "id": "heic-doc",
                "type": "document",
                "document": {
                    "filename": "damage.heic",
                    "mime_type": "application/octet-stream",
                },
            })

        stored = whatsapp.SESSIONS["user"]["photos"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(stored[0]), 64)
        data, mime = db.media_get(stored[0])
        self.assertEqual(mime, "image/heic")
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            self.assertEqual(opened.size, (32, 32))
        sent.assert_awaited_once()
        self.assertIn("Photo received (1 total)", sent.await_args.args[1])

    async def test_unsupported_message_types_explain_what_is_accepted(self):
        sent = AsyncMock()
        messages = [
            ("sticker", {"sticker": {"id": "s"}}),
            ("video", {"video": {"id": "v"}}),
            ("audio", {"audio": {"id": "a", "voice": True}}),
            ("location", {"location": {"latitude": 19.0, "longitude": 72.8}}),
            ("contacts", {"contacts": [{"name": {"formatted_name": "A"}}]}),
        ]
        with patch.object(main, "send_text", sent):
            for message_type, extra in messages:
                await main.handle_wa_message({
                    "from": "user",
                    "id": message_type,
                    "type": message_type,
                    **extra,
                })

        self.assertEqual(sent.await_count, len(messages))
        replies = [call.args[1] for call in sent.await_args_list]
        self.assertEqual(len(set(replies)), len(messages))
        for reply in replies:
            self.assertIn("photo of the damaged vehicle", reply)
            self.assertIn("claim documents", reply)
        self.assertEqual(whatsapp.SESSIONS.get("user", {}).get("photos", []), [])

    async def test_redelivered_message_id_does_not_create_a_second_claim(self):
        body = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "id": "wamid.same",
                            "from": "user",
                            "type": "image",
                            "image": {"id": "media"},
                        }],
                    },
                }],
            }],
        }

        class Request:
            async def json(self):
                return body

        class Background:
            def __init__(self):
                self.tasks = []

            def add_task(self, fn, *args, **kwargs):
                self.tasks.append((fn, args))

        first = Background()
        second = Background()
        payload = jpeg_bytes(color=(1, 2, 3))
        with patch.object(main, "download_media", AsyncMock(return_value=payload)), \
                patch.object(main, "send_text", AsyncMock()):
            await main.webhook_receive(Request(), first)
            self.assertEqual(len(first.tasks), 1)
            await first.tasks[0][0](*first.tasks[0][1])
            stored = list(whatsapp.SESSIONS["user"]["photos"])
            conn = db.get_conn()
            try:
                db.save_file("CLM-FIRST", os.path.basename(stored[0]), "photo", stored[0])
                forensics.check_duplicates(stored, conn)
            finally:
                conn.close()

            whatsapp.PROCESSED.clear()
            whatsapp._PROCESSED_ORDER.clear()
            await main.webhook_receive(Request(), second)

        self.assertEqual(second.tasks, [])
        conn = db.get_conn()
        try:
            claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(claims, 0)
        self.assertEqual(files, 1)
        self.assertEqual(len(stored), 1)

    async def test_all_providers_down_saves_an_escalated_claim(self):
        path = os.path.join(self.temp.name, "damage.jpg")
        with open(path, "wb") as handle:
            handle.write(jpeg_bytes())
        attempted = []

        def boom(parts, model, label):
            attempted.append(model)
            raise RuntimeError(f"unknown model {model}")

        with patch.dict(os.environ, {
            "VISION_PROVIDERS": "groq,gemini,openrouter",
            "GROQ_VISION_MODEL": "bogus-groq",
            "GEMINI_VISION_MODEL": "bogus-gemini",
            "OPENROUTER_VISION_MODEL": "bogus-openrouter",
            "CACHE_ENABLED": "false",
        }), patch.dict(vision.PROVIDER_CALLS, {
            "groq": boom,
            "gemini": boom,
            "openrouter": boom,
        }), patch.object(vision.time, "sleep"):
            payload = await main.analyse_claim("CLM-DOWN", [], [path])

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stopped_at"], "ai_unavailable")
        # An escalated claim is never auto-assessed.
        self.assertEqual(payload["decision"], "needs_review")
        self.assertIn(
            "Automated assessment unavailable: escalated to an assessor",
            payload["decision_reasons"],
        )
        self.assertEqual(
            attempted,
            ["bogus-groq", "bogus-groq", "bogus-gemini", "bogus-gemini",
             "bogus-openrouter", "bogus-openrouter"] * 2,
        )
        self.assertEqual(
            whatsapp.format_reply(payload),
            "\n".join([
                "*Analysis unavailable*",
                "",
                "We could not complete the automated assessment right now.",
                "Your claim *CLM-DOWN* has been escalated to a human assessor.",
                "",
                "You do not need to resend anything.",
            ]),
        )
        saved = db.get_claim("CLM-DOWN")
        self.assertEqual(saved["status"], "error")
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        escalated = source.index("const escalated = status === 'error'")
        auto = source.index("if (!escalated && decision === 'auto_assess')")
        self.assertLess(escalated, auto)
        self.assertIn("verdict ' + (escalated ? 'is-error'", source)

    def test_screen_photo_of_a_claimed_image_is_outside_the_duplicate_gate(self):
        fixtures = ROOT / "tests" / "fixtures"
        original = fixtures / "claimed_panel.jpeg"
        screen = fixtures / "claimed_panel_screenshot.jpeg"
        self.assertTrue(original.exists())
        self.assertTrue(screen.exists())

        def phash(path):
            with Image.open(path) as image:
                thumb = image.copy()
                thumb.thumbnail((256, 256), Image.LANCZOS)
                return forensics.imagehash.phash(thumb)

        distance = phash(original) - phash(screen)
        conn = db.get_conn()
        try:
            db.save_file("CLM-ORIGINAL", original.name, "photo", str(original))
            forensics.check_duplicates([str(original)], conn)
            result = forensics.check_duplicates([str(screen)], conn)
        finally:
            conn.close()

        self.assertEqual(DUPLICATE_DISTANCE, 5)
        self.assertEqual(distance, 24)
        self.assertGreater(distance, DUPLICATE_DISTANCE)
        self.assertEqual(result[0]["verdict"], "clean")


class ProcessedMessageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp.name, "messages.db")
        db.prepare_test_db()
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()

    def tearDown(self):
        whatsapp.PROCESSED.clear()
        whatsapp._PROCESSED_ORDER.clear()
        db.DB_PATH = self.original_db
        self.temp.cleanup()

    def test_expired_message_id_can_be_processed_again(self):
        old = time.time() - (49 * 60 * 60)
        self.assertTrue(db.remember_processed_message("wamid.old", now=old))
        whatsapp.PROCESSED.clear()
        self.assertTrue(whatsapp.remember_message("wamid.old"))


if __name__ == "__main__":
    unittest.main()
