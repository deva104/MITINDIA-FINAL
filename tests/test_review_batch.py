"""Findings from the production end-to-end test: silent extraction failure,
per-document extraction, provider reporting, the missing-documents decision,
indicative double counting, ELA calibration, console wording, startup check."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import io
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fitz
from fastapi.testclient import TestClient
from PIL import Image

import db
import forensics
import main
import rules
import vision
import whatsapp

try:
    from helpers import seed_session
except ImportError:
    from tests.helpers import seed_session

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
REQUIRED = "rc,driving_licence,claim_form"
CLASSES = {
    "damage.jpg": {"doc_type": "vehicle_photo", "confidence": 0.97, "number_plate": None},
    "rc.jpg": {"doc_type": "rc", "confidence": 1.0, "number_plate": None},
    "dl.jpg": {"doc_type": "driving_licence", "confidence": 1.0, "number_plate": None},
}
DAMAGE = {
    "is_vehicle": True,
    "vehicle_description": "white sedan",
    "photos": [{"source_photo": "damage.jpg", "is_vehicle": True, "vehicle_group": 0,
                "colour": "white", "make_model": "Kia", "body_type": "sedan"}],
    "damage": [{"source_photo": "damage.jpg", "part": "front_bumper", "damage_type": "dent",
                "severity": "moderate", "confidence": 0.9, "reasoning": "dent"}],
}
RC_FIELDS = [
    {"name": "owner_name_rc", "value": "R K Sharma", "confidence": 0.9, "status": "found"},
    {"name": "registration_number_rc", "value": "MH-12-AB-1234", "confidence": 0.9,
     "status": "found"},
    # A field the RC cannot carry: must be dropped, never attributed to the RC.
    {"name": "holder_name_dl", "value": "Somebody Else", "confidence": 0.9, "status": "found"},
]
DL_FIELDS = [
    {"name": "holder_name_dl", "value": "R K Sharma", "confidence": 0.9, "status": "found"},
    {"name": "dl_expiry_date", "value": "2030-01-01", "confidence": 0.9, "status": "found"},
]


def jpeg(color, size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeConnection:
    def close(self):
        pass


class ProviderBench:
    """Fake providers behind the real vision chain. Records every call."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def provider(self, name):
        def call(parts, model, label):
            images = sum(1 for part in parts if part[0] == "image")
            prompt = " ".join(part[1] for part in parts if part[0] == "text")
            self.calls.append({"provider": name, "label": label, "images": images,
                               "prompt": prompt})
            return self.behaviour(name, label)
        return call

    def patches(self):
        return [
            patch.dict(vision.PROVIDER_CALLS, {
                name: self.provider(name) for name in ("groq", "gemini", "openrouter")
            }),
            patch.dict(os.environ, {
                "VISION_PROVIDERS": "groq,gemini,openrouter",
                "CACHE_ENABLED": "false",
                "REQUIRED_DOCS": REQUIRED,
            }),
            patch.object(vision.time, "sleep"),
        ]


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = {}
        for index, name in enumerate(("damage.jpg", "rc.jpg", "dl.jpg")):
            path = os.path.join(self.temp.name, name)
            Path(path).write_bytes(jpeg((40 + index * 60, 90, 140)))
            self.paths[name] = path

    def tearDown(self):
        self.temp.cleanup()

    def analyse(self, bench, photos=("damage.jpg", "rc.jpg", "dl.jpg"), docs=()):
        stubs = bench.patches() + [patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: [],
            build_photo_url_map=lambda paths: {},
            classify_image=lambda path: CLASSES[os.path.basename(path)],
            assess_damage=lambda paths: json.loads(json.dumps(DAMAGE)),
            run_deep_forensics=lambda paths, conn, fields=None: [],
            save_claim=lambda claim_id, payload: None,
        )]
        for stub in stubs:
            stub.start()
        try:
            return asyncio.run(main.analyse_claim(
                "TEST-BATCH", list(docs), [self.paths[name] for name in photos],
            ))
        finally:
            for stub in reversed(stubs):
                stub.stop()


