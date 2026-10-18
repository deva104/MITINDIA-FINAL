try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import db
import gemini
import main
import whatsapp

try:
    from helpers import seed_session
except ImportError:
    from tests.helpers import seed_session
from rules import run_rules


FIELD_NAMES = [
    "insured_name_claim_form",
    "owner_name_rc",
    "holder_name_dl",
    "registration_number_claim_form",
    "registration_number_rc",
    "chassis_number",
    "engine_number",
    "vehicle_make_model",
    "dl_number",
    "dl_expiry_date",
    "policy_number",
    "policy_start_date",
    "policy_end_date",
    "accident_date",
    "accident_time",
    "accident_location",
    "accident_description",
]


class FakeConnection:
    def close(self):
        pass


def found_fields():
    values = {
        "registration_number_claim_form": "MH-12-AB-1234",
        "registration_number_rc": "MH-12-AB-1234",
    }
    return [
        {
            "name": name,
            "value": values.get(name, f"value-{name}"),
            "status": "found",
            "confidence": 0.99,
            "source_doc": "bundle.pdf",
        }
        for name in FIELD_NAMES
    ]


def damage_result(number_plate=None):
    return {
        "is_vehicle": True,
        "vehicle_description": "White sedan",
        "photos": [{
            "source_photo": "damage.jpg",
            "is_vehicle": True,
            "vehicle_group": 0,
            "make_model": "Kia",
            "colour": "white",
            "body_type": "sedan",
            "description": "Damaged car",
        }],
        "damage": [{
            "part": "front_bumper",
            "damage_type": "dent",
            "severity": "moderate",
            "confidence": 0.9,
            "source_photo": "damage.jpg",
        }],
        "error": None,
    }


class ClassificationTests(unittest.TestCase):
    def test_classify_image_normalises_vehicle_plate(self):
        with patch.object(
            gemini, "_image_part", return_value=("image", b"bytes", "image/jpeg")
        ), patch.object(gemini, "_generate", return_value={
            "doc_type": "vehicle_photo",
            "confidence": "0.94",
            "number_plate": "MH-12-AB-1234",
        }) as generate:
            result = gemini.classify_image("damage.jpg")

        self.assertEqual(result, {
            "doc_type": "vehicle_photo",
            "confidence": 0.94,
            "number_plate": "MH-12-AB-1234",
        })
        self.assertEqual(
            sum(part[0] == "image" for part in generate.call_args.args[0]), 1
        )

    def test_pdf_skips_classification(self):
        with patch.object(gemini, "_generate") as generate:
            result = gemini.classify_image("bundle.pdf")
        generate.assert_not_called()
        self.assertEqual(result["doc_type"], "other_document")


