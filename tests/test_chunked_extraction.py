try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import db
import gemini
import main
import rules
import vision

try:
    from helpers import fresh_key_state, provider_keys
except ImportError:
    from tests.helpers import fresh_key_state, provider_keys


def page(path, shade):
    image = Image.new("RGB", (120, 160), (shade, shade, shade))
    for x in range(0, 120, 10):  # patterned, so every page has its own hash
        image.putpixel((x, shade % 160), (255 - shade, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    with open(path, "wb") as handle:
        handle.write(buffer.getvalue())
    return path


def field(name, value, confidence, status="found"):
    return {"name": name, "value": value, "confidence": confidence, "status": status}


class ChunkedExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp.name, "chunks.db")
        db.prepare_test_db()
        self.calls = []
        self.answers = {}
        self.fail_labels = set()

    def tearDown(self):
        db.DB_PATH = self.original_db
        self.temp.cleanup()

    def document(self, pages):
        paths = [page(os.path.join(self.temp.name, f"p{n}.jpg"), 20 * n) for n in range(1, pages + 1)]
        return {"name": "Claim_Form.pdf", "paths": paths, "doc_type": "claim_form"}

    def provider(self, name):
        def call(parts, model, label):
            images = sum(1 for part in parts if part[0] == "image")
            self.calls.append((name, label, images))
            if label in self.fail_labels:
                raise RuntimeError("503 UNAVAILABLE")
            return {"fields": self.answers.get(label, [])}
        return call

    def extract(self, document, cache="false"):
        with provider_keys(), fresh_key_state(), \
                patch.dict(vision.PROVIDER_CALLS, {
                    name: self.provider(name) for name in ("groq", "gemini", "openrouter")}), \
                patch.dict(os.environ, {"VISION_PROVIDERS": "groq,gemini,openrouter",
                                        "CACHE_ENABLED": cache}), \
                patch.object(vision.time, "sleep"):
            vision.reset_cache_tracking()
            return gemini.extract_fields([document])

    def test_a_four_page_form_is_two_groq_calls(self):
        self.answers = {
            "extract_fields(Claim_Form.pdf p.1-3)": [
                field("insured_name_claim_form", "Arjun Mehta", 0.7),
                field("accident_date", None, 0, "not_present"),
            ],
            "extract_fields(Claim_Form.pdf p.4)": [
                field("insured_name_claim_form", "ARJUN MEHTA", 0.9),
                field("accident_date", "2026-10-12", 0.8),
            ],
        }
        result = self.extract(self.document(4))
        self.assertEqual(self.calls, [
            ("groq", "extract_fields(Claim_Form.pdf p.1-3)", 3),
            ("groq", "extract_fields(Claim_Form.pdf p.4)", 1),
        ])
        self.assertEqual(result["status"], "ok")
        values = {f["name"]: (f["value"], f["source_doc"]) for f in result["fields"]}
        # The highest-confidence non-null value wins, with its chunk as the source.
        self.assertEqual(values["insured_name_claim_form"], ("ARJUN MEHTA", "Claim_Form.pdf p.4"))
        self.assertEqual(values["accident_date"], ("2026-10-12", "Claim_Form.pdf p.4"))

    def test_pages_are_numbered_against_the_whole_document(self):
        document = self.document(4)
        captured = {}

        def groq(parts, model, label):
            captured[label] = [part[1] for part in parts if part[0] == "text"]
            return {"fields": [field("accident_time", "18:40", 0.9)]}

        with provider_keys(), fresh_key_state(), \
                patch.dict(vision.PROVIDER_CALLS, {"groq": groq}), \
                patch.dict(os.environ, {"VISION_PROVIDERS": "groq", "CACHE_ENABLED": "false"}):
            gemini.extract_fields([document])
        second = captured["extract_fields(Claim_Form.pdf p.4)"]
        self.assertIn("Filename: Claim_Form.pdf (page 4 of 4)", second)
        self.assertTrue(any("pages 4 to 4 of 4" in text for text in second))

    def test_a_failed_chunk_keeps_the_rest_and_names_the_pages(self):
        self.answers = {"extract_fields(Claim_Form.pdf p.1-3)": [
            field("insured_name_claim_form", "Arjun Mehta", 0.9)]}
        self.fail_labels = {"extract_fields(Claim_Form.pdf p.4)"}
        result = self.extract(self.document(4))

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["read"], ["Claim_Form.pdf"])
        self.assertEqual([f["value"] for f in result["fields"]], ["Arjun Mehta"])
        self.assertEqual(result["failed"], [{
            "name": "Claim_Form.pdf p.4", "document": "Claim_Form.pdf", "pages": "p.4",
            "doc_type": "claim_form", "error": result["failed"][0]["error"],
        }])
        # Every provider was tried for the failed chunk; gemini's 503 once more.
        self.assertEqual([c[0] for c in self.calls if c[1].endswith("p.4)")],
                         ["groq", "gemini", "gemini", "openrouter"])

        # The document still counts as received; the pages are asked for.
        check = rules.establish_documents([], result, ["Claim_Form.pdf"], ["claim_form"])
        self.assertEqual(check["missing"], [])
        entries = main.build_undetermined({"extraction": result, "fields": result["fields"]})
        pages = [e for e in entries if "Claim_Form.pdf p.4" in e["item"]]
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["reason"].startswith(
            "page 4 of Claim_Form.pdf could not be read"))
        self.assertEqual(pages[0]["audience"], "customer_actionable")
        self.assertEqual(pages[0]["request"], "please send a clear photo of page 4 of your claim form")
        reasons = rules.extraction_reasons(result)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("Claim_Form.pdf p.4 could not be read"))

    def test_every_chunk_failing_fails_the_document(self):
        self.fail_labels = {"extract_fields(Claim_Form.pdf p.1-3)",
                            "extract_fields(Claim_Form.pdf p.4)"}
        result = self.extract(self.document(4))
        self.assertEqual(result["status"], "failed")
        self.assertEqual([item["name"] for item in result["failed"]], ["Claim_Form.pdf"])

    def test_chunks_are_cached_one_by_one(self):
        self.answers = {
            "extract_fields(Claim_Form.pdf p.1-3)": [field("accident_time", "18:40", 0.9)],
            "extract_fields(Claim_Form.pdf p.4)": [field("accident_location", "Pune", 0.9)],
        }
        document = self.document(4)
        self.extract(document, cache="true")
        self.assertEqual(len(self.calls), 2)
        again = self.extract(document, cache="true")
        self.assertEqual(len(self.calls), 2)  # both chunks came from the cache
        self.assertEqual({f["name"] for f in again["fields"]}, {"accident_time", "accident_location"})

    def test_a_three_page_document_is_one_call(self):
        self.answers = {"extract_fields(Claim_Form.pdf)": [field("accident_time", "18:40", 0.9)]}
        result = self.extract(self.document(3))
        self.assertEqual(self.calls, [("groq", "extract_fields(Claim_Form.pdf)", 3)])
        self.assertEqual(result["fields"][0]["source_doc"], "Claim_Form.pdf")


if __name__ == "__main__":
    unittest.main()