# Item 1 -------------------------------------------------------------------
class SilentExtractionFailureTests(PipelineCase):
    def test_every_provider_failing_is_reported_and_forces_review(self):
        def behaviour(provider, label):
            raise RuntimeError("503 UNAVAILABLE: model overloaded")

        payload = self.analyse(ProviderBench(behaviour))

        extraction = payload["extraction"]
        self.assertEqual(extraction["status"], "failed")
        self.assertEqual(extraction["documents"], 2)
        self.assertIn("503 UNAVAILABLE", extraction["error"])
        self.assertEqual(payload["fields"], [])
        entry = next(item for item in payload["undetermined"]
                     if item["item"] == "Document details")
        self.assertTrue(entry["reason"].startswith(
            "2 documents received but could not be read: "))
        self.assertIn("503 UNAVAILABLE", entry["reason"])
        self.assertEqual(
            entry["impact"],
            "cross-document checks (name, registration, licence expiry) were not run",
        )
        self.assertEqual(payload["decision"], "needs_review")
        self.assertIn(entry["reason"], payload["decision_reasons"])

    def test_one_failed_document_keeps_the_others(self):
        def behaviour(provider, label):
            if label == "extract_fields(rc.jpg)":
                return {"fields": RC_FIELDS}
            raise RuntimeError("503 UNAVAILABLE")

        payload = self.analyse(ProviderBench(behaviour))

        self.assertEqual(payload["extraction"]["status"], "partial")
        self.assertEqual(payload["extraction"]["read"], ["rc.jpg"])
        names = {field["name"]: field for field in payload["fields"]}
        self.assertEqual(names["owner_name_rc"]["source_doc"], "rc.jpg")
        self.assertNotIn("holder_name_dl", names)
        self.assertTrue(any(item["item"] == "Document details: dl.jpg"
                            for item in payload["undetermined"]))
        self.assertTrue(any(reason.startswith("dl.jpg could not be read")
                            for reason in payload["decision_reasons"]))


# Item 6 -------------------------------------------------------------------
class PerDocumentExtractionTests(PipelineCase):
    def test_each_document_is_its_own_groq_call_with_its_own_fields(self):
        def behaviour(provider, label):
            if label == "extract_fields(rc.jpg)":
                return {"fields": RC_FIELDS}
            if label == "extract_fields(dl.jpg)":
                return {"fields": DL_FIELDS}
            raise AssertionError(label)

        bench = ProviderBench(behaviour)
        payload = self.analyse(bench)

        self.assertEqual(
            [(call["provider"], call["label"], call["images"]) for call in bench.calls],
            [("groq", "extract_fields(rc.jpg)", 1), ("groq", "extract_fields(dl.jpg)", 1)],
        )
        rc_prompt = bench.calls[0]["prompt"]
        self.assertIn("owner_name_rc", rc_prompt)
        self.assertNotIn("holder_name_dl", rc_prompt)
        self.assertEqual(payload["extraction"]["status"], "ok")
        holders = [field for field in payload["fields"] if field["name"] == "holder_name_dl"]
        self.assertEqual([field["source_doc"] for field in holders], ["dl.jpg"])
        self.assertFalse(any(item["item"] == "Primary vision provider"
                             for item in payload["undetermined"]))

    def test_bundle_over_three_pages_falls_through_the_chain(self):
        document = fitz.open()
        for page in range(4):
            document.new_page().insert_text((72, 72), f"Claim form page {page + 1}")
        pdf = os.path.join(self.temp.name, "bundle.pdf")
        document.save(pdf)
        document.close()

        def behaviour(provider, label):
            return {"fields": [{"name": "accident_date", "value": "2026-10-10",
                                "confidence": 0.9, "status": "found"}]}

        bench = ProviderBench(behaviour)
        payload = self.analyse(bench, photos=("damage.jpg",), docs=(pdf,))

        self.assertEqual([(call["provider"], call["images"]) for call in bench.calls],
                         [("gemini", 4)])
        self.assertEqual(payload["documents_received"]["claim_form"], 1)
        # Groq was skipped for capacity: routing, not uncertainty.
        self.assertFalse(any(item["item"] == "Primary vision provider"
                             for item in payload["undetermined"]))


