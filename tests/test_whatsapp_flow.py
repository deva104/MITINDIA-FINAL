"""The live sequence of 19 Oct (CLM-631A3B63), replayed with stubbed providers.

3 photos as an album (plus the extra event the album delivers), a 4-page
claim-form PDF, Done; then, mid-analysis, DONE again and the same 3 photos
(again with an extra event). Groq reads everything except the PDF's page 4,
where it is rate limited, gemini times out and openrouter returns 502.
Every reply is asserted, in order.
"""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import fitz
from PIL import Image

import db
import main
import vision
import whatsapp

try:
    from helpers import fresh_key_state, guided, provider_keys, stub_slots
except ImportError:
    from tests.helpers import fresh_key_state, guided, provider_keys, stub_slots

WA_ID = "919800000031"
COLOURS = {"vehicle_photo": (200, 30, 30), "rc": (30, 200, 30), "driving_licence": (30, 30, 200)}


def photo(kind):
    image = Image.new("RGB", (240, 180), COLOURS[kind])
    for x in range(0, 240, 12):  # a pattern, so each photo hashes apart
        for y in range(0, 180, 12):
            image.putpixel((x, y), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def claim_form_pdf():
    document = fitz.open()
    for number in range(1, 5):
        document.new_page().insert_text((72, 72), f"MOTOR CLAIM FORM page {number}")
    data = document.tobytes()
    document.close()
    return data


def kind_of(jpeg):
    with Image.open(io.BytesIO(jpeg)) as image:
        r, g, b = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
    if max(r, g, b) > 230 and min(r, g, b) > 200:
        return "page"
    return max(COLOURS, key=lambda kind: -sum(abs(a - c) for a, c in zip((r, g, b), COLOURS[kind])))


class StatusError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status_code = status


FIELDS = {
    "rc": [("owner_name_rc", "R K Sharma"), ("registration_number_rc", "MH12QZ4821")],
    "driving_licence": [("holder_name_dl", "R K Sharma"), ("dl_expiry_date", "2034-08-14")],
    "page": [("insured_name_claim_form", "R K Sharma"), ("driver_name_claim_form", "R K Sharma"),
             ("registration_number_claim_form", "MH12QZ4821"), ("accident_date", "2026-10-12")],
}


class LiveSequenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = (db.DB_PATH, main.UPLOAD_DIR)
        db.DB_PATH = os.path.join(self.temp.name, "flow.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "uploads")
        db.prepare_test_db()
        for store in (whatsapp.SESSIONS, whatsapp.PROCESSED, whatsapp.ANALYSING_IDS,
                      whatsapp.BUSY_NOTICE_SENT, whatsapp.DEFERRED_MEDIA, main._MEDIA_SEEN):
            store.clear()
        self.media = {
            "car": photo("vehicle_photo"), "rc": photo("rc"), "dl": photo("driving_licence"),
            "pdf": claim_form_pdf(),
        }
        self.calls = []
        self.analysis_paused = threading.Event()
        self.release = threading.Event()
        self.sent = []

    def tearDown(self):
        self.release.set()
        for store in (whatsapp.SESSIONS, whatsapp.ANALYSING_IDS, whatsapp.BUSY_NOTICE_SENT,
                      whatsapp.DEFERRED_MEDIA, main._MEDIA_SEEN):
            store.clear()
        db.DB_PATH, main.UPLOAD_DIR = self.original
        self.temp.cleanup()

    # --- stubbed providers ------------------------------------------------
    def groq(self, parts, model, label):
        self.calls.append(("groq", label))
        images = [part[1] for part in parts if part[0] == "image"]
        if label == "classify_image":
            kind = kind_of(images[0])
            return {"doc_type": "claim_form" if kind == "page" else kind,
                    "confidence": 0.97, "number_plate": None}
        if label.startswith("extract_fields("):
            if label.endswith("p.4)"):
                raise StatusError("429 Rate limit reached. Please try again in 0.01s.", 429)
            kind = kind_of(images[0])
            return {"fields": [{"name": name, "value": value, "confidence": 0.9,
                                "status": "found"} for name, value in FIELDS[kind]]}
        if label.startswith("assess_damage("):
            names = [part[1].split("Filename: ", 1)[1] for part in parts
                     if part[0] == "text" and part[1].startswith("Filename: ")]
            return {"is_vehicle": True, "vehicle_description": "white sedan",
                    "photos": [{"source_photo": name, "is_vehicle": True, "vehicle_group": 0,
                                "colour": "white", "make_model": "Honda City",
                                "body_type": "sedan"} for name in names],
                    "damage": [{"source_photo": names[0], "part": "front_bumper",
                                "damage_type": "dent", "severity": "moderate",
                                "confidence": 0.9, "reasoning": "dent"}]}
        raise AssertionError(f"unexpected groq call {label}")

    def gemini(self, parts, model, label):
        self.calls.append(("gemini", label))
        threading.Event().wait(1.0)  # longer than the cap: a timeout
        return {"fields": []}

    def openrouter(self, parts, model, label):
        self.calls.append(("openrouter", label))
        raise StatusError("502 Bad Gateway", 502)

    # --- the conversation -------------------------------------------------
    async def send(self, to, body):
        self.assertEqual(to, WA_ID)
        self.sent.append(body)
        return True

    def message(self, kind, media=None, body=None):
        message = {"from": WA_ID, "id": f"wamid.{len(self.sent)}.{kind}.{media}.{body}",
                   "timestamp": "1760900000", "type": kind}
        if kind == "image":
            message["image"] = {"id": media, "mime_type": "image/jpeg"}
        elif kind == "document":
            message["document"] = {"id": media, "mime_type": "application/pdf",
                                   "filename": "Claim_Form.pdf"}
        elif kind == "text":
            message["text"] = {"body": body}
        elif kind == "unsupported":
            message["errors"] = [{"code": 131051, "title": "Message type unknown"}]
        return message

    async def deliver(self, *messages):
        for message in messages:
            await main.handle_wa_message(message)

    async def test_the_live_sequence(self):
        real_forensics = main.run_deep_forensics

        def paused_forensics(*args, **kwargs):
            self.analysis_paused.set()
            # A safety stop only; the test releases it. Generous for Neon from India.
            self.assertTrue(self.release.wait(180))
            return real_forensics(*args, **kwargs)

        async def download(media_obj):
            return self.media[media_obj["id"]]

        with provider_keys(groq=1, gemini=1, openrouter=1), fresh_key_state(), \
                patch.dict(vision.PROVIDER_CALLS, {"groq": self.groq, "gemini": self.gemini,
                                                   "openrouter": self.openrouter}), \
                patch.dict(os.environ, {"VISION_PROVIDERS": "groq,gemini,openrouter",
                                        "CACHE_ENABLED": "false", "AI_DETECTION_ENABLED": "false",
                                        "REQUIRED_DOCS": "rc,driving_licence,claim_form",
                                        "STRICT_DOCS": "false", "WHATSAPP_APP_SECRET": ""}), \
                patch.object(vision, "CALL_TIMEOUT_SECONDS", 0.2), \
                patch.object(vision, "PROVIDER_BUDGET_SECONDS", 0.5), \
                patch.object(main, "download_media", download), \
                patch.object(main, "send_text", self.send), \
                patch.object(whatsapp, "send_text", self.send), \
                patch.object(main, "run_deep_forensics", paused_forensics):
            guided(WA_ID, skip=())
            album = [self.message("image", "car"), self.message("image", "rc"),
                     self.message("image", "dl"), self.message("unsupported")]
            await self.deliver(*album)
            await self.deliver(self.message("document", "pdf"))
            self.assertEqual(self.sent, [
                whatsapp.with_footer("Photo received (1 total). Reply DONE when you are done."),
                whatsapp.with_footer("That looks like your registration certificate — added to "
                                     "step 2. Send more damage photos, or reply *DONE*."),
                whatsapp.with_footer("That looks like your driving licence — added to step 3. "
                                     "Send more damage photos, or reply *DONE*."),
                whatsapp.with_footer("That looks like your claim form — added to step 4. "
                                     "Send more damage photos, or reply *DONE*."),
            ])
            await self.deliver(self.message("text", body="Done"))
            self.assertIn("*Ready to submit*", self.sent[4])
            del self.sent[4]  # the summary; analysis waits for SUBMIT

            done = asyncio.create_task(main.handle_wa_message(self.message("text", body="SUBMIT")))
            for _ in range(2400):  # up to 120 s on a remote database
                if self.analysis_paused.is_set():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(self.analysis_paused.is_set(), "analysis never started")
            self.assertEqual(self.sent[4], "Analysing your claim. This usually takes under a minute.")

            # Mid-analysis: DONE again, then the same three photos as an album.
            await self.deliver(self.message("text", body="DONE"))
            await self.deliver(self.message("image", "car"), self.message("image", "rc"),
                               self.message("image", "dl"), self.message("unsupported"))
            self.assertEqual(self.sent[5:], [
                "Your claim is still being analysed — you will get the result here.",
                # Guided: nothing is kept during an analysis, and the customer is told once.
                whatsapp.GUIDED_BUSY_MEDIA,
            ])

            self.release.set()
            await done

        claims = db.list_claims()
        self.assertEqual(len(claims), 1)
        payload = db.get_claim(claims[0]["claim_id"])
        self.assertEqual(self.sent[7:], [whatsapp.with_footer(whatsapp.format_reply(payload))])
        self.assertEqual(len(self.sent), 8)  # nothing else, no greeting, ever
        self.assertNotIn("start a claim", " ".join(self.sent[:7]))

        # Page 4 failed through the whole chain; pages 1-3 were kept.
        extraction = payload["extraction"]
        self.assertEqual(extraction["status"], "partial")
        failed = [item for item in extraction["failed"] if item.get("pages") == "p.4"]
        self.assertEqual(len(failed), 1)
        self.assertIn("timed out", failed[0]["error"])
        self.assertIn("502", failed[0]["error"])
        chain = [provider for provider, label in self.calls if label.endswith("p.4)")]
        self.assertEqual(chain[:3], ["groq", "gemini", "openrouter"])
        sources = {f["name"]: f["source_doc"] for f in payload["fields"] if f["status"] == "found"}
        # WhatsApp stores a PDF as document<n>.pdf (as CLM-631A3B63 shows).
        self.assertEqual(sources["accident_date"], "document1.pdf p.1-3")
        self.assertEqual(payload["document_check"]["missing"], [])
        asks = [entry.get("request") for entry in payload["undetermined"]
                if entry.get("audience") == "customer_actionable"]
        self.assertIn("please send a clear photo of page 4 of your claim form", asks)
        self.assertIn("page 4 of your claim form", self.sent[7])

        # Guided: the photos re-sent during the analysis were refused, not kept
        # (a new claim starts from the menu); the customer was told to resend.
        self.assertEqual(db.list_session_media(WA_ID), [])
        self.assertNotIn(WA_ID, whatsapp.ANALYSING_IDS)
        self.assertEqual(whatsapp.DEFERRED_MEDIA, {})

    async def test_an_unsupported_event_alone_gets_the_greeting_after_the_window(self):
        # The greeting is the START menu since the guided flow.
        with patch.object(main, "STRAY_EVENT_WINDOW", 0.05), \
                patch.object(main, "send_text", self.send), \
                patch.object(whatsapp, "send_text", self.send):
            await self.deliver(self.message("unsupported"))
        self.assertEqual(self.sent, [whatsapp.with_footer(whatsapp.WELCOME)])

    async def test_an_unsupported_event_before_its_photos_is_silent(self):
        async def download(media_obj):
            return self.media[media_obj["id"]]

        # Wider than a remote database round trip, far under the real 15 s.
        guided(WA_ID)
        with patch.object(main, "STRAY_EVENT_WINDOW", 3.0), \
                patch.object(main, "download_media", download), \
                patch.object(main, "send_text", self.send), \
                patch.object(whatsapp, "send_text", self.send), stub_slots():
            stray = asyncio.create_task(main.handle_wa_message(self.message("unsupported")))
            await asyncio.sleep(0.05)
            await self.deliver(self.message("image", "car"))
            await stray
        self.assertEqual(self.sent, [whatsapp.with_footer("Photo received (1 total). Reply DONE when you are done.")])

    async def test_a_later_unhandled_event_with_media_pending_gets_no_greeting(self):
        async def download(media_obj):
            return self.media[media_obj["id"]]

        later = self.message("unsupported")
        later["timestamp"] = "1760900100"  # sent 100 s after the photo
        guided(WA_ID)
        with patch.object(main, "STRAY_EVENT_WINDOW", 0.05), \
                patch.object(main, "download_media", download), \
                patch.object(main, "send_text", self.send), \
                patch.object(whatsapp, "send_text", self.send), stub_slots():
            await self.deliver(self.message("image", "car"), later)
        self.assertEqual(self.sent, [whatsapp.with_footer("Photo received (1 total). Reply DONE when you are done."),
                                     whatsapp.UNHANDLED_WITH_MEDIA_REPLY])

    async def test_a_reaction_is_never_answered(self):
        with patch.object(main, "STRAY_EVENT_WINDOW", 0.01), \
                patch.object(main, "send_text", self.send):
            await self.deliver(self.message("reaction"))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
