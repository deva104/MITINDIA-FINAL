"""The guided WhatsApp journey, end to end, with the mock VAHAN registry.

Vision is stubbed by image content (colour), so every step, slot, registry
check, result, open-claim attachment, END and tracking reply is asserted.
"""
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
from datetime import date
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

import db
import main
import registry
import rules
import whatsapp

WA_ID = "918408016464"  # the registered mobile of every mock vehicle
TODAY = date(2026, 10, 19)
COLOURS = {"car": (200, 40, 40), "car2": (40, 40, 200), "rc": (40, 180, 40),
           "dl": (200, 200, 40)}


def jpeg(kind, seed=0):
    from PIL import ImageDraw

    image = Image.new("RGB", (320, 240), COLOURS[kind])
    draw = ImageDraw.Draw(image)
    draw.rectangle([20 + seed * 37 % 200, 30, 120 + seed * 37 % 200, 120],
                   fill=tuple(max(0, value - 110) for value in COLOURS[kind]))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def pdf(pages=1):
    document = fitz.open()
    for number in range(1, pages + 1):
        document.new_page().insert_text((72, 72), f"MOTOR CLAIM FORM page {number}")
    data = document.tobytes()
    document.close()
    return data


def kind_of(path):
    with Image.open(path) as image:
        rgb = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
    if min(rgb) > 200:
        return "page"
    return min(COLOURS, key=lambda kind: sum(abs(a - b) for a, b in zip(rgb, COLOURS[kind])))


FIELDS = {
    "rc": [("owner_name_rc", "RAJESH KUMAR SHARMA"), ("registration_number_rc", "MH-12-AB-1234")],
    "driving_licence": [("holder_name_dl", "RAJESH KUMAR SHARMA"), ("dl_number", "MH1220040045678"),
                        ("dl_expiry_date", "2031-02-12")],
    None: [("insured_name_claim_form", "R. K. Sharma"), ("driver_name_claim_form", "R. K. Sharma"),
           ("registration_number_claim_form", "MH12AB1234"), ("accident_date", "2026-10-12")],
}


