import asyncio
import unittest
from unittest.mock import patch

import gemini
import main
from rules import assess_confidence


class FakeConnection:
    def close(self):
        pass


def damage(source_photo, part, severity="moderate"):
    return {
        "source_photo": source_photo,
        "part": part,
        "damage_type": "dent",
        "severity": severity,
        "confidence": 0.9,
        "reasoning": "Visible damage.",
    }


class VehicleGroupingTests(unittest.TestCase):
    def analyse(self, photos, damage_items, is_vehicle=True):
        result = {
            "is_vehicle": is_vehicle,
            "vehicle_description": "",
            "photos": photos,
            "damage": damage_items,
            "error": None,
        }
        with patch.multiple(
            main,
            get_conn=lambda: FakeConnection(),
            check_duplicates=lambda paths, conn: [],
            classify_image=lambda path: {
                "doc_type": "vehicle_photo",
                "confidence": 0.99,
                "number_plate": None,
            },
            assess_damage=lambda paths: result,
            run_deep_forensics=lambda paths, conn, fields=None: [],
            save_claim=lambda claim_id, payload: None,
        ):
            return asyncio.run(
                main.analyse_claim(
                    "TEST-VEHICLES",
                    [],
                    [photo["source_photo"] for photo in photos],
                )
            )

    def test_same_car_is_one_claimable_group(self):
        photos = [
            {
                "source_photo": "front.jpg",
                "is_vehicle": True,
                "vehicle_group": 0,
                "colour": "white",
                "make_model": "Kia",
                "body_type": "sedan",
            },
            {
                "source_photo": "rear.jpg",
                "is_vehicle": True,
                "vehicle_group": 0,
                "colour": "white",
                "make_model": "Kia",
                "body_type": "sedan",
            },
        ]
        payload = self.analyse(
            photos,
            [
                damage("front.jpg", "front_bumper"),
                damage("rear.jpg", "rear_bumper", "minor"),
            ],
        )

        self.assertEqual(payload["claimable_count"], 1)
        self.assertEqual(len(payload["vehicles"]), 1)
        vehicle = payload["vehicles"][0]
        self.assertEqual(vehicle["label"], "White Kia Sedan")
        self.assertEqual(vehicle["status"], "claimable")
        self.assertEqual(
            {item["source_photo"] for item in vehicle["damage"]},
            {"front.jpg", "rear.jpg"},
        )
        self.assertEqual(payload["estimate"], vehicle["estimate"])
        self.assertNotIn(
            "vehicle_mismatch",
            {flag["rule"] for flag in payload["flags"]},
        )

    def test_different_cars_have_separate_estimates(self):
        photos = [
            {
                "source_photo": "kia.jpg",
                "is_vehicle": True,
                "vehicle_group": 0,
                "colour": "white",
                "make_model": "Kia",
                "body_type": "sedan",
            },
            {
                "source_photo": "suv.jpg",
                "is_vehicle": True,
                "vehicle_group": 1,
                "colour": "black",
                "make_model": None,
                "body_type": "SUV",
            },
        ]
        payload = self.analyse(
            photos,
            [
                damage("kia.jpg", "front_bumper"),
                damage("suv.jpg", "bonnet", "severe"),
            ],
        )

        self.assertEqual(payload["claimable_count"], 2)
        self.assertEqual(
            [vehicle["status"] for vehicle in payload["vehicles"]],
            ["claimable", "claimable"],
        )
        self.assertEqual(payload["vehicles"][1]["label"], "Black SUV")
        self.assertEqual(payload["estimate"], payload["vehicles"][0]["estimate"])
        self.assertNotEqual(payload["estimate"], payload["vehicles"][1]["estimate"])
        mismatch = next(
            flag for flag in payload["flags"] if flag["rule"] == "vehicle_mismatch"
        )
        self.assertEqual(
            mismatch["message"],
            "Photos show 2 different vehicles. A claim covers one vehicle.",
        )
        self.assertEqual(mismatch["evidence"], ["White Kia Sedan", "Black SUV"])

    def test_undamaged_second_car_is_no_damage(self):
        photos = [
            {
                "source_photo": "damaged.jpg",
                "is_vehicle": True,
                "vehicle_group": 0,
                "colour": "silver",
                "make_model": "BMW",
                "body_type": "sedan",
            },
            {
                "source_photo": "clean.jpg",
                "is_vehicle": True,
                "vehicle_group": 1,
                "colour": "blue",
                "make_model": "Kia",
                "body_type": "sedan",
            },
        ]
        payload = self.analyse(
            photos,
            [damage("damaged.jpg", "bonnet", "severe")],
        )

        self.assertEqual(payload["claimable_count"], 1)
        self.assertEqual(
            [vehicle["status"] for vehicle in payload["vehicles"]],
            ["claimable", "no_damage"],
        )
        self.assertEqual(payload["vehicles"][1]["estimate"]["total_high"], 0)

    def test_non_vehicle_gets_its_own_group(self):
        photos = [
            {
                "source_photo": "car.jpg",
                "is_vehicle": True,
                "vehicle_group": 0,
                "colour": "red",
                "make_model": "Honda",
                "body_type": "sedan",
            },
            {
                "source_photo": "receipt.jpg",
                "is_vehicle": False,
                "vehicle_group": None,
                "colour": None,
                "make_model": None,
                "body_type": None,
            },
        ]
        payload = self.analyse(
            photos,
            [damage("car.jpg", "front_bumper")],
        )

        self.assertEqual(payload["claimable_count"], 1)
        self.assertEqual(
            [vehicle["status"] for vehicle in payload["vehicles"]],
            ["claimable", "not_vehicle"],
        )
        self.assertEqual(payload["vehicles"][1]["photos"], ["receipt.jpg"])

    def test_model_output_normalizes_group_and_source_photo(self):
        photos = gemini._normalise_photos(
            [{
                "source_photo": "car.jpg",
                "is_vehicle": True,
                "vehicle_group": "0",
            }],
            ["C:/claims/car.jpg"],
        )
        damage_items = gemini._normalise_damage([{
            "part": "hood",
            "damage_type": "dent",
            "severity": "moderate",
            "source_photo": "folder/car.jpg",
        }])

        self.assertEqual(photos[0]["vehicle_group"], 0)
        self.assertIn("source_photo", damage_items[0])
        self.assertEqual(damage_items[0]["source_photo"], "car.jpg")

    def test_vehicle_label_preserves_model_tokens(self):
        self.assertEqual(
            main.format_vehicle_label([
                "silver", "maruti suzuki swift vxi", "sedan",
            ]),
            "Silver Maruti Suzuki Swift VXI Sedan",
        )
        self.assertEqual(
            main.format_vehicle_label(["grey", "honda cr-v", "suv"]),
            "Grey Honda CR-V SUV",
        )

    def test_whatsapp_unverifiable_exif_is_informational(self):
        assessment = assess_confidence({
            "status": "analysed",
            "source": "whatsapp",
            "damage": [{"part": "rear_bumper", "confidence": 0.9}],
            "flags": [],
            "claimable_count": 1,
            "estimate": {
                "requires_survey": False,
                "unpriced": [],
            },
            "fields": [],
            "forensics": [{
                "check": "exif",
                "verdict": "unverifiable",
            }],
        })

        self.assertEqual(assessment["decision"], "auto_assess")
        self.assertEqual(assessment["reasons"], [])


if __name__ == "__main__":
    unittest.main()