# Item 7 -------------------------------------------------------------------
class ProviderReportingTests(unittest.TestCase):
    def run_chain(self, behaviour, images):
        bench = ProviderBench(behaviour)
        parts = [("image", jpeg((index, 10, 10)), "image/jpeg") for index in range(images)]
        stubs = bench.patches()
        for stub in stubs:
            stub.start()
        try:
            vision.reset_cache_tracking()
            try:
                result = vision.generate_json(parts + [("text", "x")], "probe")
            except vision.VisionUnavailable as error:
                result = error
            return result, list(vision.fallbacks_used())
        finally:
            for stub in reversed(stubs):
                stub.stop()

    def test_capacity_skip_is_not_undetermined(self):
        result, fallbacks = self.run_chain(lambda provider, label: {"ok": provider}, images=4)
        self.assertEqual(result, {"ok": "gemini"})
        self.assertEqual(fallbacks[0]["reason"], "skipped")
        self.assertEqual(fallbacks[0]["detail"], "4 images exceeds groq limit 3")
        self.assertEqual(main.build_undetermined({}, fallbacks), [])

    def test_real_failure_is_undetermined_with_its_cause(self):
        def behaviour(provider, label):
            if provider == "groq":
                raise RuntimeError("rate limited")
            return {"ok": provider}

        result, fallbacks = self.run_chain(behaviour, images=1)
        self.assertEqual(result, {"ok": "gemini"})
        self.assertEqual(fallbacks[0]["reason"], "failed")
        entries = main.build_undetermined({}, fallbacks)
        self.assertEqual(entries[0]["item"], "Primary vision provider")
        self.assertIn("groq unavailable (RuntimeError: rate limited)", entries[0]["reason"])

    def test_exhausted_chain_names_every_provider(self):
        def behaviour(provider, label):
            raise RuntimeError("503 UNAVAILABLE")

        error, _ = self.run_chain(behaviour, images=6)
        self.assertIsInstance(error, vision.VisionUnavailable)
        self.assertEqual(
            str(error),
            "no vision provider succeeded (groq skipped: 6 images exceeds groq limit 3; "
            "gemini, openrouter failed: RuntimeError: 503 UNAVAILABLE)",
        )

    def test_a_chain_that_fails_records_no_fallback(self):
        def behaviour(provider, label):
            raise RuntimeError("503 UNAVAILABLE")

        _, fallbacks = self.run_chain(behaviour, images=1)
        self.assertEqual(fallbacks, [])


# Item 3 -------------------------------------------------------------------
class MissingDocumentDecisionTests(PipelineCase):
    def test_one_function_states_missing_and_unread(self):
        received = {"rc": 1, "driving_licence": 1, "claim_form": 0}
        required = ["rc", "driving_licence", "claim_form"]
        self.assertEqual(rules.document_requirements(received, required)["reason"],
                         "documents not provided: claim form")
        unread = rules.document_requirements(received, required, unread_documents=1)
        self.assertTrue(unread["unverified"])
        self.assertEqual(
            unread["reason"],
            "required documents not established: claim form "
            "(1 document received could not be read)",
        )

    def test_web_upload_without_documents_gets_the_reason(self):
        payload = self.analyse(ProviderBench(lambda p, l: {}), photos=("damage.jpg",))
        self.assertIn(
            "documents not provided: registration certificate, driving licence, claim form",
            payload["decision_reasons"],
        )

    def test_unread_pdf_is_not_established_never_not_provided(self):
        """CLM-F148F0EB's shape: RC and DL photos read, claim-form PDF unread."""
        document = fitz.open()
        document.new_page().insert_text((72, 72), "MOTOR CLAIM FORM")
        pdf = os.path.join(self.temp.name, "claim.pdf")
        document.save(pdf)
        document.close()

        def behaviour(provider, label):
            if label == "extract_fields(rc.jpg)":
                return {"fields": RC_FIELDS}
            if label == "extract_fields(dl.jpg)":
                return {"fields": DL_FIELDS}
            raise RuntimeError("503 UNAVAILABLE")

        payload = self.analyse(ProviderBench(behaviour), docs=(pdf,))
        reason = ("required documents not established: claim form "
                  "(1 document received could not be read)")
        self.assertIn(reason, payload["decision_reasons"])
        claim_form = next(item for item in payload["undetermined"]
                          if item["item"] == "Claim form")
        self.assertEqual(claim_form["reason"], "not established: 1 received document could not be read")


class WhatsAppSameReasonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = (db.DB_PATH, main.UPLOAD_DIR)
        db.DB_PATH = os.path.join(self.temp.name, "batch.db")
        main.UPLOAD_DIR = os.path.join(self.temp.name, "uploads")
        db.prepare_test_db()
        whatsapp.SESSIONS.clear()
        whatsapp.ANALYSING_IDS.clear()

    def tearDown(self):
        whatsapp.SESSIONS.clear()
        db.DB_PATH, main.UPLOAD_DIR = self.original
        self.temp.cleanup()

    async def test_skip_gets_the_same_reason_as_a_web_upload(self):
        seed_session("user", photos=1, updated=time.time())
        classify = lambda path: {"doc_type": "vehicle_photo", "confidence": 0.95,  # noqa: E731
                                 "number_plate": None}
        damage = json.loads(json.dumps(DAMAGE))

        def assess(paths):
            damage["photos"][0]["source_photo"] = os.path.basename(paths[0])
            damage["damage"][0]["source_photo"] = os.path.basename(paths[0])
            return damage

        with patch.dict(os.environ, {"REQUIRED_DOCS": REQUIRED, "AI_DETECTION_ENABLED": "false"}), \
                patch.object(whatsapp, "classify_image", classify), \
                patch.object(main, "classify_image", classify), \
                patch.object(main, "assess_damage", assess), \
                patch.object(whatsapp, "send_text", AsyncMock()):
            await whatsapp.process_session("user", skip_missing=True)

        conn = db.get_conn()
        try:
            claim = json.loads(conn.execute(
                "SELECT payload_json FROM claims").fetchone()["payload_json"])
        finally:
            conn.close()
        self.assertIn(
            "documents not provided: registration certificate, driving licence, claim form",
            claim["decision_reasons"],
        )


# Item 2 -------------------------------------------------------------------
class IndicativeRangeTests(unittest.TestCase):
    def test_confirmed_part_contributes_nothing_to_the_indicative_range(self):
        estimate = rules.build_estimate([
            {"part": "rear_right_quarter", "damage_type": "dent", "severity": "severe",
             "confidence": 0.5, "confirmed": True},
            {"part": "rear_right_quarter", "damage_type": "paint_damage",
             "severity": "severe", "confidence": 0.5},
            {"part": "rear_right_quarter", "damage_type": "dent", "severity": "severe",
             "confidence": 0.5},
        ])
        self.assertEqual((estimate["total_low"], estimate["total_high"]), (13000, 20000))
        self.assertEqual((estimate["indicative_low"], estimate["indicative_high"]), (0, 0))
        self.assertTrue(all(entry["counts_toward_indicative"] is False
                            for entry in estimate["unconfirmed"]))

    def test_one_indicative_line_per_part_most_severe_wins(self):
        estimate = rules.build_estimate([
            {"part": "front_bumper", "damage_type": "scratch", "severity": "minor",
             "confidence": 0.4},
            {"part": "front_bumper", "damage_type": "crack", "severity": "severe",
             "confidence": 0.4},
            {"part": "bonnet", "damage_type": "dent", "severity": "moderate", "confidence": 0.4},
        ])
        counted = [(entry["part"], entry["severity"]) for entry in estimate["unconfirmed"]
                   if entry["counts_toward_indicative"]]
        self.assertEqual(counted, [("front_bumper", "severe"), ("bonnet", "moderate")])
        bumper = next(e for e in estimate["unconfirmed"] if e["severity"] == "severe")
        bonnet = next(e for e in estimate["unconfirmed"] if e["part"] == "bonnet")
        self.assertEqual(estimate["indicative_high"], bumper["high"] + bonnet["high"])

    def test_whatsapp_reply_uses_the_deduplicated_range(self):
        estimate = rules.build_estimate([
            {"part": "rear_bumper", "damage_type": "dent", "severity": "severe",
             "confidence": 0.5},
            {"part": "rear_bumper", "damage_type": "dent", "severity": "severe",
             "confidence": 0.5},
        ])
        text = whatsapp._estimate_range(estimate)
        self.assertIn(f"Rs {estimate['indicative_high']:,}", text)
        self.assertEqual(estimate["indicative_high"], 15000)


