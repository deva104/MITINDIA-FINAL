"""Fixes from the CLM-C88F2040 live re-test: UTC timestamps, owner versus
driver, customer-actionable undetermined items, provider provenance."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import db
import main
import rules
import vision
import whatsapp

try:
    from helpers import fresh_key_state, provider_keys
except ImportError:
    from tests.helpers import fresh_key_state, provider_keys

ROOT = Path(__file__).resolve().parents[1]


def field(name, value):
    return {"name": name, "value": value, "status": "found", "confidence": 0.9,
            "source_doc": "doc.jpg"}


# 1 -------------------------------------------------------------------------
class UtcTimestampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp.name, "utc.db")
        db.prepare_test_db()

    def tearDown(self):
        db.DB_PATH = self.original
        self.temp.cleanup()

    def test_new_timestamps_are_zoned_utc(self):
        stamp = db.utc_now_iso()
        self.assertTrue(stamp.endswith("+00:00"), stamp)
        db.save_claim("CLM-UTC", {"claim_id": "CLM-UTC", "status": "analysed"})
        db.audit_log_add("CLM-UTC", "a", "set_decision", "claim", None, {"value": "approved"}, "r")
        self.assertTrue(db.audit_log_for_claim("CLM-UTC")[0]["created_at"].endswith("+00:00"))
        self.assertTrue(db.list_claims()[0]["created_at"].endswith("+00:00"))

    def test_legacy_naive_rows_are_read_as_utc(self):
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO claims (claim_id, created_at, payload_json) VALUES (?, ?, ?)",
                ("CLM-OLD", "2026-10-19T05:51:43.010273",
                 json.dumps({"claim_id": "CLM-OLD", "created_at": "2026-10-19T05:51:43.010273"})),
            )
            conn.execute(
                "INSERT INTO audit_log (claim_id, actor, action, target, before_json, "
                "after_json, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("CLM-OLD", "a", "set_decision", "claim", None, None, "r",
                 "2026-10-19T05:52:10"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(db.get_claim("CLM-OLD")["created_at"], "2026-10-19T05:51:43.010273+00:00")
        self.assertEqual(db.list_claims()[0]["created_at"], "2026-10-19T05:51:43.010273+00:00")
        audit = db.audit_log_for_claim("CLM-OLD")[0]["created_at"]
        self.assertEqual(audit, "2026-10-19T05:52:10+00:00")
        # 05:52 UTC is 11:22 IST, the time the action was taken.
        ist = datetime.fromisoformat(audit).astimezone(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30)))
        self.assertEqual(ist.strftime("%H:%M"), "11:22")

    def test_no_naive_now_left_in_the_code(self):
        for name in ("db.py", "main.py", "whatsapp.py", "forensics.py", "rules.py"):
            self.assertNotIn("datetime.now()", (ROOT / name).read_text(encoding="utf-8"), name)


# 2 -------------------------------------------------------------------------
class OwnerDriverTests(unittest.TestCase):
    def test_driver_need_not_be_the_owner(self):
        fields = [field("owner_name_rc", "Arjun Mehta"), field("holder_name_dl", "Kavita Rao")]
        flags = rules.run_rules(fields, [])
        self.assertEqual([f for f in flags if f["severity"] == "HIGH"], [])
        notes = rules.identity_notes(fields)
        self.assertEqual([note["rule"] for note in notes], ["driver_not_owner"])
        self.assertEqual(notes[0]["severity"], "INFO")
        self.assertIn("driver is not the registered owner", notes[0]["message"].lower())

    def test_insured_must_match_owner_and_driver_must_match_licence(self):
        fields = [
            field("insured_name_claim_form", "Arjun Mehta"),
            field("owner_name_rc", "Rohan Das"),
            field("driver_name_claim_form", "Kavita Rao"),
            field("holder_name_dl", "Imran Shaikh"),
        ]
        rules_hit = {f["rule"]: f["severity"] for f in rules.run_rules(fields, [])}
        self.assertEqual(rules_hit.get("insured_owner_mismatch"), "HIGH")
        self.assertEqual(rules_hit.get("driver_licence_mismatch"), "HIGH")
        self.assertNotIn("name_mismatch", rules_hit)

    def test_matching_driver_and_insured_raise_nothing(self):
        fields = [
            field("insured_name_claim_form", "A. Mehta"),
            field("owner_name_rc", "ARJUN MEHTA"),
            field("driver_name_claim_form", "Kavita Rao"),
            field("holder_name_dl", "KAVITA RAO"),
        ]
        self.assertEqual(rules.run_rules(fields, []), [])

    def test_no_claim_form_makes_driver_identity_undetermined(self):
        """CLM-C88F2040's shape: RC and DL read, names differ, no claim form."""
        payload = {
            "fields": [field("owner_name_rc", "Arjun Mehta"), field("holder_name_dl", "Kavita Rao")],
            "document_check": rules.establish_documents(
                [{"filename": "rc.jpg", "doc_type": "rc", "confidence": 1.0},
                 {"filename": "dl.jpg", "doc_type": "driving_licence", "confidence": 1.0}],
                {"fields": []}, [], ["rc", "driving_licence", "claim_form"],
            ),
        }
        entries = main.build_undetermined(payload)
        driver = [entry for entry in entries if entry["item"] == "Driver identity"]
        self.assertEqual(len(driver), 1)
        self.assertEqual(driver[0]["reason"],
                         "no claim form, so the licence cannot be matched to the driver")
        self.assertEqual([f for f in rules.run_rules(payload["fields"], [])
                          if f["severity"] == "HIGH"], [])
        self.assertEqual(len(rules.identity_notes(payload["fields"])), 1)

    def test_claim_form_extraction_asks_for_the_driver(self):
        import gemini
        self.assertIn("driver_name_claim_form", gemini.DOCUMENT_FIELDS["claim_form"])
        self.assertIn("driver_name_claim_form", gemini.EXTRACT_PROMPT)