class JourneyBase(unittest.IsolatedAsyncioTestCase):
    seen = {"colour": "silver", "make_model": "Maruti Suzuki Swift", "body_type": "sedan"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = (db.DB_PATH, main.UPLOAD_DIR)
        db.DB_PATH = os.path.join(self.temp.name, "journey.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "uploads")
        db.prepare_test_db()
        registry.load(force=True)
        for store in (whatsapp.SESSIONS, whatsapp.PROCESSED, whatsapp.ANALYSING_IDS,
                      whatsapp.BUSY_NOTICE_SENT, whatsapp.DEFERRED_MEDIA, main._MEDIA_SEEN):
            store.clear()
        self.sent = []
        self.media = {}
        self.counter = 0
        self.patches = [
            patch.object(registry, "today", lambda: TODAY),
            patch.dict(os.environ, {"AI_DETECTION_ENABLED": "false", "CACHE_ENABLED": "false",
                                    "REQUIRED_DOCS": "rc,driving_licence,claim_form",
                                    "STRICT_DOCS": "false", "WHATSAPP_APP_SECRET": "",
                                    "ASSESSOR_CONTACT": "+91 98000 00000"}),
            patch.object(main, "send_text", self.send),
            patch.object(whatsapp, "send_text", self.send),
            patch.object(main, "download_media", self.download),
            patch.object(main, "classify_image", self.classify),
            patch.object(whatsapp, "classify_image", self.classify),
            patch.object(main, "extract_fields", self.extract),
            patch.object(whatsapp, "extract_fields", self.extract),
            patch.object(main, "assess_damage", self.assess),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        for store in (whatsapp.SESSIONS, whatsapp.ANALYSING_IDS, whatsapp.DEFERRED_MEDIA):
            store.clear()
        db.DB_PATH, main.UPLOAD_DIR = self.original
        self.temp.cleanup()

    # stubs ---------------------------------------------------------------
    async def send(self, to, body):
        self.sent.append(body)
        return True

    async def download(self, media_obj):
        return self.media[media_obj["id"]]

    def classify(self, path):
        kind = kind_of(path)
        doc = {"rc": "rc", "dl": "driving_licence", "page": "other_document"}.get(kind, "vehicle_photo")
        return {"doc_type": doc, "confidence": 0.97, "number_plate": None}

    def extract(self, documents):
        result = {"status": "ok", "fields": [], "documents": len(documents), "read": [],
                  "failed": [], "error": None}
        for document in documents:
            result["read"].append(document["name"])
            for name, value in FIELDS[document.get("doc_type")]:
                result["fields"].append({"name": name, "value": value, "status": "found",
                                         "confidence": 0.92, "source_doc": document["name"]})
        return result

    def assess(self, paths):
        names = [os.path.basename(path) for path in paths]
        return {"is_vehicle": True, "vehicle_description": "silver Maruti Suzuki Swift",
                "photos": [dict(self.seen, source_photo=name, is_vehicle=True, vehicle_group=0)
                           for name in names],
                "damage": [{"source_photo": names[0], "part": "rear_bumper", "damage_type": "dent",
                            "severity": "moderate", "confidence": 0.9, "reasoning": "dent"}]}

    # messages ------------------------------------------------------------
    async def say(self, body):
        self.counter += 1
        await main.handle_wa_message({"from": WA_ID, "id": f"m{self.counter}",
                                      "timestamp": str(int(time.time())), "type": "text",
                                      "text": {"body": body}})
        return self.sent[-1]

    async def image(self, data):
        self.counter += 1
        key = f"img{self.counter}"
        self.media[key] = data
        await main.handle_wa_message({"from": WA_ID, "id": f"m{self.counter}",
                                      "timestamp": str(int(time.time())), "type": "image",
                                      "image": {"id": key, "mime_type": "image/jpeg"}})
        return self.sent[-1]

    async def document(self, data):
        self.counter += 1
        key = f"doc{self.counter}"
        self.media[key] = data
        await main.handle_wa_message({"from": WA_ID, "id": f"m{self.counter}",
                                      "timestamp": str(int(time.time())), "type": "document",
                                      "document": {"id": key, "mime_type": "application/pdf",
                                                   "filename": "Claim_Form.pdf"}})
        return self.sent[-1]

    async def to_step_one(self, plate="MH12AB1234"):
        await self.say("hi")
        await self.say("1")
        await self.say(plate)
        return await self.say("YES")


class FullJourneyTests(JourneyBase):
    async def test_the_full_journey(self):
        self.assertEqual(await self.say("hello"), whatsapp.WELCOME)
        self.assertEqual(await self.say("1"), whatsapp.PLATE_PROMPT)
        self.assertEqual(await self.say("MH-12-XYZ"),
                         "That is not a valid registration number. " + whatsapp.PLATE_PROMPT)
        summary = await self.say("mh 12 ab 1234")
        for text in ("*Vehicle found: MH12AB1234*", "Silver Maruti Suzuki Swift VXI (sedan)",
                     "Registered owner: R*** K*** Sharma", "Insurer: The New India Assurance",
                     "Policy: ****8271, valid until 31 Dec 2026",
                     "✓ This WhatsApp number matches the registered mobile",
                     whatsapp.CONFIRM_PROMPT):
            self.assertIn(text, summary)
        self.assertEqual(await self.say("Yes"), whatsapp.STEP_PROMPTS[1])
        self.assertEqual(await self.image(jpeg("car", 1)),
                         "Photo received (1 total). Reply DONE when you are done.")
        self.assertEqual(await self.image(jpeg("car", 2)),
                         "Photo received (2 total). Reply DONE when you are done.")
        self.assertEqual(await self.say("DONE"), whatsapp.STEP_PROMPTS[2])
        self.assertEqual(await self.image(jpeg("rc")),
                         "Registration certificate received (step 2 of 4).\n\n"
                         + whatsapp.STEP_PROMPTS[3])
        self.assertEqual(await self.say("skip"), whatsapp.STEP_PROMPTS[4])
        before = len(self.sent)
        result = await self.document(pdf())
        self.assertEqual(self.sent[before:before + 2], [
            "Claim form received (step 4 of 4).",
            whatsapp.ANALYSING_REPLY,
        ])
        claims = db.list_claims()
        self.assertEqual(len(claims), 1)
        claim_id = claims[0]["claim_id"]
        payload = db.get_claim(claim_id)
        self.assertEqual(result, whatsapp.format_reply(payload))
        for text in (f"Tracking ID: *{claim_id}*", "Assessor: Meera Kulkarni, +91 98000 00000",
                     "Next: Your assessor will review this within 1 working day.",
                     whatsapp.OPEN_CLAIM_LINE):
            self.assertIn(text, result)
        self.assertEqual(payload["registry"]["plate"], "MH12AB1234")
        self.assertEqual(payload["document_check"]["missing"], ["driving_licence"])

        # The licence, sent after the result, joins the SAME claim.
        attached = await self.image(jpeg("dl"))
        self.assertEqual(len(db.list_claims()), 1)
        self.assertTrue(attached.startswith(f"Added your driving licence to *{claim_id}*."), attached)
        self.assertIn("✓ Driving licence", attached)
        updated = db.get_claim(claim_id)
        self.assertEqual(updated["document_check"]["missing"], [])
        self.assertEqual(updated["documents_received"]["driving_licence"], 1)
        events = [event["event"] for event in db.claim_events_for(claim_id)]
        self.assertEqual(events, ["submitted", "assessor_assigned", "documents_added"])

        self.assertEqual(await self.say("END"),
                         f"Session closed. Your tracking ID is *{claim_id}* — send *2* any time "
                         "to check it. We will message you here when the decision is made.")
        self.assertEqual(await self.say("2"), whatsapp.TRACK_PROMPT)
        status = await self.say(claim_id.lower())
        for text in (f"*{claim_id}*", "MH12AB1234", "Status:", "Assessor: Meera Kulkarni",
                     "Last update: Documents added", "Next:"):
            self.assertIn(text, status)
        everything = await self.say("ALL")
        self.assertIn(claim_id, everything)


class RegistryGateTests(JourneyBase):
    def plate_with(self, state):
        for record in registry.records():
            if registry.policy_state(record, TODAY) == state:
                return record
        raise AssertionError(state)

    async def test_expired_policy_ends_the_claim_path(self):
        record = self.plate_with("expired")
        await self.say("hi")
        await self.say("1")
        reply = await self.say(record["plate"])
        self.assertIn("expired", reply)
        self.assertIn(record["insurer"], reply)
        self.assertTrue(reply.endswith("Reply *MENU* to start again."))
        self.assertIsNone(whatsapp.get_session(WA_ID).get("stage"))

    async def test_third_party_only_cannot_claim_own_damage(self):
        record = self.plate_with("third_party_only")
        await self.say("hi")
        await self.say("1")
        reply = await self.say(record["plate"])
        self.assertIn("third-party-only policy", reply)
        self.assertIn("does not cover damage to your own vehicle", reply)

    async def test_unknown_plate_is_asked_again(self):
        await self.say("hi")
        await self.say("1")
        self.assertEqual(await self.say("KA01ZZ0001"),
                         "We could not find *KA01ZZ0001* in the registry. Check the number "
                         "and send it again.")
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "ask_plate")

    async def test_bh_series_plates_are_valid(self):
        self.assertTrue(registry.valid_plate("22 BH 1234 AB"))
        self.assertFalse(registry.valid_plate("MH12AB12"))


class MidFlowTests(JourneyBase):
    async def test_a_document_at_the_wrong_step_fills_its_own_slot(self):
        await self.to_step_one()
        self.assertEqual(await self.image(jpeg("rc")),
                         "That looks like your registration certificate — added to step 2. "
                         "Send more damage photos, or reply *DONE*.")
        await self.image(jpeg("car", 3))
        # Step 2 is already filled, so DONE goes straight to step 3.
        self.assertEqual(await self.say("done"), whatsapp.STEP_PROMPTS[3])

    async def test_photos_cannot_be_skipped(self):
        await self.to_step_one()
        self.assertIn("cannot be skipped", await self.say("SKIP"))

    async def test_menu_mid_flow_restarts(self):
        await self.to_step_one()
        await self.image(jpeg("car", 4))
        self.assertEqual(await self.say("menu."), whatsapp.WELCOME)
        self.assertEqual(db.list_session_media(WA_ID), [])
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "menu")

    async def test_end_mid_flow_closes_the_session(self):
        await self.to_step_one()
        self.assertEqual(await self.say("End"), "Session closed. Send any message to start again.")
        self.assertIsNone(whatsapp.get_session(WA_ID).get("stage"))

    async def test_help_lists_the_commands(self):
        await self.to_step_one()
        help_text = await self.say("help?")
        for command in ("MENU", "END", "HELP", "DONE", "SKIP"):
            self.assertIn(command, help_text)
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "collect")

    async def test_session_expiry_says_so_then_shows_start(self):
        await self.to_step_one()
        await self.image(jpeg("car", 5))
        later = time.time() + whatsapp.PENDING_CHOICE_TIMEOUT + 60
        with patch("time.time", return_value=later):
            reply = await self.say("DONE")
        self.assertEqual(reply, "Your session timed out after 30 minutes of inactivity. "
                                "The photos you sent were removed.\n\n" + whatsapp.WELCOME)

    async def test_a_more_info_claim_asks_before_attaching(self):
        await self.to_step_one()
        await self.image(jpeg("car", 6))
        await self.say("done")
        await self.image(jpeg("rc", 6))
        await self.say("skip")
        await self.say("skip")
        claim_id = db.list_claims()[0]["claim_id"]
        await self.say("END")
        db.audit_log_add(claim_id, "assessor", "set_decision", "claim", None,
                         {"value": "more_info"}, "send the licence")
        self.assertEqual(await self.image(jpeg("dl", 6)),
                         f"Add this to *{claim_id}*? Reply *YES* or *NO*.")
        attached = await self.say("yes")
        self.assertTrue(attached.startswith(f"Added your driving licence to *{claim_id}*."), attached)
        self.assertEqual(len(db.list_claims()), 1)

    async def test_tracking_shows_only_this_numbers_claims(self):
        db.save_claim("CLM-OTHER001", {"claim_id": "CLM-OTHER001", "from_number": "919999999999",
                                       "created_at": db.utc_now_iso(), "status": "analysed"})
        await self.say("hi")
        await self.say("2")
        self.assertIn("We could not find *CLM-OTHER001* for this number", await self.say("CLM-OTHER001"))


