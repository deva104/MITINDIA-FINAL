"""Second production run: unreported model confidence, documents caught by the
duplicate gate, and document views that disagreed."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import hashlib
import io
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import db
import gemini
import main
import rules
import vision
import whatsapp

try:
    from helpers import fresh_key_state, provider_keys, seed_session
except ImportError:
    from tests.helpers import fresh_key_state, provider_keys, seed_session

ROOT = Path(__file__).resolve().parents[1]


def jpeg(color, size=(96, 64)):
    """Patterned, so different colours also differ in perceptual hash (solid
    images all share phash 8000000000000000)."""
    image = Image.new("RGB", size, color)
    rng = random.Random(repr(color))
    for x in range(0, size[0], 8):
        for y in range(0, size[1], 8):
            if rng.random() < 0.4:
                for dx in range(8):
                    for dy in range(8):
                        image.putpixel((min(x + dx, size[0] - 1), min(y + dy, size[1] - 1)),
                                       tuple(255 - value for value in color))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


# The shape production's cache holds: damage items with no confidence key.
PRODUCTION_RESPONSE = {
    "is_vehicle": True,
    "vehicle_description": "silver hatchback",
    "photos": [{"source_photo": "damage.jpg", "is_vehicle": True, "vehicle_group": 0,
                "colour": "silver", "make_model": None, "body_type": "hatchback"}],
    "damage": [
        {"part": "front_left_door", "damage_type": "dent", "severity": "severe",
         "reasoning": "deep dent", "source_photo": "damage.jpg"},
        {"part": "front_left_fender", "damage_type": "scratch", "severity": "minor",
         "confidence": "85%", "reasoning": "scratch", "source_photo": "damage.jpg"},
    ],
}


# A -------------------------------------------------------------------------
class ConfidenceTests(unittest.TestCase):
    def test_parser_reads_real_shapes_and_never_defaults(self):
        cases = [
            (0.85, 0.85), (85, 0.85), ("85%", 0.85), (" 85 % ", 0.85), ("0.7", 0.7),
            ({"value": 0.6}, 0.6), ({"score": "90%"}, 0.9), ("high", 0.9),
            (None, None), ("", None), ("abc", None), (True, None), (150, None),
            (-1, None), (float("nan"), None), ([0.9], None),
        ]
        for raw, expected in cases:
            self.assertEqual(gemini.parse_confidence(raw), expected, raw)

    def test_prompt_demands_a_number(self):
        self.assertIn("confidence MUST be a number between 0 and 1", gemini.DAMAGE_PROMPT)
        self.assertIn("confidence MUST be a number between 0 and 1", gemini.CLASSIFY_PROMPT)

    def test_missing_confidence_is_an_honest_gap_not_half(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        photo = os.path.join(temp.name, "damage.jpg")
        Path(photo).write_bytes(jpeg((150, 150, 160)))

        class Conn:
            def close(self):
                pass

        with provider_keys(), fresh_key_state(), patch.dict(vision.PROVIDER_CALLS, {
            "groq": lambda parts, model, label: json.loads(json.dumps(PRODUCTION_RESPONSE)),
        }), patch.dict(os.environ, {"VISION_PROVIDERS": "groq", "CACHE_ENABLED": "false",
                                    "REQUIRED_DOCS": ""}), \
                patch.multiple(
                    main,
                    get_conn=lambda: Conn(),
                    check_duplicates=lambda paths, conn: [],
                    build_photo_url_map=lambda paths: {},
                    classify_image=lambda path: {"doc_type": "vehicle_photo",
                                                 "confidence": 0.95, "number_plate": None},
                    run_deep_forensics=lambda paths, conn, fields=None: [],
                    save_claim=lambda claim_id, payload: None,
                ):
            payload = asyncio.run(main.analyse_claim("TEST-CONF", [], [photo]))

        by_part = {item["part"]: item for item in payload["damage"]}
        self.assertIsNone(by_part["front_left_door"]["confidence"])
        self.assertFalse(by_part["front_left_door"]["confidence_reported"])
        self.assertEqual(by_part["front_left_fender"]["confidence"], 0.85)
        self.assertNotIn(0.5, [item["confidence"] for item in payload["damage"]])
        self.assertIn({
            "item": "Front Left Door damage",
            "reason": "model did not report confidence for front left door",
            "impact": "excluded from the headline estimate until an assessor confirms it",
            "audience": "internal",
        }, payload["undetermined"])
        self.assertIn("Model did not report confidence for front left door",
                      payload["decision_reasons"])
        # The finding is kept, never auto-confirmed, and the headline average
        # uses only the score the model gave.
        self.assertEqual(payload["confidence"], 0.85)
        unconfirmed = [entry["part"] for entry in payload["estimate"]["unconfirmed"]]
        self.assertIn("front_left_door", unconfirmed)


class WhatsAppFlow(unittest.IsolatedAsyncioTestCase):
    """Real intake, pipeline and storage; vision is stubbed by image content."""

    REQUIRED = "rc,driving_licence,claim_form"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = (db.DB_PATH, main.UPLOAD_DIR)
        db.DB_PATH = os.path.join(self.temp.name, "flow.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "uploads")
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()
        self.types = {}
        self.sent = []
        self.intakes = []
        self.extract_fails = False
        self.env = patch.dict(os.environ, {"REQUIRED_DOCS": self.REQUIRED,
                                           "AI_DETECTION_ENABLED": "false",
                                           "STRICT_DOCS": "false"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        whatsapp.SESSIONS.clear()
        db.DB_PATH, main.UPLOAD_DIR = self.original
        self.temp.cleanup()

    def image(self, kind, color):
        data = jpeg(color)
        self.types[hashlib.sha256(data).hexdigest()] = kind
        return data

    def classify(self, path):
        kind = self.types[hashlib.sha256(Path(path).read_bytes()).hexdigest()]
        return {"doc_type": kind, "confidence": 0.97, "number_plate": None}

    def extract(self, documents):
        result = {"status": "ok", "fields": [], "documents": len(documents),
                  "read": [], "failed": [], "error": None}
        values = {
            "rc": [("owner_name_rc", "R K Sharma")],
            "driving_licence": [("holder_name_dl", "R K Sharma")],
            "claim_form": [("insured_name_claim_form", "R K Sharma")],
            None: [("accident_date", "2026-10-10")],
        }
        for document in documents:
            if self.extract_fails or (document.get("doc_type") is None and self.pdf_unread):
                result["failed"].append({"name": document["name"],
                                         "doc_type": document.get("doc_type"),
                                         "error": "503 UNAVAILABLE"})
                continue
            result["read"].append(document["name"])
            for name, value in values[document.get("doc_type")]:
                result["fields"].append({"name": name, "value": value, "status": "found",
                                         "confidence": 0.9, "source_doc": document["name"]})
        if result["failed"]:
            result["status"] = "partial" if result["read"] else "failed"
            result["error"] = result["failed"][0]["error"]
        return result

    def assess(self, paths):
        photos = [{"source_photo": os.path.basename(path), "is_vehicle": True,
                   "vehicle_group": 0, "colour": "white", "make_model": "Kia",
                   "body_type": "sedan"} for path in paths]
        damage = [{"source_photo": os.path.basename(paths[0]), "part": "front_bumper",
                   "damage_type": "dent", "severity": "moderate", "confidence": 0.9,
                   "reasoning": "dent"}] if paths else []
        return {"is_vehicle": True, "vehicle_description": "white Kia",
                "photos": photos, "damage": damage}

    async def send(self, to, body):
        self.sent.append(body)
        return True

    async def run_claim(self, wa_id, photos, documents=(), skip=True):
        self.pdf_unread = getattr(self, "pdf_unread", False)
        seed_session(wa_id, photos=list(photos), documents=list(documents),
                     updated=time.time())
        real_intake = whatsapp.classify_session_intake

        def recording(view):
            result = real_intake(view)
            self.intakes.append(result)
            return result

        with patch.object(whatsapp, "classify_image", self.classify), \
                patch.object(main, "classify_image", self.classify), \
                patch.object(whatsapp, "extract_fields", self.extract), \
                patch.object(main, "extract_fields", self.extract), \
                patch.object(main, "assess_damage", self.assess), \
                patch.object(whatsapp, "classify_session_intake", recording), \
                patch.object(whatsapp, "send_text", self.send):
            await whatsapp.process_session(wa_id, skip_missing=skip)
        conn = db.get_conn()
        try:
            rows = conn.execute("SELECT payload_json FROM claims").fetchall()
        finally:
            conn.close()
        claims = sorted((json.loads(row["payload_json"]) for row in rows),
                        key=lambda claim: claim["created_at"])
        return claims[-1]


# B -------------------------------------------------------------------------
class DocumentsAreNeverDuplicateGatedTests(WhatsAppFlow):
    REQUIRED = "rc,driving_licence"

    async def test_reused_rc_and_licence_with_a_new_photo(self):
        rc = self.image("rc", (200, 200, 30))
        dl = self.image("driving_licence", (30, 200, 200))
        first = await self.run_claim("910000000010",
                                     [self.image("vehicle_photo", (120, 20, 20)), rc, dl])
        self.sent.clear()
        second = await self.run_claim("910000000010",
                                      [rc, dl, self.image("vehicle_photo", (20, 120, 20))])

        self.assertEqual(second["status"], "analysed")
        self.assertEqual(second["excluded_photos"], [])
        self.assertEqual(len(second["vehicles"]), 1)
        self.assertEqual(second["vehicles"][0]["status"], "claimable")
        self.assertEqual(len(second["vehicles"][0]["photos"]), 1)
        self.assertEqual(second["document_check"]["received"]["rc"], 1)
        self.assertEqual(second["document_check"]["received"]["driving_licence"], 1)
        state = whatsapp.SESSIONS.get("910000000010")
        self.assertIsNone((state or {}).get("pending_choice"))
        reply = self.sent[-1]
        for phrase in ("already submitted", "cannot be claimed", "duplicate", "Reply *YES*"):
            self.assertNotIn(phrase, reply)
        notes = [entry for entry in second["forensics"] if entry["check"] == "document_reuse"]
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(first["claim_id"] in note["detail"] for note in notes))
        self.assertTrue(all(note["verdict"] == "info" for note in notes))

    async def test_a_reused_damage_photo_is_still_rejected(self):
        damage = self.image("vehicle_photo", (90, 10, 10))
        await self.run_claim("910000000011", [damage, self.image("rc", (5, 5, 200))])
        second = await self.run_claim("910000000011", [damage])
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["stopped_at"], "duplicate")


# C -------------------------------------------------------------------------
class FourViewsAgreeTests(WhatsAppFlow):
    def views(self, payload):
        intake = self.intakes[0]
        labels = rules.DOCUMENT_LABELS
        required = self.REQUIRED.split(",")
        checklist = whatsapp.checklist_reply(intake) if intake["vehicle_count"] else ""
        card = payload["document_check"]
        return {
            "checklist_received": {t for t in required if intake["documents_received"].get(t)},
            "checklist_missing": set(intake["document_check"]["missing"]),
            "checklist_text_received": {t for t in required if f"✓ {labels[t]}" in checklist},
            "checklist_text_missing": {t for t in required if f"- {labels[t]}" in checklist}
            if intake["missing"] else set(),
            "card_received": {t for t in required if card["received"].get(t)},
            "card_missing": set(card["missing"]),
            "undetermined_missing": {
                t for t in required
                if any(entry["item"] == labels[t] for entry in payload["undetermined"])
            },
            "reason": card["reason"],
            "reason_in_decision": card["reason"] in payload["decision_reasons"]
            if card["reason"] else not any(
                reason.startswith(("documents not provided", "required documents not established"))
                for reason in payload["decision_reasons"]),
            "unverified": (intake["document_check"]["unverified"], card["unverified"]),
        }

    def assert_agree(self, views, missing):
        required = set(self.REQUIRED.split(","))
        self.assertEqual(views["checklist_missing"], missing)
        self.assertEqual(views["card_missing"], missing)
        self.assertEqual(views["undetermined_missing"], missing)
        self.assertEqual(views["checklist_received"], required - missing)
        self.assertEqual(views["card_received"], required - missing)
        self.assertTrue(views["reason_in_decision"])
        if missing and not any(views["unverified"]):
            self.assertEqual(views["checklist_text_missing"], missing)
            self.assertEqual(views["checklist_text_received"], required - missing)

    async def test_all_documents_present(self):
        payload = await self.run_claim("910000000020", [
            self.image("vehicle_photo", (1, 100, 1)), self.image("rc", (100, 1, 1)),
            self.image("driving_licence", (1, 1, 100)), self.image("claim_form", (100, 100, 1)),
        ])
        views = self.views(payload)
        self.assert_agree(views, set())
        self.assertIsNone(views["reason"])

    async def test_one_missing(self):
        payload = await self.run_claim("910000000021", [
            self.image("vehicle_photo", (2, 100, 1)), self.image("rc", (101, 1, 1)),
            self.image("driving_licence", (1, 2, 100)),
        ])
        views = self.views(payload)
        self.assert_agree(views, {"claim_form"})
        self.assertEqual(views["reason"], "documents not provided: claim form")

    async def test_all_missing(self):
        payload = await self.run_claim("910000000022", [self.image("vehicle_photo", (3, 100, 1))])
        views = self.views(payload)
        self.assert_agree(views, {"rc", "driving_licence", "claim_form"})
        self.assertEqual(
            views["reason"],
            "documents not provided: registration certificate, driving licence, claim form",
        )

    async def test_present_but_extraction_failed(self):
        self.extract_fails = True
        payload = await self.run_claim("910000000023", [
            self.image("vehicle_photo", (4, 100, 1)), self.image("rc", (102, 1, 1)),
            self.image("driving_licence", (1, 3, 100)), self.image("claim_form", (100, 101, 1)),
        ])
        views = self.views(payload)
        self.assert_agree(views, set())
        self.assertEqual(payload["extraction"]["status"], "failed")
        self.assertTrue(any(entry["item"] == "Document details"
                            for entry in payload["undetermined"]))

    async def test_unread_claim_form_pdf_is_not_established_everywhere(self):
        self.pdf_unread = True
        payload = await self.run_claim("910000000024", [
            self.image("vehicle_photo", (5, 100, 1)), self.image("rc", (103, 1, 1)),
            self.image("driving_licence", (1, 4, 100)),
        ], documents=[jpeg((250, 250, 250))])
        views = self.views(payload)
        self.assertEqual(views["unverified"], (True, True))
        self.assertEqual(views["checklist_missing"], {"claim_form"})
        self.assertEqual(views["card_missing"], {"claim_form"})
        self.assertEqual(views["undetermined_missing"], {"claim_form"})
        self.assertTrue(views["reason"].startswith("required documents not established"))
        self.assertTrue(views["reason_in_decision"])

    def test_console_documents_card_reads_document_check(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const received = (check && check.received) || p.documents_received", source)


if __name__ == "__main__":
    unittest.main()