# 3 -------------------------------------------------------------------------
class CustomerActionableTests(unittest.TestCase):
    def payload(self):
        return {
            "fields": [field("owner_name_rc", "Arjun Mehta"), field("holder_name_dl", "Kavita Rao"),
                       {"name": "dl_expiry_date", "value": None, "status": "unreadable",
                        "source_doc": "dl.jpg"}],
            "photos": [{"source_photo": "p.jpg", "is_vehicle": True, "number_plate": None}],
            "forensics": [
                {"check": "exif", "verdict": "unverifiable"},
                {"check": "ela", "verdict": "informational"},
                {"check": "ai_generated", "verdict": "unavailable", "detail": "x"},
            ],
            "document_check": rules.establish_documents(
                [{"filename": "rc.jpg", "doc_type": "rc", "confidence": 1.0},
                 {"filename": "dl.jpg", "doc_type": "driving_licence", "confidence": 1.0}],
                {"fields": []}, [], ["rc", "driving_licence", "claim_form"],
            ),
        }

    def test_entries_are_split_by_audience(self):
        entries = main.build_undetermined(self.payload())
        actionable = {e["item"]: e["request"] for e in entries
                      if e["audience"] == "customer_actionable"}
        self.assertEqual(actionable, {
            "Claim form": "please send your claim form",
            "Number plate": "please send a clear photo of the number plate",
            "Driving licence expiry": "please send a clearer photo of your driving licence",
        })
        internal = {e["item"] for e in entries if e["audience"] == "internal"}
        self.assertTrue({"Photo capture time and location", "Photo editing",
                         "AI-generation check", "Driver identity"} <= internal)

    def test_whatsapp_shows_only_requests(self):
        entries = main.build_undetermined(self.payload())
        text = "\n".join(whatsapp._undetermined_lines(entries))
        self.assertIn("*To complete your claim:*", text)
        self.assertIn("- Please send your claim form.", text)
        self.assertIn("- Please send a clear photo of the number plate.", text)
        lowered = text.lower()
        for internal in ("metadata", "editing", "ai-generation", "error-level",
                         "provider", "groq", "gemini", "could not determine", "driver identity"):
            self.assertNotIn(internal, lowered)


# 4 -------------------------------------------------------------------------
class ProviderProvenanceTests(unittest.TestCase):
    def test_rate_limited_fallback_is_shown_on_the_stage_not_undetermined(self):
        class RateLimitError(Exception):
            status_code = 429

        def groq(parts, model, label):
            raise RateLimitError("Error code: 429 - {'error': 'org_secret'}")

        def gemini(parts, model, label):
            if label.startswith("assess_damage"):
                return {"is_vehicle": True, "vehicle_description": "car", "photos": [
                    {"source_photo": "damage.jpg", "is_vehicle": True, "vehicle_group": 0}],
                    "damage": [{"part": "front_bumper", "damage_type": "dent",
                                "severity": "moderate", "confidence": 0.9,
                                "source_photo": "damage.jpg"}]}
            return {"doc_type": "vehicle_photo", "confidence": 0.9, "number_plate": None}

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        photo = os.path.join(temp.name, "damage.jpg")
        Image.new("RGB", (64, 48), (90, 40, 40)).save(photo, format="JPEG")

        class Conn:
            def close(self):
                pass

        with provider_keys(), fresh_key_state(), \
                patch.dict(vision.PROVIDER_CALLS, {"groq": groq, "gemini": gemini}), \
                patch.dict(os.environ, {"VISION_PROVIDERS": "groq,gemini", "CACHE_ENABLED": "false",
                                        "REQUIRED_DOCS": "", "AI_DETECTION_ENABLED": "false"}), \
                patch.multiple(main, get_conn=lambda: Conn(),
                               check_duplicates=lambda paths, conn: [],
                               build_photo_url_map=lambda paths: {},
                               run_deep_forensics=lambda paths, conn, fields=None: [],
                               save_claim=lambda claim_id, payload: None):
            payload = asyncio.run(main.analyse_claim("TEST-PROV", [], [photo]))

        stages = {t["stage"]: t for t in payload["timings"]}
        self.assertEqual(stages["stage1_gemini"]["via"], "via gemini (groq rate-limited)")
        self.assertEqual(stages["stage0_5_classify"]["via"], "via gemini (groq rate-limited)")
        self.assertFalse(any(e["item"] == "Primary vision provider"
                             for e in payload["undetermined"]))
        self.assertNotIn("org_secret", json.dumps(payload))
        self.assertTrue(payload["created_at"].endswith("+00:00"), payload["created_at"])

    def test_console_shows_provenance_in_the_trace(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("timing.via", source)


if __name__ == "__main__":
    unittest.main()