class RegistryCrossCheckTests(unittest.TestCase):
    RECORD = {"plate": "MH12AB1234", "owner_name": "RAJESH KUMAR SHARMA", "make": "Maruti Suzuki",
              "model": "Swift", "colour": "silver", "body_type": "sedan",
              "policy_type": "comprehensive", "policy_start": "2024-01-01",
              "policy_end": "2026-12-31", "insurer": "The New India Assurance"}
    FIELDS = [{"name": "registration_number_rc", "value": "MH-12-AB-1234", "status": "found"},
              {"name": "owner_name_rc", "value": "R. K. Sharma", "status": "found"},
              {"name": "accident_date", "value": "2024-03-10", "status": "found"}]

    def flags(self, seen, fields=None, record=None):
        return {flag["rule"]: (flag["severity"], flag["message"]) for flag in rules.run_rules(
            fields or self.FIELDS, [], registry=record or self.RECORD, seen=seen)}

    def test_matching_vehicle_raises_nothing(self):
        self.assertEqual(self.flags({"colour": "grey", "make_model": "Maruti Suzuki Swift VXI",
                                     "body_type": "sedan"}), {})

    def test_make_mismatch_is_high_with_both_vehicles_named(self):
        severity, message = self.flags({"colour": "red", "make_model": "Hyundai Creta",
                                        "body_type": "suv"})["registry_vehicle_mismatch"]
        self.assertEqual(severity, "HIGH")
        self.assertEqual(message, "Photos show a red Hyundai Creta; this plate is registered "
                                  "to a silver Maruti Suzuki Swift.")

    def test_colour_alone_is_medium(self):
        severity, _ = self.flags({"colour": "red", "make_model": "Maruti Swift",
                                  "body_type": "sedan"})["registry_vehicle_mismatch"]
        self.assertEqual(severity, "MEDIUM")

    def test_rc_plate_and_owner_against_the_registry(self):
        fields = [{"name": "registration_number_rc", "value": "KA01AB0001", "status": "found"},
                  {"name": "owner_name_rc", "value": "Suresh Verma", "status": "found"}]
        found = self.flags(None, fields=fields)
        self.assertEqual(found["registry_rc_mismatch"][0], "HIGH")
        self.assertEqual(found["registry_owner_mismatch"][0], "HIGH")

    def test_policy_dates_come_from_the_registry(self):
        lapsed = dict(self.RECORD, policy_start="2025-01-01", policy_end="2025-12-31")
        self.assertEqual(self.flags(None, record=lapsed)["policy_not_active"][0], "HIGH")
        third = dict(self.RECORD, policy_type="third_party_only")
        self.assertIn("policy_third_party_only", self.flags(None, record=third))

    def test_two_vehicles_pick_the_registered_one(self):
        mine = {"colour": "silver", "make_model": "Maruti Suzuki Swift", "body_type": "sedan"}
        other = {"colour": "red", "make_model": "Hyundai Creta", "body_type": "suv"}
        self.assertGreater(rules.vehicle_matches_registry(self.RECORD, mine),
                           rules.vehicle_matches_registry(self.RECORD, other))


