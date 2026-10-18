try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import db
import forensics
import main
import whatsapp
from rules import assess_confidence, build_estimate, run_rules


class FakeConnection:
    def close(self):
        pass


def fake_response(score=None, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = (
        {"type": {"ai_generated": score}} if score is not None else {}
    )
    return response


class AiDetectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "ai-cache.db")
        db.prepare_test_db()
        self.env = patch.dict(
            os.environ,
            {
                "AI_DETECTION_ENABLED": "true",
                "SIGHTENGINE_API_USER": "test-user",
                "SIGHTENGINE_API_SECRET": "test-secret",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        db.DB_PATH = self.original_db
        self.folder.cleanup()

    def test_thresholds_and_original_bytes(self):
        original = b"original-upload-bytes"
        cases = [
            (0.23, "clean"),
            (0.62, "inconclusive"),
            (0.91, "suspicious"),
        ]
        for index, (score, verdict) in enumerate(cases):
            with self.subTest(score=score), patch.object(
                forensics.httpx,
                "post",
                return_value=fake_response(score),
            ) as request:
                data = original + bytes([index])
                result = forensics.check_ai_generated(
                    data, f"photo-{index}.jpg", "image/jpeg"
                )
                self.assertEqual(result["verdict"], verdict)
                self.assertEqual(result["score"], score)
                self.assertEqual(
                    request.call_args.kwargs["files"]["media"][1], data
                )
                self.assertEqual(request.call_args.kwargs["timeout"], 8.0)

    def test_success_is_cached_and_second_run_spends_no_operation(self):
        with patch.object(
            forensics.httpx,
            "post",
            return_value=fake_response(0.91),
        ) as request:
            first = forensics.check_ai_generated(
                b"same-photo", "damage.jpg", "image/jpeg"
            )
            second = forensics.check_ai_generated(
                b"same-photo", "damage.jpg", "image/jpeg"
            )

        self.assertEqual(first, second)
        self.assertEqual(request.call_count, 1)

    def test_missing_keys_is_unavailable_and_undetermined(self):
        with patch.dict(
            os.environ,
            {"SIGHTENGINE_API_USER": "", "SIGHTENGINE_API_SECRET": ""},
        ), patch.object(forensics.httpx, "post") as request:
            result = forensics.check_ai_generated(
                b"photo", "damage.jpg", "image/jpeg"
            )

        self.assertEqual(result["verdict"], "unavailable")
        self.assertIn("credentials", result["detail"])
        request.assert_not_called()
        undetermined = main.build_undetermined({
            "fields": [],
            "damage": [],
            "forensics": [result],
        })
        self.assertIn({
            "item": "AI-generation check",
            "reason": "Sightengine credentials are not configured",
            "impact": "photo authenticity not independently checked",
        }, undetermined)

    def test_suspicious_signal_flags_and_requires_review(self):
        checks = [{
            "check": "ai_generated",
            "verdict": "suspicious",
            "score": 0.91,
            "source_photo": "damage.jpg",
        }]
        flags = run_rules(
            [],
            [{"part": "front_bumper", "confidence": 0.9}],
            photo_count=1,
            is_vehicle=True,
            claimable_count=1,
            forensics=checks,
        )
        flag = next(item for item in flags if item["rule"] == "ai_generated_photo")
        self.assertEqual(flag["severity"], "HIGH")
        self.assertIn("damage.jpg", flag["message"])
        self.assertIn("0.91", flag["message"])
        assessment = assess_confidence({
            "status": "analysed",
            "damage": [{"part": "front_bumper", "confidence": 0.9}],
            "flags": flags,
            "claimable_count": 1,
            "estimate": {"requires_survey": False, "unpriced": []},
            "fields": [],
        })
        self.assertEqual(assessment["decision"], "needs_review")

    def test_inconclusive_is_undetermined(self):
        undetermined = main.build_undetermined({
            "fields": [],
            "damage": [],
            "forensics": [{
                "check": "ai_generated",
                "verdict": "inconclusive",
                "score": 0.62,
            }],
        })
        self.assertIn({
            "item": "Photo authenticity",
            "reason": "AI-generation score inconclusive (0.62)",
            "impact": "authenticity not confirmed",
        }, undetermined)

    def test_duplicate_and_non_vehicle_stop_before_sightengine(self):
        duplicate_path = os.path.join(self.folder.name, "duplicate.jpg")
        Image.new("RGB", (12, 12), "red").save(duplicate_path)
        duplicate_result = [{
            "check": "duplicate",
            "verdict": "suspicious",
            "detail": "match",
            "photo_path": os.path.abspath(duplicate_path),
        }]
        with patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: duplicate_result,
            build_photo_url_map=lambda paths: {},
            save_claim=lambda claim_id, payload: None,
        ), patch.object(main, "run_deep_forensics") as deep:
            asyncio.run(main.analyse_claim(
                "TEST-DUPLICATE", [], [duplicate_path]
            ))
        deep.assert_not_called()

        dog_path = os.path.join(self.folder.name, "dog.jpg")
        Image.new("RGB", (12, 12), "brown").save(dog_path)
        dog_result = {
            "is_vehicle": False,
            "vehicle_description": "A dog",
            "photos": [{
                "source_photo": "dog.jpg",
                "is_vehicle": False,
                "vehicle_group": None,
                "make_model": None,
                "colour": None,
                "body_type": None,
                "description": "A dog",
            }],
            "damage": [],
            "error": None,
        }
        with patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: [],
            build_photo_url_map=lambda paths: {},
            classify_image=lambda path: {
                "doc_type": "other",
                "confidence": 0.99,
                "number_plate": None,
            },
            assess_damage=lambda paths: dog_result,
            save_claim=lambda claim_id, payload: None,
        ), patch.object(main, "run_deep_forensics") as deep:
            asyncio.run(main.analyse_claim(
                "TEST-DOG", [], [dog_path]
            ))
        deep.assert_not_called()

        with patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: duplicate_result,
            build_photo_url_map=lambda paths: {},
            classify_image=lambda path: {
                "doc_type": "other",
                "confidence": 0.99,
                "number_plate": None,
            },
            assess_damage=lambda paths: dog_result,
            save_claim=lambda claim_id, payload: None,
        ), patch.object(main, "run_deep_forensics") as deep:
            payload = asyncio.run(main.analyse_claim(
                "TEST-DUPLICATE-DOG", [], [duplicate_path, dog_path]
            ))
        self.assertEqual(payload["stopped_at"], "not_a_vehicle")
        deep.assert_not_called()

    def test_mixed_bundle_sends_only_confirmed_vehicle_to_stage_two(self):
        car_path = os.path.join(self.folder.name, "car.jpg")
        dog_path = os.path.join(self.folder.name, "dog.jpg")
        Image.new("RGB", (12, 12), "blue").save(car_path)
        Image.new("RGB", (12, 12), "brown").save(dog_path)
        result = {
            "is_vehicle": True,
            "vehicle_description": "A car and a dog",
            "photos": [
                {
                    "source_photo": "car.jpg",
                    "is_vehicle": True,
                    "vehicle_group": 0,
                    "make_model": "Kia",
                    "colour": "blue",
                    "body_type": "sedan",
                    "description": "Damaged car",
                },
                {
                    "source_photo": "dog.jpg",
                    "is_vehicle": False,
                    "vehicle_group": None,
                    "make_model": None,
                    "colour": None,
                    "body_type": None,
                    "description": "A dog",
                },
            ],
            "damage": [{
                "part": "front_bumper",
                "damage_type": "dent",
                "severity": "moderate",
                "confidence": 0.9,
                "source_photo": "car.jpg",
            }],
            "error": None,
        }
        with patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: [],
            build_photo_url_map=lambda paths: {},
            classify_image=lambda path: {
                "doc_type": (
                    "vehicle_photo"
                    if os.path.basename(path) == "car.jpg"
                    else "other"
                ),
                "confidence": 0.99,
                "number_plate": None,
            },
            assess_damage=lambda paths: result,
            save_claim=lambda claim_id, payload: None,
        ), patch.object(
            main, "run_deep_forensics", return_value=[]
        ) as deep:
            asyncio.run(main.analyse_claim(
                "TEST-MIXED", [], [car_path, dog_path]
            ))

        self.assertEqual(deep.call_args.args[0], [car_path])

    def test_missing_keys_does_not_stop_claim(self):
        car_path = os.path.join(self.folder.name, "car-unavailable.jpg")
        Image.new("RGB", (24, 24), "silver").save(car_path)
        result = {
            "is_vehicle": True,
            "vehicle_description": "Silver car",
            "photos": [{
                "source_photo": "car-unavailable.jpg",
                "is_vehicle": True,
                "vehicle_group": 0,
                "make_model": "Kia",
                "colour": "silver",
                "body_type": "sedan",
                "description": "Damaged car",
            }],
            "damage": [{
                "part": "front_bumper",
                "damage_type": "dent",
                "severity": "moderate",
                "confidence": 0.9,
                "source_photo": "car-unavailable.jpg",
            }],
            "error": None,
        }
        with patch.dict(
            os.environ,
            {"SIGHTENGINE_API_USER": "", "SIGHTENGINE_API_SECRET": ""},
        ), patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: [],
            build_photo_url_map=lambda paths: {},
            classify_image=lambda path: {
                "doc_type": "vehicle_photo",
                "confidence": 0.99,
                "number_plate": None,
            },
            assess_damage=lambda paths: result,
            save_claim=lambda claim_id, payload: None,
        ), patch.object(forensics, "OUTPUT_DIR", Path(self.folder.name)):
            payload = asyncio.run(main.analyse_claim(
                "TEST-UNAVAILABLE", [], [car_path]
            ))

        self.assertEqual(payload["status"], "analysed")
        ai_check = next(
            item for item in payload["forensics"]
            if item["check"] == "ai_generated"
        )
        self.assertEqual(ai_check["verdict"], "unavailable")
        self.assertIn(
            "AI-generation check",
            {item["item"] for item in payload["undetermined"]},
        )

    def test_whatsapp_uses_neutral_authenticity_wording(self):
        damage = [{
            "part": "front_bumper",
            "damage_type": "dent",
            "severity": "moderate",
            "confidence": 0.9,
        }]
        estimate = build_estimate(damage)
        reply = whatsapp.format_reply({
            "claim_id": "CLM-1234",
            "status": "analysed",
            "stopped_at": None,
            "decision": "needs_review",
            "decision_reasons": [
                "damage.jpg shows strong signs of AI generation (score 0.91)."
            ],
            "vehicles": [{
                "group": 0,
                "label": "White Kia Sedan",
                "status": "claimable",
                "photos": ["damage.jpg"],
                "excluded_photos": [],
                "damage": damage,
                "estimate": estimate,
            }],
            "claimable_count": 1,
            "excluded_photos": [],
            "photos": [{"source_photo": "damage.jpg"}],
            "undetermined": [],
        })

        self.assertIn(
            "Flagged for manual review: photo authenticity requires verification.",
            reply,
        )
        lowered = reply.lower()
        self.assertNotIn("ai generation", lowered)
        self.assertNotIn("ai-generation", lowered)
        self.assertNotIn("fraud", lowered)


if __name__ == "__main__":
    unittest.main()
