try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import unittest

import rules


def field(name, value):
    return {"name": name, "value": value, "status": "found"}


class IndianNameMatchTests(unittest.TestCase):
    def test_every_pair_from_the_audit(self):
        cases = [
            # The pairs that raised a false HIGH mismatch before.
            ("S Karthik", "Karthik S", "match"),
            ("S. Karthik", "Karthik Subramanian", "match"),
            ("K R Priya", "Priya K R", "match"),
            ("Mohd Rafiq", "Mohammed Rafiq", "match"),
            ("MD. Rafiq", "Muhammad Rafiq", "match"),
            ("Ramesh S/O Krishnan", "Ramesh", "match"),
            ("Ramesh S.O. Krishnan", "Ramesh", "match"),
            ("Kavita W/O Suresh Rao", "Kavita", "match"),
            ("ABC Pvt Ltd", "ABC Private Limited", "match"),
            ("M/S ABC Pvt. Ltd.", "ABC Private Limited", "match"),
            ("Rajesh Kumar", "Rajesh Kumar Singh", "partial"),
            ("Priya Iyer", "Priya Ramesh Iyer", "partial"),
            # Honorifics and case.
            ("Shri ARJUN MEHTA", "arjun mehta", "match"),
            ("A. Mehta", "ARJUN MEHTA", "match"),
            # Different people stay different.
            ("Arjun Mehta", "Rohan Das", "mismatch"),
            ("Kavita Rao", "Imran Shaikh", "mismatch"),
            ("Rajesh Kumar", "Rakesh Kumar", "mismatch"),  # shared surname only
            ("Rajesh Kumar", "Suresh Verma", "mismatch"),
            # Initials alone prove nothing.
            ("S K", "Sunil Kumar", "mismatch"),
            ("", "Sunil Kumar", "mismatch"),
        ]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(rules.name_match(a, b), expected)
                self.assertEqual(rules.name_match(b, a), expected)

    def test_partial_match_is_an_info_note_never_a_flag(self):
        fields = [
            field("insured_name_claim_form", "Rajesh Kumar"),
            field("owner_name_rc", "Rajesh Kumar Singh"),
            field("driver_name_claim_form", "S Karthik"),
            field("holder_name_dl", "Karthik S"),
        ]
        flags = rules.run_rules(fields, [])
        self.assertEqual([f["rule"] for f in flags], [])
        notes = rules.identity_notes(fields)
        # The owner and the licence holder are different people: INFO as well.
        self.assertEqual([n["rule"] for n in notes],
                         ["insured_owner_partial_match", "driver_not_owner"])
        self.assertEqual(notes[0]["severity"], "INFO")
        self.assertIn("in part", notes[0]["message"])

    def test_a_real_mismatch_is_still_high(self):
        fields = [
            field("insured_name_claim_form", "Arjun Mehta"),
            field("owner_name_rc", "Rohan Das"),
        ]
        flags = {f["rule"]: f["severity"] for f in rules.run_rules(fields, [])}
        self.assertEqual(flags, {"insured_owner_mismatch": "HIGH"})

    def test_owner_and_licence_holder_written_differently_are_one_person(self):
        fields = [field("owner_name_rc", "K R Priya"), field("holder_name_dl", "Priya K R")]
        self.assertEqual(rules.identity_notes(fields), [])



class SurveyCitationTests(unittest.TestCase):
    def test_survey_reason_cites_the_master_circular(self):
        damage = [{"part": part, "damage_type": "dent", "severity": "severe", "confidence": 0.9}
                  for part in ("roof", "bonnet", "boot")]
        estimate = rules.build_estimate(damage)
        self.assertTrue(estimate["requires_survey"])
        self.assertEqual(
            estimate["survey_reason"],
            "IRDAI Master Circular on Protection of Policyholders' Interests, 2024: "
            "motor losses below Rs 50,000 need not be surveyed by a registered surveyor.",
        )
        self.assertEqual(rules.build_estimate(damage[:1])["survey_reason"], "")



class ClaimedVehiclePlateTests(unittest.TestCase):
    RC = [{"name": "registration_number_rc", "value": "MH-12-QZ-4821", "status": "found"}]

    def vehicles(self, mine_status="claimable", other_status="claimable"):
        return [
            {"group": 0, "status": mine_status, "label": "White Honda City",
             "photos": ["mine.jpg"]},
            {"group": 1, "status": other_status, "label": "Red Truck",
             "photos": ["other.jpg"]},
        ]

    def plates(self, mine, other):
        return [
            {"source_photo": "uploads/CLM-1/mine.jpg", "number_plate": mine, "vehicle_group": 0},
            {"source_photo": "uploads/CLM-1/other.jpg", "number_plate": other, "vehicle_group": 1},
        ]

    def identity_flags(self, photos, vehicles, claimable):
        flags = rules.run_rules(self.RC, [], photos=photos, vehicles=vehicles,
                                claimable_count=claimable)
        return [f for f in flags if f["rule"] == "vehicle_identity_mismatch"], flags

    def test_the_other_partys_plate_is_not_a_mismatch(self):
        found, flags = self.identity_flags(
            self.plates("MH12QZ4821", "KA01AB1234"), self.vehicles(), 2)
        self.assertEqual(found, [])
        # The second vehicle is still reported, once.
        self.assertEqual([f["rule"] for f in flags], ["vehicle_mismatch"])

    def test_a_wrong_plate_on_the_claimed_vehicle_is_still_high(self):
        found, _ = self.identity_flags(
            self.plates("MH12QZ9999", "KA01AB1234"), self.vehicles(other_status="no_damage"), 1)
        self.assertEqual(len(found), 1)
        self.assertIn("MH12QZ9999", found[0]["message"])

    def test_without_a_claimable_vehicle_every_plate_is_checked(self):
        found, _ = self.identity_flags(
            self.plates("MH12QZ4821", "KA01AB1234"),
            self.vehicles("no_damage", "no_damage"), 0)
        self.assertEqual([f["evidence"][1] for f in found], ["KA01AB1234"])

if __name__ == "__main__":
    unittest.main()
