"""Rules for the demo sets, on synthetic data only (the real sets are real
people's documents and never enter the repo): plate shapes, the registry
sync, the vehicle normaliser, licence NT/TR validity, RC registration
validity, late intimation, organisation owners, assessors."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import assessors
import db
import gemini
import main
import registry
import rules
import vehicle_terms as vt
import vision
import whatsapp


def field(name, value, status="found"):
    return {"name": name, "value": value, "status": status, "confidence": 0.9}


def flags_of(fields, **kwargs):
    return {flag["rule"]: flag for flag in rules.run_rules(fields, [], **kwargs)}


class PlateShapeTests(unittest.TestCase):
    def test_single_letter_series_and_union_territory_codes(self):
        for plate in ("DD01A1234", "CH02BC5678", "AN02Z9999", "SK04G0001", "MN05AB0123"):
            series = plate[4:-4]
            for text in (plate, plate.lower(), f"{plate[:2]} {plate[2:4]} {series} {plate[-4:]}",
                         f"{plate[:2]}-{plate[2:4]}-{series}-{plate[-4:]}".lower()):
                self.assertTrue(registry.valid_plate(text), text)
                self.assertEqual(registry.normalise_plate(text), plate)

    def test_bad_shapes_are_refused(self):
        for text in ("DD01A123", "D01A1234", "DD01ABCD1234", "1234", "DDAA1234"):
            self.assertFalse(registry.valid_plate(text), text)


class RegistrySyncTests(unittest.TestCase):
    ROW = {"plate": "DD01A1234", "owner_name": "TEST OWNER ONE", "owner_mobile": "910000000001",
           "make": "Hyundai", "model": "Grand i10 Nios", "colour": "blue",
           "body_type": "hatchback", "chassis_last5": "01234", "engine_last5": "00999",
           "policy_type": "comprehensive", "policy_start": "2026-01-01",
           "policy_end": "2026-12-31", "insurer": "Test Insurer", "city": "Daman", "idv": 1,
           "ncb_percent": 20}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "registry.db")
        db.prepare_test_db()
        registry._LOADED["fingerprint"] = None

    def tearDown(self):
        registry._LOADED["fingerprint"] = None
        db.DB_PATH = self.original
        self.folder.cleanup()

    def load_with(self, rows):
        with patch.dict(os.environ, {"REGISTRY_PRIVATE_ROWS": json.dumps(rows)}):
            return registry.load()

    def test_private_rows_join_the_public_rows_and_win_a_plate(self):
        public_plate = registry.records()[0]["plate"]
        overriding = dict(self.ROW, plate=public_plate, owner_name="PRIVATE WINS")
        count = self.load_with([self.ROW, overriding])
        self.assertEqual(count, 101)  # 100 public, one new, one replaced
        self.assertEqual(db.registry_get(public_plate)["owner_name"], "PRIVATE WINS")
        record = db.registry_get("DD01A1234")
        self.assertEqual((record["chassis_last5"], record["engine_last5"]), ("01234", "00999"))

    def test_an_edit_with_the_same_row_count_is_applied(self):
        self.load_with([self.ROW])
        # A redeploy with an edited row: same count, new content.
        self.load_with([dict(self.ROW, chassis_last5="")])
        self.assertEqual(db.registry_get("DD01A1234")["chassis_last5"], "")
        registry._LOADED["fingerprint"] = None  # a new process (restart)
        self.load_with([dict(self.ROW, chassis_last5="54321")])
        self.assertEqual(db.registry_get("DD01A1234")["chassis_last5"], "54321")

    def test_unchanged_rows_are_not_rewritten_in_one_process(self):
        self.load_with([self.ROW])
        with patch.object(db, "registry_replace") as replace:
            self.load_with([self.ROW])
        replace.assert_not_called()

    def test_tests_never_see_real_private_rows(self):
        self.assertEqual(os.environ.get("REGISTRY_PRIVATE_ROWS"), "[]")
        self.assertEqual(len(registry.records()), 100)


class NormaliserTests(unittest.TestCase):
    def test_makes(self):
        for text, expected in (
            ("TOYOTA KIRLOSKAR MOTOR PVT LTD", "toyota"), ("HYUNDAI MOTOR INDIA LTD", "hyundai"),
            ("TATA MOTORS LTD", "tata"), ("Tata", "tata"), ("MARUTI SUZUKI INDIA LTD", "maruti suzuki"),
            ("Maruti", "maruti suzuki"), ("Suzuki", "maruti suzuki"),
            ("MAHINDRA & MAHINDRA LIMITED", "mahindra"), ("M&M", "mahindra"),
            ("Brezza", "maruti suzuki"), ("RIGID (PASSENGER CAR)", None), ("", None),
        ):
            self.assertEqual(vt.make(text), expected, text)

    def test_models_ignore_spacing_and_case(self):
        for a, b in (("V BREZZA ZXI", "Vitara Brezza"), ("V BREZZA ZXI", "Brezza"),
                     ("GRANDI10NIOS1.2MTKAPPASP", "Grand i10 Nios"),
                     ("COROLLA ALTIS D4D G", "Corolla Altis"),
                     ("TATA INDICA VISTA AURA SAFIRE", "Indica Vista")):
            self.assertEqual(vt.model_relation(a, b), "same", (a, b))
        self.assertEqual(vt.model_relation("Grand i10 Nios", "i20"), "different")
        self.assertIsNone(vt.model_relation("Unknown Model X", "Swift"))

    def test_colour_families(self):
        for text, family in (("TEAL BLUE", "blue"), ("teal-blue", "blue"), ("Turquoise", "blue"),
                             ("PEARL ARCTIC WHITE", "white"), ("DIAMOND WHITE", "white"),
                             ("M-SILVER", "silver"), ("SILVER MM", "silver"),
                             ("PEARL MIDNIGHT BLACK", "black"), ("MOONDUST", None)):
            self.assertEqual(vt.colour(text), family, text)
        self.assertEqual(vt.colour_relation("TEAL BLUE", "green"), "near")
        self.assertEqual(vt.colour_relation("M-SILVER", "grey"), "near")
        self.assertEqual(vt.colour_relation("PEARL ARCTIC WHITE", "white"), "same")
        self.assertEqual(vt.colour_relation("white", "red"), "different")
        self.assertIsNone(vt.colour_relation("STARDUST", "white"))

    def test_body_types(self):
        for text, body in (("SALOON", "sedan"), ("HATCH BACK", "hatchback"), ("HARD TOP", "suv"),
                           ("crossover", "suv"), ("MUV", "suv"), ("compact SUV", "suv"),
                           ("RIGID (PASSENGER CAR)", None), ("", None)):
            self.assertEqual(vt.body(text), body, text)

    RECORD = {"plate": "AN02Z9999", "make": "Maruti Suzuki", "model": "Vitara Brezza",
              "colour": "PEARL ARCTIC WHITE", "body_type": "SUV"}

    def test_the_same_vehicle_in_raw_rc_words_raises_nothing(self):
        seen = {"colour": "white", "make_model": "MARUTI SUZUKI INDIA LTD V BREZZA ZXI",
                "body_type": "compact SUV"}
        self.assertIsNone(rules.appearance_mismatch(self.RECORD, seen))
        fields = [field("vehicle_make_model", "MARUTI SUZUKI INDIA LTD / V BREZZA ZXI")]
        self.assertEqual(rules.rc_identity_flags(fields, self.RECORD), [])

    def test_near_colours_and_unknown_values_are_skipped(self):
        record = dict(self.RECORD, colour="M-SILVER", body_type="RIGID (PASSENGER CAR)")
        seen = {"colour": "grey", "make_model": None, "body_type": "hatchback"}
        self.assertIsNone(rules.appearance_mismatch(record, seen))

    def test_a_white_suv_is_not_a_red_hatchback(self):
        record = dict(self.RECORD, make="Hyundai", model="Creta")
        seen = {"colour": "red", "make_model": "Maruti Suzuki Swift", "body_type": "hatchback"}
        severity, _ = rules.appearance_mismatch(record, seen)
        self.assertEqual(severity, "HIGH")
        self.assertLess(rules.vehicle_matches_registry(record, seen), 0)

    def test_rc_make_and_model_against_the_registry(self):
        other_make = rules.rc_identity_flags(
            [field("vehicle_make_model", "HYUNDAI MOTOR INDIA LTD CRETA")], self.RECORD)
        self.assertEqual([(f["rule"], f["severity"]) for f in other_make],
                         [("registry_rc_make_mismatch", "HIGH")])
        other_model = rules.rc_identity_flags(
            [field("vehicle_make_model", "MARUTI SUZUKI INDIA LTD SWIFT VXI")], self.RECORD)
        self.assertEqual([(f["rule"], f["severity"], f["review"]) for f in other_model],
                         [("registry_rc_model_mismatch", "MEDIUM", True)])
        unknown = rules.rc_identity_flags([field("vehicle_make_model", "RIGID")], self.RECORD)
        self.assertEqual(unknown, [])


class ChassisTests(unittest.TestCase):
    RECORD = {"plate": "CH02BC5678", "make": "Toyota", "model": "Corolla Altis",
              "chassis_last5": "12345", "engine_last5": "0O678"}

    def rules_for(self, chassis, record=None):
        return [f["rule"] for f in rules.rc_identity_flags(
            [field("chassis_number", chassis)], record or self.RECORD)]

    def test_last_five_are_compared(self):
        self.assertEqual(self.rules_for("MAT00000000012345"), [])
        self.assertEqual(self.rules_for("MAT00000000012346"), ["registry_chassis_mismatch"])
        flag = rules.rc_identity_flags([field("chassis_number", "MAT00000000099999")],
                                       self.RECORD)[0]
        self.assertEqual((flag["severity"], flag["review"]), ("MEDIUM", True))
        self.assertEqual(flag["comparison"]["verdict"], "MISMATCH")

    def test_an_unreadable_character_or_a_blank_skips_it(self):
        self.assertEqual(self.rules_for("MAT0000000001?345"), [])
        self.assertEqual(self.rules_for("MAT00000000099999", dict(self.RECORD, chassis_last5="")), [])
        self.assertEqual(self.rules_for(None), [])

    def test_vins_have_no_o_i_or_q(self):
        engine = rules.rc_identity_flags([field("engine_number", "ENG00678")], self.RECORD)
        self.assertEqual(engine, [])  # registry "0O678" is read as 00678


class LicenceValidityTests(unittest.TestCase):
    ACCIDENT = field("accident_date", "2026-10-15")

    def test_transport_expired_and_non_transport_valid_raises_nothing(self):
        fields = [self.ACCIDENT, field("licence_expiry_nt", "2031-06-27"),
                  field("licence_expiry_tr", "2026-06-27"), field("dl_expiry_date", "2031-06-27")]
        self.assertNotIn("dl_expired_at_loss", flags_of(fields))
        checks = {c["rule"]: c for c in rules.passed_checks(fields)}
        self.assertEqual(checks["dl_valid_at_loss"]["comparison"]["rows"][0]["value"], "2031-06-27")

    def test_non_transport_expired_is_high(self):
        fields = [self.ACCIDENT, field("licence_expiry_nt", "2026-06-27"),
                  field("licence_expiry_tr", "2031-06-27")]
        flag = flags_of(fields)["dl_expired_at_loss"]
        self.assertEqual(flag["severity"], "HIGH")
        self.assertIn("non-transport", flag["message"])

    def test_a_single_date_behaves_as_before(self):
        self.assertIn("dl_expired_at_loss", flags_of([self.ACCIDENT,
                                                      field("dl_expiry_date", "2023-11-14")]))
        self.assertNotIn("dl_expired_at_loss", flags_of([self.ACCIDENT,
                                                         field("dl_expiry_date", "2030-01-01")]))

    def test_two_unlabelled_dates_use_the_later_one(self):
        fields = [self.ACCIDENT, field("dl_expiry_date", "2031-06-27"),
                  field("licence_expiry_tr", "2026-06-27")]
        self.assertEqual(rules.licence_validity(fields)[0], date(2031, 6, 27))

    def test_a_transport_vehicle_needs_transport_validity(self):
        fields = [self.ACCIDENT, field("licence_expiry_nt", "2031-06-27"),
                  field("licence_expiry_tr", "2026-06-27")]
        self.assertIn("dl_expired_at_loss",
                      flags_of(fields, registry={"vehicle_class": "Transport (goods)"}))

    def test_dl_expiry_date_carries_the_non_transport_date(self):
        fields = gemini.reconcile_licence_dates([
            field("dl_expiry_date", "2026-06-27"), field("licence_expiry_nt", "2031-06-27"),
            field("licence_expiry_tr", "2026-06-27")])
        self.assertEqual(rules.get_field(fields, "dl_expiry_date"), "2031-06-27")

    def test_the_licence_prompt_asks_for_both_dates(self):
        self.assertIn("licence_expiry_nt", gemini.DOCUMENT_FIELDS["driving_licence"])
        self.assertIn("licence_expiry_tr", gemini.DOCUMENT_FIELDS["driving_licence"])
        self.assertIn("Non-Transport", gemini.DOCUMENT_NOTES["driving_licence"])

    def test_a_changed_prompt_is_a_new_cache_key(self):
        image = ("image", "image/jpeg", b"same image bytes")
        old = vision._content_hash([image, ("text", "old prompt")], "extract_fields(dl.jpg)",
                                   "groq", "model")
        new = vision._content_hash([image, ("text", "new prompt")], "extract_fields(dl.jpg)",
                                   "groq", "model")
        self.assertNotEqual(old, new)


class RegistrationValidityTests(unittest.TestCase):
    def test_an_accident_after_the_registration_ended_is_high(self):
        fields = [field("accident_date", "2026-10-15"), field("registration_valid_until", "2026-03-23")]
        flag = flags_of(fields)["registration_expired_at_loss"]
        self.assertEqual(flag["severity"], "HIGH")
        self.assertTrue(flag["message"].startswith("Registration had expired before the accident"))
        self.assertEqual(flag["comparison"], {"rows": [
            {"source": "Accident date", "value": "2026-10-15"},
            {"source": "RC registration valid until", "value": "2026-03-23"}],
            "verdict": "EXPIRED"})

    def test_valid_on_the_day_raises_nothing(self):
        fields = [field("accident_date", "2026-03-10"), field("registration_valid_until", "2026-03-23")]
        self.assertNotIn("registration_expired_at_loss", flags_of(fields))
        self.assertIn("registration_valid_at_loss",
                      {c["rule"] for c in rules.passed_checks(fields)})

    def test_not_read_is_silent(self):
        fields = [field("accident_date", "2026-10-15"),
                  field("registration_valid_until", None, "unreadable")]
        self.assertEqual(flags_of(fields), {})
        payload = {"fields": fields}
        self.assertEqual(main.build_undetermined(payload), [])
        self.assertEqual(rules.assess_confidence({"status": "analysed", **payload})["decision"],
                         "auto_assess")

    def test_the_customer_is_never_told(self):
        flag = flags_of([field("accident_date", "2026-10-15"),
                         field("registration_valid_until", "2026-03-23")])["registration_expired_at_loss"]
        payload = {"claim_id": "CLM-TEST0001", "status": "analysed", "decision": "needs_review",
                   "decision_reasons": [flag["message"]], "flags": [flag], "estimate": {},
                   "damage": [], "undetermined": [], "vehicles": []}
        text = whatsapp.format_reply(payload)
        self.assertNotIn("Registration had expired", text)
        self.assertNotIn("2026-03-23", text)


class LateIntimationTests(unittest.TestCase):
    def flags(self, accident, submitted, **env):
        with patch.dict(os.environ, env):
            return flags_of([field("accident_date", accident)], submitted_on=submitted)

    def test_more_than_thirty_days_is_medium_and_goes_to_review(self):
        flag = self.flags("2026-03-10", date(2026, 10, 20))["late_intimation"]
        self.assertEqual((flag["severity"], flag["review"]), ("MEDIUM", True))
        self.assertEqual(flag["message"],
                         "Reported 224 days after the accident — ask the customer for the reason.")
        decision = rules.assess_confidence({"status": "analysed", "flags": [flag]})
        self.assertEqual(decision["decision"], "needs_review")  # never reject

    def test_thirty_days_or_a_longer_setting_is_not_late(self):
        self.assertNotIn("late_intimation", self.flags("2026-09-20", date(2026, 10, 20)))
        self.assertNotIn("late_intimation",
                         self.flags("2026-08-01", date(2026, 10, 20), LATE_INTIMATION_DAYS="90"))

    def test_an_accident_after_submission_is_high(self):
        flag = self.flags("2026-11-01", date(2026, 10, 20))["accident_after_submission"]
        self.assertEqual(flag["severity"], "HIGH")

    def test_an_accident_before_the_policy_start_is_still_caught(self):
        record = {"plate": "SK04G0001", "policy_start": "2026-06-24", "policy_end": "2027-06-23",
                  "policy_type": "comprehensive", "insurer": "Test Insurer"}
        flags = flags_of([field("accident_date", "2026-06-01")], registry=record,
                         submitted_on=date(2026, 6, 10))
        self.assertEqual(flags["policy_not_active"]["severity"], "HIGH")

    def test_submission_day_is_india_time(self):
        self.assertEqual(main.submitted_date("2026-10-19T20:00:00+00:00"), date(2026, 10, 20))
        self.assertEqual(main.submitted_date("2026-10-19T10:00:00+00:00"), date(2026, 10, 19))


class OrganisationNameTests(unittest.TestCase):
    def test_department_spellings_spacing_and_the(self):
        for a, b in (("THE TEST DIRECTOR , SAMPLE DEPTT", "The Test Director, Sample Department"),
                     ("THE DEPUTY COMMISSIONER , TRANSPORT DEPTT",
                      "Deputy Commissioner Transport Department"),
                     ("THE DEPUTY COMMISSIONER, TRANSPORT DEPT.", "the deputy commissioner transport deptt")):
            self.assertEqual(rules.name_match(a, b), "match", (a, b))

    def test_an_organisation_is_never_matched_by_initials(self):
        self.assertEqual(rules.name_match("T D Sample Department", "Test Director Sample Department"),
                         "mismatch")
        self.assertEqual(rules.name_match("THE DEPUTY COMMISSIONER , TRANSPORT DEPTT",
                                          "Rakesh Kumar"), "mismatch")

    def test_the_insured_organisation_matches_the_rc(self):
        fields = [field("insured_name_claim_form", "The Test Director, Sample Department"),
                  field("owner_name_rc", "THE TEST DIRECTOR , SAMPLE DEPTT")]
        self.assertNotIn("insured_owner_mismatch", flags_of(fields))

    def test_person_names_are_unchanged(self):
        self.assertEqual(rules.name_match("R K Sharma", "Rajesh Kumar Sharma"), "match")
        self.assertEqual(rules.name_match("Rajesh Kumar", "Rajesh Kumar Singh"), "partial")
        self.assertEqual(rules.name_match("Suresh Verma", "Rajesh Kumar Sharma"), "mismatch")


class AssessorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "assessors.db")
        db.prepare_test_db()

    def tearDown(self):
        db.DB_PATH = self.original
        self.folder.cleanup()

    def test_every_new_city_has_its_own_assessor(self):
        for city in ("Daman", "Chandigarh", "Port Blair", "Gangtok", "Imphal"):
            chosen = assessors.assign(city)
            self.assertEqual(chosen["city"], city)
            self.assertTrue(chosen["name"])

    def test_a_city_without_one_goes_to_its_region_then_anyone(self):
        self.assertIn(assessors.assign("Shillong")["city"], ("Gangtok", "Imphal"))
        self.assertTrue(assessors.assign("Nowhere")["name"])
        self.assertTrue(assessors.assign(None)["name"])

    def test_a_database_failure_still_assigns(self):
        with patch.object(db, "assessors_list", side_effect=RuntimeError("down")):
            chosen = assessors.assign("Imphal")
        self.assertEqual(chosen["city"], "Imphal")


if __name__ == "__main__":
    unittest.main()
