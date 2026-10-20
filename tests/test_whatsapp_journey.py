"""The guided WhatsApp journey, end to end, with the mock VAHAN registry.

Vision is stubbed by image content (colour), so every step, slot, registry
check, result, open-claim attachment, EXIT, END and tracking reply is asserted.
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
COLOURS = {
    "car": (200, 40, 40), "car2": (40, 40, 200), "rc": (40, 180, 40), "dl": (200, 200, 40),
    "shot": (128, 128, 128), "selfie": (225, 160, 120), "black": (5, 5, 5),
}
# What the stub classifier answers for each kind of image.
CLASSES = {
    "car": ("vehicle_photo", 0.95), "car2": ("vehicle_photo", 0.95), "rc": ("rc", 0.97),
    "dl": ("driving_licence", 0.97), "page": ("claim_form", 0.97), "shot": ("screenshot", 0.95),
    "selfie": ("other", 0.9), "black": ("other", 0.2),
}
F = whatsapp.with_footer


def jpeg(kind, seed=0):
    from PIL import ImageDraw

    image = Image.new("RGB", (320, 240), COLOURS[kind])
    if kind not in ("black", "shot"):
        draw = ImageDraw.Draw(image)
        draw.rectangle([20 + seed * 37 % 200, 30, 120 + seed * 37 % 200, 120],
                       fill=tuple(max(0, value - 60) for value in COLOURS[kind]))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def pdf(pages=("page",)):
    """One page per kind: "page" is a plain claim-form page; "rc" and "dl"
    pages are tinted so the stub classifier tells them apart."""
    document = fitz.open()
    for number, kind in enumerate(pages, 1):
        page = document.new_page()
        if kind != "page":
            colour = tuple(value / 255 for value in COLOURS[kind])
            page.draw_rect(page.rect, color=None, fill=colour)
        page.insert_text((72, 72), f"MOTOR CLAIM BUNDLE page {number}")
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
    seen = {"colour": "silver", "make_model": "Maruti Suzuki Swift", "body_type": "sedan",
            "make_confidence": 0.9}

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
        doc_type, confidence = CLASSES[kind_of(path)]
        return {"doc_type": doc_type, "confidence": confidence, "number_plate": None}

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

    async def document(self, data, filename="Claim_Form.pdf"):
        self.counter += 1
        key = f"doc{self.counter}"
        self.media[key] = data
        await main.handle_wa_message({"from": WA_ID, "id": f"m{self.counter}",
                                      "timestamp": str(int(time.time())), "type": "document",
                                      "document": {"id": key, "mime_type": "application/pdf",
                                                   "filename": filename}})
        return self.sent[-1]

    async def kind(self, message_type):
        self.counter += 1
        await main.handle_wa_message({"from": WA_ID, "id": f"m{self.counter}",
                                      "timestamp": str(int(time.time())), "type": message_type,
                                      message_type: {"id": "x"}})
        return self.sent[-1]

    async def to_step_one(self, plate="MH12AB1234"):
        await self.say("hi")
        await self.say("1")
        await self.say(plate)
        return await self.say("YES")

    async def submitted_claim(self):
        await self.to_step_one()
        await self.image(jpeg("car", 6))
        await self.say("done")
        await self.image(jpeg("rc", 6))
        await self.say("skip")
        await self.say("skip")
        await self.say("submit")
        return db.list_claims()[0]["claim_id"]


class FullJourneyTests(JourneyBase):
    async def test_the_full_journey(self):
        self.assertEqual(await self.say("hello"), F(whatsapp.WELCOME))
        self.assertEqual(await self.say("1"), F(whatsapp.PLATE_PROMPT))
        self.assertEqual(await self.say("MH-12-XYZ"),
                         F("That is not a valid registration number. " + whatsapp.PLATE_PROMPT))
        summary = await self.say("mh 12 ab 1234")
        for text in ("*Vehicle found: MH12AB1234*", "Silver Maruti Suzuki Swift VXI (sedan)",
                     "Registered owner: R*** K*** Sharma", "Insurer: The New India Assurance",
                     "Policy: ****8271, valid until 31 Dec 2026",
                     "✓ This WhatsApp number matches the registered mobile",
                     whatsapp.CONFIRM_PROMPT, whatsapp.FOOTER):
            self.assertIn(text, summary)
        self.assertEqual(await self.say("Yes"), F(whatsapp.STEP_PROMPTS[1]))
        self.assertEqual(await self.image(jpeg("car", 1)),
                         F("Photo received (1 total). Reply DONE when you are done."))
        self.assertEqual(await self.image(jpeg("car", 2)),
                         F("Photo received (2 total). Reply DONE when you are done."))
        self.assertEqual(await self.say("DONE"), F(whatsapp.STEP_PROMPTS[2]))
        self.assertEqual(await self.image(jpeg("rc")),
                         F("Registration certificate received.\n\n" + whatsapp.STEP_PROMPTS[3]))
        self.assertEqual(await self.say("skip"), F(whatsapp.STEP_PROMPTS[4]))
        ready = await self.document(pdf())
        self.assertEqual(ready, F(
            "Claim form received.\n\n*Ready to submit*\n"
            "✓ Damage photos (2) · ✓ RC · – Licence (skipped) · ✓ Claim form\n"
            "Reply *SUBMIT* to file the claim, or send anything missing."))
        self.assertEqual(db.list_claims(), [])  # nothing is analysed before SUBMIT
        before = len(self.sent)
        result = await self.say("SUBMIT")
        self.assertEqual(self.sent[before], whatsapp.ANALYSING_REPLY)
        claims = db.list_claims()
        self.assertEqual(len(claims), 1)
        claim_id = claims[0]["claim_id"]
        payload = db.get_claim(claim_id)
        self.assertEqual(result, F(whatsapp.format_reply(payload)))
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
        events = [event["event"] for event in db.claim_events_for(claim_id)]
        self.assertEqual(events, ["submitted", "assessor_assigned", "documents_added"])

        self.assertEqual(await self.say("END"),
                         f"Session closed. Your tracking ID is *{claim_id}* — send *2* any time "
                         "to check it. We will message you here when the decision is made.")
        self.assertEqual(await self.say("2"), F(whatsapp.TRACK_PROMPT))
        status = await self.say(claim_id.lower())
        for text in (f"*{claim_id}*", "MH12AB1234", "Status:", "Assessor: Meera Kulkarni",
                     "Last update: Documents added", "Next:"):
            self.assertIn(text, status)
        self.assertIn(claim_id, await self.say("ALL"))


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
                         F("We could not find *KA01ZZ0001* in the registry. Check the number "
                           "and send it again."))
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "ask_plate")

    async def test_bh_series_plates_are_valid(self):
        self.assertTrue(registry.valid_plate("22 BH 1234 AB"))
        self.assertFalse(registry.valid_plate("MH12AB12"))


# 1 ------------------------------------------------------------------------
class ExitTests(JourneyBase):
    async def test_exit_before_submission_counts_what_is_discarded(self):
        await self.to_step_one()
        await self.image(jpeg("car", 1))
        await self.image(jpeg("car", 2))
        await self.say("done")
        await self.image(jpeg("rc"))
        self.assertEqual(await self.say("cancel"),
                         "Exit this claim? The 2 photos and 1 document you sent will be discarded. "
                         "Reply *YES* to exit or *NO* to continue.")
        # NO returns to the exact prompt.
        self.assertEqual(await self.say("no"), F(whatsapp.STEP_PROMPTS[3]))
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "collect")
        await self.say("EXIT")
        reply = await self.say("haan")
        self.assertEqual(reply, F("Claim cancelled; nothing was submitted.\n\n" + whatsapp.WELCOME))
        self.assertEqual(db.list_session_media(WA_ID), [])
        self.assertEqual(db.list_claims(), [])

    async def test_exit_after_submission_never_discards_the_claim(self):
        claim_id = await self.submitted_claim()
        self.assertEqual(await self.say("quit"),
                         f"Your claim *{claim_id}* stays submitted — reply *2* any time to track "
                         "it. End this session? *YES* / *NO*")
        self.assertIn("is open", await self.say("nope"))
        await self.say("stop")
        self.assertIn(f"Your tracking ID is *{claim_id}*", await self.say("y"))
        self.assertEqual(len(db.list_claims()), 1)

    async def test_exit_during_analysis_does_not_cancel(self):
        whatsapp.ANALYSING_IDS.add(WA_ID)
        try:
            self.assertEqual(await self.say("exit"), whatsapp.ANALYSING_EXIT_REPLY)
        finally:
            whatsapp.ANALYSING_IDS.discard(WA_ID)

    async def test_every_waiting_prompt_has_the_footer(self):
        for reply in (await self.say("hi"), await self.say("1"), await self.say("MH12AB1234"),
                      await self.say("yes"), await self.image(jpeg("car")), await self.say("done")):
            self.assertTrue(reply.endswith(whatsapp.FOOTER), reply)


# 2 ------------------------------------------------------------------------
class GreetingTests(JourneyBase):
    async def test_greetings_mid_flow_restate_the_step(self):
        await self.to_step_one()
        await self.image(jpeg("car"))
        await self.say("done")
        self.assertEqual(await self.say("Hello!"), F(whatsapp.STEP_PROMPTS[2]))
        self.assertEqual(await self.say("namaste"), F(whatsapp.STEP_PROMPTS[2]))

    async def test_a_greeting_is_never_a_plate(self):
        await self.say("hii")
        await self.say("1")
        self.assertEqual(await self.say("hey"), F(whatsapp.PLATE_PROMPT))

    async def test_tracking_shows_the_options_after_one_miss(self):
        await self.say("hi")
        await self.say("2")
        self.assertEqual(await self.say("hello"), F(whatsapp.TRACK_PROMPT))
        # A word is never "could not find"; an ID-like miss is, once.
        self.assertEqual(await self.say("banana"),
                         F(f"{whatsapp.NOT_A_TRACKING_ID} {whatsapp.TRACK_PROMPT}"))
        self.assertEqual(await self.say("status"), F(whatsapp.TRACK_PROMPT))
        self.assertIn("We could not find *CLM-12345678*", await self.say("CLM-12345678"))
        self.assertEqual(await self.say("clm 8765 4321"), F(whatsapp.TRACK_PROMPT))

    async def test_a_tracking_id_in_lowercase_with_spaces(self):
        claim_id = await self.submitted_claim()
        await self.say("END")
        await self.say("2")
        spaced = " ".join([claim_id[:3].lower(), claim_id[4:8].lower(), claim_id[8:].lower()])
        self.assertIn(f"*{claim_id}*", await self.say(spaced))


# 3 and 4 ------------------------------------------------------------------
class ClassificationTests(JourneyBase):
    async def test_a_screenshot_is_refused_and_not_stored(self):
        await self.to_step_one()
        self.assertEqual(await self.image(jpeg("shot")),
                         F(whatsapp.SCREENSHOT_REPLY + "\n\n" + whatsapp.STEP_PROMPTS[1]))
        self.assertEqual(db.list_session_media(WA_ID), [])

    async def test_an_unrecognised_photo_fills_no_slot(self):
        await self.to_step_one()
        for kind in ("selfie", "black"):
            self.assertEqual(await self.image(jpeg(kind)), F(
                "That does not look like a damage photo. Please send a photo of the damage to "
                "your vehicle.\n\n" + whatsapp.STEP_PROMPTS[1]))
        self.assertEqual(db.list_session_media(WA_ID), [])
        await self.image(jpeg("car"))
        await self.say("done")
        self.assertEqual(await self.image(jpeg("selfie")), F(
            "That does not look like a registration certificate. Please send a photo of the RC, "
            "or reply *SKIP*.\n\n" + whatsapp.STEP_PROMPTS[2]))

    async def test_a_random_pdf_is_refused(self):
        await self.to_step_one()
        await self.image(jpeg("car"))
        await self.say("done")
        self.assertIn("That does not look like a registration certificate",
                      await self.document(pdf(("selfie",)), "holiday.pdf"))
        self.assertEqual(sum(1 for row in db.list_session_media(WA_ID)
                             if row["kind"] == "document_page"), 0)


# 5 ------------------------------------------------------------------------
class SubmitTests(JourneyBase):
    async def test_nothing_is_analysed_before_submit_and_late_items_update_the_summary(self):
        await self.to_step_one()
        await self.image(jpeg("car"))
        await self.say("done")
        await self.say("skip")
        await self.say("skip")
        summary = await self.say("skip")
        self.assertIn("✓ Damage photos (1) · – RC (skipped) · – Licence (skipped) · "
                      "– Claim form (skipped)", summary)
        self.assertEqual(db.list_claims(), [])
        updated = await self.image(jpeg("dl"))
        self.assertIn("Driving licence received.", updated)
        self.assertIn("✓ Licence", updated)
        self.assertEqual(await self.say("hello"), F(whatsapp.ready_summary(WA_ID)))
        await self.say("submit")
        self.assertEqual(len(db.list_claims()), 1)


# 6 ------------------------------------------------------------------------
class MultiDocumentPdfTests(JourneyBase):
    async def test_one_pdf_fills_every_slot_it_covers(self):
        await self.to_step_one()
        await self.image(jpeg("car"))
        await self.say("done")
        reply = await self.document(pdf(("page", "rc", "dl")), "Motor_Bundle.pdf")
        self.assertTrue(reply.startswith(
            "That PDF filled: claim form, registration certificate and driving licence."), reply)
        self.assertIn("*Ready to submit*", reply)
        self.assertIn("✓ RC · ✓ Licence · ✓ Claim form", reply)


# 7 ------------------------------------------------------------------------
class MakeConfidenceTests(unittest.TestCase):
    RECORD = {"plate": "MH12AB1234", "make": "Maruti Suzuki", "model": "Swift",
              "colour": "white", "body_type": "sedan"}

    def test_make_alone_is_medium_and_says_it_was_not_confirmed(self):
        severity, message = rules.appearance_mismatch(
            self.RECORD, {"colour": "white", "make_model": "Subaru Impreza", "body_type": "sedan"})
        self.assertEqual(severity, "MEDIUM")
        self.assertTrue(message.startswith("Vehicle make could not be confirmed from photos"))

    def test_make_and_body_together_are_high(self):
        severity, _ = rules.appearance_mismatch(
            self.RECORD, {"colour": "white", "make_model": "Subaru Forester", "body_type": "suv"})
        self.assertEqual(severity, "HIGH")

    def test_an_unconfirmed_make_is_never_shown(self):
        photo = {"source_photo": "a.jpg", "is_vehicle": True, "vehicle_group": 0,
                 "colour": "white", "make_model": "Subaru Impreza", "body_type": "sedan"}
        unsure = main.build_vehicle_groups([dict(photo, make_confidence=0.5)], [], [])
        sure = main.build_vehicle_groups([dict(photo, make_confidence=0.85)], [], [])
        self.assertEqual(unsure[0]["label"], "White Sedan")
        self.assertIn("Subaru", sure[0]["label"])


class MidFlowTests(JourneyBase):
    async def test_a_document_at_the_wrong_step_fills_its_own_slot(self):
        await self.to_step_one()
        self.assertEqual(await self.image(jpeg("rc")),
                         F("That looks like your registration certificate — added to step 2. "
                           "Send more damage photos, or reply *DONE*."))
        await self.image(jpeg("car", 3))
        self.assertEqual(await self.say("done"), F(whatsapp.STEP_PROMPTS[3]))

    async def test_photos_cannot_be_skipped(self):
        await self.to_step_one()
        self.assertIn("cannot be skipped", await self.say("SKIP"))

    async def test_menu_mid_flow_asks_before_discarding(self):
        await self.to_step_one()
        await self.image(jpeg("car", 4))
        self.assertEqual(await self.say("menu."),
                         "Exit this claim? The 1 photo and 0 documents you sent will be "
                         "discarded. Reply *YES* to exit or *NO* to continue.")
        self.assertEqual(len(db.list_session_media(WA_ID)), 1)
        self.assertEqual(await self.say("yes"),
                         F("Claim cancelled; nothing was submitted.\n\n" + whatsapp.WELCOME))
        self.assertEqual(db.list_session_media(WA_ID), [])
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "menu")

    async def test_menu_with_nothing_sent_goes_straight_to_the_menu(self):
        await self.to_step_one()
        self.assertEqual(await self.say("MENU"), F(whatsapp.WELCOME))

    async def test_end_mid_flow_closes_the_session(self):
        await self.to_step_one()
        self.assertEqual(await self.say("End"), "Session closed. Send any message to start again.")
        self.assertIsNone(whatsapp.get_session(WA_ID).get("stage"))

    async def test_help_lists_the_commands(self):
        await self.to_step_one()
        help_text = await self.say("help?")
        for command in ("MENU", "EXIT", "END", "HELP", "DONE", "SKIP", "SUBMIT"):
            self.assertIn(command, help_text)
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "collect")

    async def test_track_typed_mid_claim_explains(self):
        await self.to_step_one()
        reply = await self.say("status")
        self.assertTrue(reply.startswith(whatsapp.MID_CLAIM_TRACK), reply)
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "collect")

    async def test_unsupported_types_restate_the_step(self):
        await self.to_step_one()
        for message_type in ("video", "audio", "sticker", "location", "contacts"):
            reply = await self.kind(message_type)
            self.assertIn("not accepted", reply)
            self.assertIn(whatsapp.STEP_PROMPTS[1], reply)

    async def test_session_expiry_says_so_then_shows_start(self):
        await self.to_step_one()
        await self.image(jpeg("car", 5))
        later = time.time() + whatsapp.PENDING_CHOICE_TIMEOUT + 60
        with patch("time.time", return_value=later):
            reply = await self.say("DONE")
        self.assertEqual(reply, F("Your session timed out after 30 minutes of inactivity. "
                                  "The photos you sent were removed.\n\n" + whatsapp.WELCOME))

    async def test_a_more_info_claim_asks_before_attaching(self):
        claim_id = await self.submitted_claim()
        await self.say("END")
        db.audit_log_add(claim_id, "assessor", "set_decision", "claim", None,
                         {"value": "more_info"}, "send the licence")
        self.assertEqual(await self.image(jpeg("dl", 6)),
                         F(f"Add this to *{claim_id}*? Reply *YES* or *NO*."))
        # A greeting restates the question, never the menu.
        self.assertEqual(await self.say("hi"), F(f"Add this to *{claim_id}*? Reply *YES* or *NO*."))
        attached = await self.say("yes")
        self.assertTrue(attached.startswith(f"Added your driving licence to *{claim_id}*."), attached)
        self.assertEqual(len(db.list_claims()), 1)

    async def test_two_claims_back_to_back(self):
        first = await self.submitted_claim()
        await self.say("1")
        await self.say("MH12AB1234")
        await self.say("yes")
        await self.image(jpeg("car", 9))
        await self.say("done")
        await self.say("skip")
        await self.say("skip")
        await self.say("skip")
        await self.say("submit")
        claims = [claim["claim_id"] for claim in db.list_claims()]
        self.assertEqual(len(claims), 2)
        self.assertIn(first, claims)

    async def test_tracking_shows_only_this_numbers_claims(self):
        db.save_claim("CLM-0DD00001", {"claim_id": "CLM-0DD00001", "from_number": "919999999999",
                                       "created_at": db.utc_now_iso(), "status": "analysed"})
        await self.say("hi")
        await self.say("2")
        self.assertIn("We could not find *CLM-0DD00001* for this number",
                      await self.say("CLM-0DD00001"))


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

    def test_make_and_body_mismatch_is_high_with_both_vehicles_named(self):
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
        looks = [{"colour": "silver", "make_model": "Maruti Suzuki Swift", "body_type": "sedan",
                  "make_confidence": 0.9},
                 {"colour": "red", "make_model": "Hyundai Creta", "body_type": "suv",
                  "make_confidence": 0.9}]
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
        await self.say("skip")
        result = await self.say("submit")
        payload = db.get_claim(db.list_claims()[0]["claim_id"])
        self.assertEqual(payload["selected_vehicle_group"], 0)
        self.assertNotIn("vehicle_mismatch", [flag["rule"] for flag in payload["flags"]])
        self.assertIn("other_vehicle_in_photos", [note["rule"] for note in payload["notes"]])
        self.assertNotIn("Reply *1*", result)
        self.assertIsNone(whatsapp.get_session(WA_ID).get("pending_choice"))


# 8: what the adversarial sweep found -------------------------------------------
class SweepFixTests(JourneyBase):
    async def test_a_photo_at_the_plate_prompt_is_kept_for_step_one(self):
        await self.say("hi")
        await self.say("1")
        self.assertEqual(await self.image(jpeg("car", 1)),
                         F(whatsapp.KEPT_FOR_CLAIM + "\n\n" + whatsapp.PLATE_PROMPT))
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "ask_plate")
        await self.say("MH12AB1234")
        self.assertIn("You have already sent 1 photo", await self.say("yes"))

    async def test_media_sent_at_start_is_kept_when_the_claim_begins(self):
        self.assertEqual(await self.image(jpeg("car", 2)),
                         F(whatsapp.KEPT_FOR_CLAIM + "\n\n" + whatsapp.WELCOME))
        await self.say("1")
        await self.say("MH12AB1234")
        self.assertIn("You have already sent 1 photo", await self.say("yes"))
        self.assertEqual(len(db.list_session_media(WA_ID)), 1)

    async def test_exit_and_end_at_the_menu_ask_when_something_was_kept(self):
        await self.image(jpeg("car", 3))
        self.assertTrue((await self.say("exit")).startswith("Exit this claim? The 1 photo"))
        self.assertEqual(await self.say("no"), F(whatsapp.WELCOME))
        self.assertTrue((await self.say("END")).startswith("Exit this claim? The 1 photo"))
        self.assertEqual(len(db.list_session_media(WA_ID)), 1)

    async def test_exit_with_nothing_sent_does_not_count_zeroes(self):
        await self.say("hi")
        await self.say("1")
        self.assertEqual(await self.say("exit"),
                         "Exit this claim? Reply *YES* to exit or *NO* to continue.")

    async def test_media_after_exit_means_carrying_on(self):
        await self.to_step_one()
        await self.say("exit")
        await self.image(jpeg("car", 4))
        self.assertFalse(whatsapp.get_session(WA_ID).get("exit_pending"))
        self.assertEqual(await self.say("yes"), F(whatsapp.STEP_PROMPTS[1]))

    async def test_media_during_a_guided_analysis_is_refused_once(self):
        await self.to_step_one()
        await self.image(jpeg("car", 5))
        await self.say("done")
        await self.say("skip")
        await self.say("skip")
        await self.say("skip")
        rows = len(db.list_session_media(WA_ID))
        whatsapp.ANALYSING_IDS.add(WA_ID)
        try:
            self.assertEqual(await self.image(jpeg("rc", 5)), whatsapp.GUIDED_BUSY_MEDIA)
            before = len(self.sent)
            await self.image(jpeg("dl", 5))
            self.assertEqual(len(self.sent), before)  # one notice per analysis
        finally:
            whatsapp.ANALYSING_IDS.discard(WA_ID)
        self.assertEqual(len(db.list_session_media(WA_ID)), rows)

    async def test_a_resend_says_so_and_restates_the_step(self):
        await self.to_step_one()
        await self.image(jpeg("car", 6))
        await self.say("done")
        await self.image(jpeg("rc", 6))
        self.assertEqual(await self.image(jpeg("rc", 6)), F(
            "You already sent that registration certificate.\n\n" + whatsapp.STEP_PROMPTS[3]))

    async def test_a_document_already_on_the_claim_is_not_attached_again(self):
        claim_id = await self.submitted_claim()  # its RC is jpeg("rc", 6)
        reply = await self.image(jpeg("rc", 6))
        self.assertEqual(reply, F(f"That registration certificate is already on *{claim_id}*.\n\n"
                                  f"Your claim *{claim_id}* is open. {whatsapp.OPEN_CLAIM_LINE}"))
        events = [event["event"] for event in db.claim_events_for(claim_id)]
        self.assertNotIn("documents_added", events)
        self.assertTrue((await self.image(jpeg("dl", 6))).startswith("Added your driving licence"))
        self.assertIn("is already on", await self.image(jpeg("dl", 6)))

    async def test_submit_before_the_last_step_shows_what_is_missing(self):
        await self.to_step_one()
        self.assertIn("Send at least one photo", await self.say("submit"))
        await self.image(jpeg("car", 7))
        await self.say("done")
        summary = await self.say("SUBMIT")
        self.assertIn("✓ Damage photos (1) · ✗ RC · ✗ Licence · ✗ Claim form", summary)
        self.assertEqual(db.list_claims(), [])
        await self.say("submit")
        self.assertEqual(len(db.list_claims()), 1)

    async def test_a_tracking_id_works_at_the_menu_and_on_an_open_claim(self):
        claim_id = await self.submitted_claim()
        self.assertIn("Status:", await self.say(claim_id.lower()))  # open claim
        self.assertEqual(whatsapp.get_session(WA_ID).get("stage"), "open_claim")
        await self.say("END")
        await self.say("hi")
        self.assertIn(f"*{claim_id}*", await self.say(claim_id))  # at the menu
        await self.say("1")
        await self.say("MH12AB1234")
        await self.say("yes")
        self.assertTrue((await self.say(claim_id)).startswith(whatsapp.MID_CLAIM_TRACK))

    async def test_a_session_from_before_this_deploy_restates_its_step(self):
        await self.to_step_one()
        await self.image(jpeg("car", 9))
        await self.say("done")
        whatsapp.get_session(WA_ID).update(last_prompt=None)  # no last_prompt yet
        self.assertEqual(await self.say("hello"), F(whatsapp.STEP_PROMPTS[2]))

    async def test_exit_at_the_attach_question_names_the_claim(self):
        claim_id = await self.submitted_claim()
        await self.say("END")
        db.audit_log_add(claim_id, "assessor", "set_decision", "claim", None,
                         {"value": "more_info"}, "send the licence")
        await self.image(jpeg("dl", 8))
        self.assertTrue((await self.say("exit")).startswith(f"Your claim *{claim_id}* stays "
                                                             "submitted"))


if __name__ == "__main__":
    unittest.main()
