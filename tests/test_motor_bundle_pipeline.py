import asyncio
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import gemini
import main
from rules import parse_date


FIXTURE = Path(__file__).parent / "fixtures" / "Synthetic Motor Claim Test Bundle.pdf"
PHOTO_NAME = "synthetic_damage.jpg"

FIELD_VALUES = {
    "insured_name_claim_form": "R. K. Sharma",
    "owner_name_rc": "RAJESH KUMAR SHARMA",
    "holder_name_dl": "RAJESH KUMAR SHARMA",
    "registration_number_claim_form": "MH-12-AB-1234",
    "registration_number_rc": "MH-12-AB-1234",
    "chassis_number": "MA3EJKD1S00123456",
    "engine_number": "K12MN9876543",
    "vehicle_make_model": "Maruti Suzuki Swift VXI",
    "dl_number": "MH12-20040045678",
    "dl_expiry_date": "12-02-2024",
    "policy_number": "MOT-NIA-2023-01948271",
    "policy_start_date": "01-04-2023",
    "policy_end_date": "31-03-2024",
    "accident_date": "10-03-2024",
    "accident_time": "14:15",
    "accident_location": "Paud Road Junction, Kothrud, Pune, Maharashtra - 411038",
    "accident_description": (
        "Low-speed collision caused a deep dent and horizontal crack on "
        "the front bumper."
    ),
}


class FakeConnection:
    def close(self):
        pass


class MotorBundlePipelineTests(unittest.TestCase):
    def test_motor_bundle_full_pipeline(self):
        source_doc = FIXTURE.name
        extracted_fields = [
            {
                "name": name,
                "value": value,
                "source_doc": source_doc,
                "confidence": 1.0,
                "status": "found",
            }
            for name, value in FIELD_VALUES.items()
        ]

        def fake_generate(parts, label):
            self.assertEqual(label, "extract_fields(1 docs)")
            image_parts = [part for part in parts if part[0] == "image"]
            self.assertEqual(len(image_parts), 3)
            return {"fields": extracted_fields}

        damage_result = {
            "is_vehicle": True,
            "vehicle_description": "Silver Maruti Suzuki Swift sedan",
            "photos": [{
                "source_photo": PHOTO_NAME,
                "is_vehicle": True,
                "vehicle_group": 0,
                "make_model": "Maruti Suzuki Swift VXI",
                "colour": "silver",
                "body_type": "sedan",
                "description": "Front bumper damage.",
            }],
            "damage": [
                {
                    "part": "front_bumper",
                    "damage_type": "dent",
                    "severity": "moderate",
                    "confidence": 1.0,
                    "reasoning": "A deep dent is visible.",
                    "source_photo": PHOTO_NAME,
                },
                {
                    "part": "front_bumper",
                    "damage_type": "crack",
                    "severity": "severe",
                    "confidence": 1.0,
                    "reasoning": "A horizontal crack is visible.",
                    "source_photo": PHOTO_NAME,
                },
            ],
            "error": None,
        }

        with (
            patch.object(gemini, "generate_json", side_effect=fake_generate),
            patch.multiple(
                main,
                get_conn=lambda: FakeConnection(),
                check_duplicates=lambda paths, conn: [],
                assess_damage=lambda paths: damage_result,
                run_deep_forensics=lambda paths, conn, fields=None: [],
                save_claim=lambda claim_id, payload: None,
            ),
        ):
            payload = asyncio.run(
                main.analyse_claim(
                    "TEST-MOTOR-BUNDLE",
                    [str(FIXTURE)],
                    [os.path.join("fixtures", PHOTO_NAME)],
                )
            )

        fields = payload["fields"]
        self.assertEqual(len(fields), 17)
        self.assertTrue(
            all(field["status"] == "found" for field in fields if field["value"])
        )

        rules = {flag["rule"] for flag in payload["flags"]}
        self.assertNotIn("name_mismatch", rules)
        self.assertNotIn("registration_mismatch", rules)
        self.assertIn("dl_expired_at_loss", rules)
        self.assertNotIn("policy_not_active", rules)

        accident_date, ambiguous = parse_date(
            next(
                field["value"]
                for field in fields
                if field["name"] == "accident_date"
            )
        )
        self.assertEqual(accident_date, date(2024, 3, 10))
        self.assertTrue(ambiguous)

        line_items = payload["estimate"]["line_items"]
        self.assertEqual(len(line_items), 1)
        self.assertEqual(line_items[0]["part"], "front_bumper")
        self.assertEqual(line_items[0]["damage_type"], "crack")
        self.assertEqual(line_items[0]["severity"], "severe")
        self.assertEqual(line_items[0]["also_reported"], ["dent"])


if __name__ == "__main__":
    unittest.main()