class TwoVehicleJourneyTests(JourneyBase):
    def assess(self, paths):
        names = [os.path.basename(path) for path in paths]
        looks = [{"colour": "silver", "make_model": "Maruti Suzuki Swift", "body_type": "sedan"},
                 {"colour": "red", "make_model": "Hyundai Creta", "body_type": "suv"}]
        return {"is_vehicle": True, "vehicle_description": "two cars",
                "photos": [dict(looks[index % 2], source_photo=name, is_vehicle=True,
                                vehicle_group=index % 2) for index, name in enumerate(names)],
                "damage": [{"source_photo": name, "part": "rear_bumper", "damage_type": "dent",
                            "severity": "moderate", "confidence": 0.9, "reasoning": "dent"}
                           for name in names]}

    async def test_the_registered_vehicle_is_chosen_without_asking(self):
        await self.to_step_one()
        await self.image(jpeg("car", 7))
        await self.image(jpeg("car2", 8))
        await self.say("done")
        await self.say("skip")
        await self.say("skip")
        result = await self.say("skip")
        payload = db.get_claim(db.list_claims()[0]["claim_id"])
        self.assertEqual(payload["selected_vehicle_group"], 0)
        self.assertNotIn("vehicle_mismatch", [flag["rule"] for flag in payload["flags"]])
        self.assertIn("other_vehicle_in_photos", [note["rule"] for note in payload["notes"]])
        self.assertNotIn("Reply *1*", result)
        self.assertIsNone(whatsapp.get_session(WA_ID).get("pending_choice"))


if __name__ == "__main__":
    unittest.main()