# Item 4 -------------------------------------------------------------------
class ElaCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def recompressed(self, name, quality=75, edit=None):
        with Image.open(FIXTURES / name) as image:
            image = image.convert("RGB")
            image.thumbnail((1600, 1600))
            if edit:
                image = edit(image)
            path = os.path.join(self.temp.name, f"{quality}-{name}")
            image.save(path, format="JPEG", quality=quality)
        return path

    def ela(self, path):
        with patch.dict(os.environ, {"AI_DETECTION_ENABLED": "false"}), \
                patch.object(forensics, "_store_processed", lambda image, kind: None):
            results = forensics.run_deep_forensics([path])
        return next(item for item in results if item["check"] == "ela")

    def test_genuine_whatsapp_photo_is_informational_not_suspicious(self):
        for quality in (70, 75, 80):
            entry = self.ela(self.recompressed("claimed_panel.jpeg", quality))
            self.assertEqual(entry["verdict"], "informational")
            self.assertIn("informational only", entry["detail"])

    def test_edited_photo_is_never_called_clean(self):
        donor = Image.open(FIXTURES / "claimed_panel_screenshot.jpeg").convert("RGB")

        def splice(image):
            image = image.copy()
            image.paste(donor.resize((image.width // 4, image.height // 4)),
                        (image.width // 3, image.height // 3))
            return image

        entry = self.ela(self.recompressed("claimed_panel.jpeg", 75, splice))
        self.assertEqual(entry["verdict"], "informational")
        self.assertNotIn(entry["verdict"], ("clean", "suspicious"))

    def test_undetermined_says_editing_is_not_ruled_out(self):
        entries = main.build_undetermined({"forensics": [
            {"check": "ela", "verdict": "informational", "detail": "x"},
        ]})
        self.assertIn({
            "item": "Photo editing",
            "reason": "error-level analysis cannot tell recompression from editing on these photos",
            "impact": "editing neither confirmed nor ruled out",
        }, entries)


# Items 1 and 5: console wording -------------------------------------------
class ConsoleWordingTests(unittest.TestCase):
    source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_unread_documents_are_never_no_documents(self):
        self.assertIn("received — extraction failed: ", self.source)
        self.assertIn("'Cross-document checks not run — documents could not be read.'",
                      self.source)
        self.assertNotIn("No inconsistencies detected", self.source)

    def test_zero_system_estimate_and_rupee_sign(self):
        self.assertIn("System estimate: no confirmed findings", self.source)
        self.assertNotIn("Rs ", self.source)


# Item 8 -------------------------------------------------------------------
class StartupCheckTests(unittest.TestCase):
    def test_health_reports_chain_without_secrets(self):
        payload = TestClient(main.app).get("/api/health").json()
        chain = payload["vision"]["chain"]
        self.assertTrue(chain)
        for item in chain:
            self.assertIsInstance(item.get("key_configured"), bool)
        text = json.dumps(payload)
        for key in vision.PROVIDER_KEYS.values():
            value = os.getenv(key, "").strip()
            if value:
                self.assertNotIn(value, text)

    def test_listed_provider_without_key_and_unused_key_are_warned(self):
        with patch.dict(os.environ, {
            "VISION_PROVIDERS": "groq,gemini",
            "GROQ_API_KEY": "",
            "GEMINI_API_KEY": "x",
            "OPENROUTER_API_KEY": "y",
        }):
            warnings = vision.provider_status()["warnings"]
        self.assertIn("groq is in VISION_PROVIDERS but GROQ_API_KEY is not set", warnings)
        self.assertIn(
            "OPENROUTER_API_KEY is set but openrouter is not in VISION_PROVIDERS, "
            "so it is never used",
            warnings,
        )


if __name__ == "__main__":
    unittest.main()