class PipelineRoutingTests(unittest.TestCase):
    def analyse(self, classifications, photos=None, docs=None, fields=None,
                forced_review_reasons=None):
        extracted_from = []
        assessed_from = []

        def fake_extract(paths):
            extracted_from.extend(paths)
            return list(fields or [])

        def fake_assess(paths):
            assessed_from.extend(paths)
            return damage_result()

        with patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: [],
            build_photo_url_map=lambda paths: {
                os.path.basename(path).lower(): f"/media/{os.path.basename(path)}"
                for path in paths
            },
            classify_image=lambda path: classifications[os.path.basename(path)],
            extract_fields=fake_extract,
            assess_damage=fake_assess,
            run_deep_forensics=lambda paths, conn, fields=None: [],
            save_claim=lambda claim_id, payload: None,
        ):
            payload = asyncio.run(main.analyse_claim(
                "TEST-INTAKE",
                docs or [],
                photos or [],
                forced_review_reasons=forced_review_reasons,
            ))
        return payload, extracted_from, assessed_from

    def test_routes_four_whatsapp_images_and_extracts_all_fields(self):
        classifications = {
            "damage.jpg": {
                "doc_type": "vehicle_photo", "confidence": 0.98,
                "number_plate": "MH-12-AB-1234",
            },
            "rc.jpg": {
                "doc_type": "rc", "confidence": 0.98, "number_plate": None,
            },
            "dl.jpg": {
                "doc_type": "driving_licence", "confidence": 0.98,
                "number_plate": None,
            },
            "claim.jpg": {
                "doc_type": "claim_form", "confidence": 0.98,
                "number_plate": None,
            },
        }
        payload, extracted, assessed = self.analyse(
            classifications,
            photos=["damage.jpg", "rc.jpg", "dl.jpg", "claim.jpg"],
            fields=found_fields(),
        )

        self.assertEqual(assessed, ["damage.jpg"])
        self.assertEqual(extracted, ["rc.jpg", "dl.jpg", "claim.jpg"])
        self.assertEqual(len(payload["fields"]), 17)
        self.assertEqual(payload["documents_received"]["rc"], 1)
        self.assertEqual(payload["documents_received"]["driving_licence"], 1)
        self.assertEqual(payload["documents_received"]["claim_form"], 1)
        self.assertIn(
            "stage0_5_classify",
            {entry["stage"] for entry in payload["timings"]},
        )

    def test_low_confidence_is_kept_and_reported(self):
        payload, _, assessed = self.analyse(
            {"unclear.jpg": {
                "doc_type": "other", "confidence": 0.42, "number_plate": None,
            }},
            photos=["unclear.jpg"],
        )
        self.assertEqual(assessed, ["unclear.jpg"])
        self.assertTrue(any(
            item["item"] == "unclear.jpg"
            and "low classification confidence" in item["reason"]
            for item in payload["undetermined"]
        ))

    def test_forced_missing_documents_requires_review(self):
        payload, _, _ = self.analyse(
            {"damage.jpg": {
                "doc_type": "vehicle_photo", "confidence": 0.99,
                "number_plate": None,
            }},
            photos=["damage.jpg"],
            forced_review_reasons=[
                "documents not provided: driving licence, claim form"
            ],
        )
        self.assertEqual(payload["decision"], "needs_review")
        self.assertIn(
            "documents not provided: driving licence, claim form",
            payload["decision_reasons"],
        )

    def test_ten_images_are_capped_at_eight(self):
        paths = [f"damage-{index}.jpg" for index in range(10)]
        classifications = {
            os.path.basename(path): {
                "doc_type": "vehicle_photo",
                "confidence": 0.99,
                "number_plate": None,
            }
            for path in paths
        }
        payload, _, assessed = self.analyse(
            classifications, photos=paths
        )
        self.assertEqual(len(assessed), 8)
        self.assertEqual(
            payload["intake_excluded"], ["damage-8.jpg", "damage-9.jpg"]
        )

    def test_vehicle_identity_mismatch(self):
        flags = run_rules(
            [{
                "name": "registration_number_rc",
                "value": "MH-12-AB-1234",
            }],
            [],
            photos=[{
                "source_photo": "damage.jpg",
                "number_plate": "KA 01 ZZ 9999",
            }],
        )
        flag = next(
            item for item in flags
            if item["rule"] == "vehicle_identity_mismatch"
        )
        self.assertEqual(flag["severity"], "HIGH")
        self.assertIn("KA 01 ZZ 9999", flag["message"])

    def test_pipeline_vehicle_plate_mismatch_fires(self):
        fields = [{
            "name": "registration_number_rc",
            "value": "MH-12-AB-1234",
            "status": "found",
            "confidence": 0.99,
            "source_doc": "rc.jpg",
        }]
        classifications = {
            "damage.jpg": {
                "doc_type": "vehicle_photo", "confidence": 0.99,
                "number_plate": "KA-01-ZZ-9999",
            },
            "rc.jpg": {
                "doc_type": "rc", "confidence": 0.99, "number_plate": None,
            },
        }
        payload, _, _ = self.analyse(
            classifications,
            photos=["damage.jpg", "rc.jpg"],
            fields=fields,
        )
        self.assertIn(
            "vehicle_identity_mismatch",
            {flag["rule"] for flag in payload["flags"]},
        )


class WhatsAppChecklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "checklist.db")
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()

    def tearDown(self):
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()
        db.DB_PATH = self.original_db
        self.folder.cleanup()

    async def test_damage_only_done_then_skip(self):
        seed_session("user", photos=2, updated=time.time())
        intake = {
            "classifications": [],
            "documents_received": {
                "rc": 0, "driving_licence": 0, "claim_form": 0, "policy": 0,
            },
            "vehicle_count": 2,
            "missing": ["rc", "driving_licence", "claim_form"],
        }
        sent = AsyncMock()
        with patch.object(
            whatsapp, "classify_session_intake", return_value=intake
        ), patch.object(whatsapp, "send_text", sent):
            await whatsapp.process_session("user")

        expected = "\n".join([
            "*Almost there — documents needed*",
            "",
            "Received:",
            "✓ Damage photos (2)",
            "",
            "Still needed:",
            "- Registration certificate",
            "- Driving licence",
            "- Claim form",
            "",
            (
                "Send them here. Or reply *SKIP* to submit now — your claim "
                "will go to an assessor, who will request them."
            ),
        ])
        sent.assert_awaited_once_with("user", expected)
        self.assertTrue(whatsapp.SESSIONS["user"]["awaiting_docs"])

        result = {
            "claim_id": "CLM-SKIP",
            "status": "analysed",
            "stopped_at": None,
            "vehicles": [],
            "claimable_count": 0,
            "damage": [],
            "estimate": {},
            "flags": [],
            "decision": "needs_review",
            "decision_reasons": [
                "documents not provided: registration certificate, "
                "driving licence, claim form"
            ],
        }
        analyse = AsyncMock(return_value=result)
        sent.reset_mock()
        with patch.object(
            whatsapp, "classify_session_intake", return_value=intake
        ), patch.object(whatsapp, "send_text", sent), patch.object(
            main, "analyse_claim", analyse
        ), patch("db.save_file"):
            await whatsapp.process_session("user", skip_missing=True)

        self.assertEqual(
            analyse.await_args.kwargs["forced_review_reasons"],
            [
                "documents not provided: registration certificate, "
                "driving licence, claim form"
            ],
        )

    async def test_rc_only_refuses_skip(self):
        seed_session("user", photos=1, awaiting_docs=True, updated=time.time())
        intake = {
            "classifications": [],
            "documents_received": {
                "rc": 1, "driving_licence": 0, "claim_form": 0, "policy": 0,
            },
            "vehicle_count": 0,
            "missing": ["driving_licence", "claim_form"],
        }
        sent = AsyncMock()
        with patch.object(
            whatsapp, "classify_session_intake", return_value=intake
        ), patch.object(whatsapp, "send_text", sent):
            await whatsapp.process_session("user", skip_missing=True)

        sent.assert_awaited_once_with("user", "\n".join([
            "*Vehicle photo needed*",
            "",
            "I received your documents, but no vehicle photo was found.",
            "Send at least one clear photo of the damaged vehicle.",
            "SKIP is not available until a vehicle photo is provided.",
        ]))
        self.assertIn("user", whatsapp.SESSIONS)

    async def test_strict_docs_does_not_offer_skip(self):
        seed_session("user", photos=1, awaiting_docs=True, updated=time.time())
        intake = {
            "classifications": [],
            "documents_received": {
                "rc": 0, "driving_licence": 0, "claim_form": 0, "policy": 0,
            },
            "vehicle_count": 1,
            "missing": ["rc", "driving_licence", "claim_form"],
        }
        sent = AsyncMock()
        analyse = AsyncMock()
        with patch.dict(os.environ, {"STRICT_DOCS": "true"}), patch.object(
            whatsapp, "classify_session_intake", return_value=intake
        ), patch.object(whatsapp, "send_text", sent), patch.object(
            main, "analyse_claim", analyse
        ):
            await whatsapp.process_session("user", skip_missing=True)

        analyse.assert_not_awaited()
        self.assertNotIn("SKIP", sent.await_args.args[1])

    def test_stale_awaiting_docs_session_expires(self):
        seed_session(
            "user", photos=1, awaiting_docs=True,
            updated=time.time() - whatsapp.PENDING_CHOICE_TIMEOUT - 1,
        )
        session = whatsapp.get_session("user")
        self.assertFalse(session["awaiting_docs"])
        self.assertEqual(session["photos"], [])

    async def test_pdf_bundle_satisfies_checklist(self):
        seed_session("user", photos=1, documents=1, updated=time.time())
        result = {
            "claim_id": "CLM-PDF",
            "status": "analysed",
            "stopped_at": None,
            "vehicles": [],
            "claimable_count": 0,
            "damage": [],
            "estimate": {},
            "flags": [],
        }
        sent = AsyncMock()
        analyse = AsyncMock(return_value=result)
        with patch.object(whatsapp, "classify_image", return_value={
            "doc_type": "vehicle_photo",
            "confidence": 0.99,
            "number_plate": None,
        }), patch.object(
            whatsapp, "extract_fields", return_value=found_fields()
        ), patch.object(whatsapp, "send_text", sent), patch.object(
            main, "analyse_claim", analyse
        ), patch("db.save_file"):
            await whatsapp.process_session("user")

        analyse.assert_awaited_once()
        self.assertEqual(sent.await_args_list[0].args[1], (
            "Analysing your claim. This takes a few seconds."
        ))

    async def test_image_limit_replies_once_and_keeps_eight(self):
        seed_session("user", photos=8, limit_notified=False, updated=time.time())
        sent = AsyncMock()
        download = AsyncMock(return_value=b"bytes")
        with patch.object(main, "send_text", sent), patch.object(
            main, "download_media", download
        ):
            await main._store_wa_media("user", "9", {}, "photos")
            await main._store_wa_media("user", "10", {}, "photos")

        self.assertEqual(len(whatsapp.SESSIONS["user"]["photos"]), 8)
        download.assert_not_awaited()
        sent.assert_awaited_once_with(
            "user", "Maximum 8 images per claim — keeping the first 8."
        )


if __name__ == "__main__":
    unittest.main()
