"""Dashboard batch: sign-in, sessions, the custom header, the actor on every
override, the feed's final decision, request info end to end, comparisons."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import json
import os
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import db
import main
import rules
import whatsapp

try:
    from tests import test_whatsapp_journey as journey
except ImportError:
    import test_whatsapp_journey as journey

HEADERS = {"X-Requested-With": "InsureIT"}
TOKEN = "test-admin-token"


def console_users(**passwords):
    """CONSOLE_USERS for test users, with cheap hashes (the cost is stored)."""
    return json.dumps([
        {"username": name, "display_name": name.title() + " Tester", "city": "Pune",
         "password_hash": auth.hash_password(password, iterations=1000)}
        for name, password in passwords.items()
    ])


class HashTests(unittest.TestCase):
    def test_pbkdf2_with_a_salt_per_hash(self):
        first, second = auth.hash_password("pw-one"), auth.hash_password("pw-one")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("pbkdf2_sha256$310000$"))
        self.assertTrue(auth.verify_password("pw-one", first))
        self.assertFalse(auth.verify_password("pw-two", first))
        self.assertFalse(auth.verify_password("pw-one", "not-a-hash"))

    def test_the_script_never_prints_the_password(self):
        import io
        from contextlib import redirect_stdout
        from scripts import hash_password as script

        out = io.StringIO()
        with patch("getpass.getpass", side_effect=["s3cret-value", "s3cret-value"]), \
                redirect_stdout(out):
            script.main(["--json", "deva", "Deva H", "Pune"])
        entry = json.loads(out.getvalue())
        self.assertNotIn("s3cret-value", out.getvalue())
        self.assertEqual(entry["username"], "deva")
        self.assertTrue(auth.verify_password("s3cret-value", entry["password_hash"]))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "console.db")
        db.prepare_test_db()
        auth.reset_state()
        main.forget_feed_state()
        self.password = "pw-" + secrets.token_hex(8)
        self.env = patch.dict(os.environ, {
            "ADMIN_TOKEN": TOKEN, "SESSION_SECRET": "test-session-secret",
            "CONSOLE_USERS": console_users(meera=self.password, ravi="other-" + self.password),
        })
        self.env.start()
        # Secure cookies are only sent back over https.
        self.client = TestClient(main.app, base_url="https://testserver")
        db.save_claim("CLM-AUTH0001", {"claim_id": "CLM-AUTH0001", "status": "analysed",
                                       "decision": "needs_review",
                                       "created_at": db.utc_now_iso()})

    def tearDown(self):
        self.env.stop()
        auth.reset_state()
        main.forget_feed_state()
        db.DB_PATH = self.original_db
        self.folder.cleanup()

    def login(self, username="meera", password=None, headers=HEADERS):
        return self.client.post("/api/login", headers=headers,
                                json={"username": username, "password": password or self.password})

    def approve(self, headers=HEADERS, actor="someone-else"):
        return self.client.post("/api/claims/CLM-AUTH0001/override", headers=headers, json={
            "actor": actor, "action": "set_decision", "target": "claim",
            "value": "approved", "reason": "documents agree"})

    def test_sign_in_sets_a_hardened_cookie(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"username": "meera", "display_name": "Meera Tester",
                                           "city": "Pune", "signed_in": True})
        cookie = response.headers["set-cookie"]
        for part in ("claimpilot_session=", "HttpOnly", "Secure", "SameSite=lax",
                     "Max-Age=43200", "Path=/"):
            self.assertIn(part.lower(), cookie.lower())
        self.assertNotIn(self.password, cookie)
        self.assertEqual(self.client.get("/api/claims").status_code, 200)

    def test_wrong_password_and_unknown_user(self):
        for username, password in (("meera", "wrong"), ("nobody", self.password)):
            response = self.login(username, password)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"signed_in": False,
                                               "detail": "User ID or password is incorrect."})
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.get("/api/claims").status_code, 401)

    def test_five_failures_lock_the_address_for_ten_minutes(self):
        now = time.time()
        with patch.object(auth.time, "time", return_value=now):
            for attempt in range(4):
                self.assertNotIn("locked", self.login(password=f"wrong-{attempt}").json())
            locked = self.login(password="wrong-4").json()
            self.assertTrue(locked["locked"])
            self.assertEqual(locked["detail"], "Too many failed sign-ins. Try again in 10 minutes.")
            # The right password is refused while locked.
            self.assertTrue(self.login().json()["locked"])
        with patch.object(auth.time, "time", return_value=now + 601):
            self.assertTrue(self.login().json()["signed_in"])

    def test_failures_older_than_ten_minutes_do_not_count(self):
        now = time.time()
        for offset in range(4):
            with patch.object(auth.time, "time", return_value=now + offset):
                self.login(password="wrong")
        with patch.object(auth.time, "time", return_value=now + 700):
            self.assertNotIn("locked", self.login(password="wrong").json())

    def test_sign_in_and_changes_need_the_custom_header(self):
        self.assertEqual(self.login(headers={}).status_code, 403)
        self.login()
        self.assertEqual(self.approve(headers={}).status_code, 403)
        self.assertEqual(self.client.post("/api/demo/reset").status_code, 403)
        # Reads need no header; the token path needs none either.
        self.assertEqual(self.client.get("/api/claims/CLM-AUTH0001").status_code, 200)

    def test_the_signed_in_user_is_the_actor(self):
        self.login()
        response = self.approve(actor="spoofed-name")
        self.assertEqual(response.status_code, 200)
        row = [row for row in db.audit_log_for_claim("CLM-AUTH0001")
               if row["action"] == "set_decision"][-1]
        self.assertEqual(row["actor"], "meera")
        final = response.json()["final_decision"]
        self.assertEqual((final["actor"], final["actor_name"]), ("meera", "Meera Tester"))
        events = db.claim_events_for("CLM-AUTH0001")
        self.assertEqual(events[-1]["actor"], "meera")

    def test_the_admin_token_still_works_for_scripts(self):
        response = self.approve(headers={"X-Admin-Token": TOKEN}, actor="seed-script")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["final_decision"]["actor"], "seed-script")

    def test_the_feed_shows_the_final_decision_at_once(self):
        self.login()
        self.assertIsNone(self.client.get("/api/claims").json()[0]["final_decision"])
        self.approve()
        card = self.client.get("/api/claims").json()[0]
        self.assertEqual(card["final_decision"]["value"], "approved")
        self.assertEqual(card["final_decision"]["actor_name"], "Meera Tester")

    def test_profile_counts_my_decisions(self):
        self.login()
        self.assertEqual(self.client.get("/api/session").json()["decided"], 0)
        self.approve()
        me = self.client.get("/api/me").json()
        self.assertEqual((me["display_name"], me["city"], me["decided"]),
                         ("Meera Tester", "Pune", 1))
        self.assertNotIn("password_hash", me)

    def test_logout_clears_and_revokes_the_cookie(self):
        self.login()
        old = self.client.cookies.get(auth.COOKIE_NAME)
        response = self.client.post("/api/logout", headers=HEADERS)
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/claims").status_code, 401)
        # The old cookie, replayed, is refused by this process.
        replay = TestClient(main.app, base_url="https://testserver")
        replay.cookies.set(auth.COOKIE_NAME, old)
        self.assertEqual(replay.get("/api/claims").status_code, 401)
        self.assertEqual(self.client.get("/api/session").json(), {"signed_in": False})

    def test_a_tampered_or_expired_cookie_is_refused(self):
        value = auth.make_session("meera")
        body, signature = value.rsplit(".", 1)
        self.assertIsNone(auth.session_user(body + "." + signature[::-1]))
        self.assertIsNone(auth.session_user(value, now=time.time() + auth.SESSION_SECONDS + 1))
        self.assertEqual(auth.session_user(value)["username"], "meera")

    def test_no_credentialed_cross_origin_requests(self):
        response = self.client.options("/api/claims", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with"})
        self.assertNotEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_session_status_is_never_a_401(self):
        self.assertEqual(self.client.get("/api/session").status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 401)


class RequestInfoJourneyTests(journey.JourneyBase):
    """The assessor asks for the licence; the customer sends the RC first,
    then the licence, which is attached and marks the card."""

    def setUp(self):
        super().setUp()
        main.forget_feed_state()

    def tearDown(self):
        main.forget_feed_state()
        super().tearDown()

    async def request(self, claim_id, item="driving_licence", comment="Front side, please"):
        return await main.request_info(claim_id, main.InfoRequest(
            item=item, comment=comment, actor="meera"))

    async def test_wrong_document_then_the_right_one(self):
        claim_id = await self.submitted_claim()  # photos + RC; no licence
        await self.say("END")
        before = len(self.sent)
        response = await self.request(claim_id)
        self.assertEqual(response["final_decision"]["value"], "more_info")
        self.assertEqual(response["info_request"]["item"], "driving_licence")
        self.assertFalse(response["info_request"]["received"])
        self.assertEqual(self.sent[before], (
            f"*More information needed for {claim_id}*\n"
            "Your assessor asked for the driving licence.\n"
            "Note from your assessor: Front side, please\n"
            "Send it here as a photo or PDF.\n"
            f"Tracking ID: {claim_id} · Assessor: Meera Kulkarni"))
        events = [event["event"] for event in db.claim_events_for(claim_id)]
        self.assertEqual(events[-1], "more_info_requested")

        wrong = await self.image(journey.jpeg("rc", 21))
        self.assertEqual(wrong, "That looks like a registration certificate, but your assessor "
                                "asked for the driving licence. Please send the driving licence.")
        self.assertEqual(db.list_session_media(journey.WA_ID), [])
        self.assertEqual(db.get_claim(claim_id)["document_check"]["missing"],
                         ["driving_licence", "claim_form"])

        right = await self.image(journey.jpeg("dl", 21))
        self.assertEqual(right, f"Thank you — your driving licence has been added to "
                                f"*{claim_id}*. Your assessor will review it and reply here.")
        payload = db.get_claim(claim_id)
        # Extraction and rules re-ran: the licence is no longer missing.
        self.assertEqual(payload["document_check"]["missing"], ["claim_form"])
        events = db.claim_events_for(claim_id)
        self.assertEqual([event["event"] for event in events[-2:]],
                         ["documents_added", "documents_received"])
        self.assertEqual(events[-1]["detail"], "driving licence")
        # The claim keeps the licence image, so the console can show it.
        added = [item for item in payload["classifications"]
                 if item["filename"].startswith("driving_licence-added")]
        self.assertTrue(added and added[0]["url"])
        self.assertIsNotNone(db.media_get(added[0]["url"].rsplit("/", 1)[-1]))
        card = next(item for item in main.claims() if item["claim_id"] == claim_id)
        self.assertTrue(card["info_request"]["received"])
        self.assertEqual(len(db.list_claims()), 1)

    async def test_a_screenshot_or_selfie_is_not_the_requested_item(self):
        claim_id = await self.submitted_claim()
        await self.say("END")
        await self.request(claim_id, item="damage_photos", comment="")
        self.assertEqual(await self.image(journey.jpeg("shot")),
                         whatsapp.SCREENSHOT_REPLY + " Your assessor asked for clearer photos "
                                                     "of the damage.")
        self.assertEqual(await self.image(journey.jpeg("selfie")),
                         "That does not look like clearer photos of the damage. Please send "
                         "clearer photos of the damage.")
        reply = await self.image(journey.jpeg("car", 22))
        self.assertTrue(reply.startswith("Thank you — your photo has been added"), reply)
        added = db.get_claim(claim_id)["added_photos"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["requested"], "Clearer damage photos")
        media_id = added[0]["url"].rsplit("/", 1)[-1]
        self.assertIsNotNone(db.media_get(media_id))  # the claim owns the blob

    async def test_outside_the_window_it_is_sent_when_the_customer_writes(self):
        claim_id = await self.submitted_claim()
        await self.say("END")
        with patch.object(whatsapp, "last_inbound_at", return_value=time.time() - 90000):
            before = len(self.sent)
            response = await self.request(claim_id, item="claim_form", comment="")
        self.assertEqual(len(self.sent), before)  # not sent outside 24 h
        self.assertFalse(response["info_request"]["delivered"])
        actions = [row["action"] for row in db.audit_log_for_claim(claim_id)]
        self.assertEqual(actions[-1], "notification_not_sent")
        reply = await self.say("hi")
        self.assertTrue(reply.startswith(f"*More information needed for {claim_id}*"), reply)
        self.assertTrue(main.feed_state(claim_id, fresh=True)["info_request"]["delivered"])
        self.assertEqual(await self.say("hi"), whatsapp.with_footer(whatsapp.WELCOME))

    async def test_tracking_shows_what_was_requested(self):
        claim_id = await self.submitted_claim()
        await self.say("END")
        await self.request(claim_id)
        await self.say("2")
        self.assertIn("Requested: the driving licence", await self.say(claim_id))


class ComparisonTests(unittest.TestCase):
    FIELDS = [
        {"name": "insured_name_claim_form", "value": "R. K. Sharma", "status": "found"},
        {"name": "owner_name_rc", "value": "SURENDRA UTTAM ARAKH", "status": "found"},
        {"name": "registration_number_rc", "value": "MH12AB1234", "status": "found"},
        {"name": "registration_number_claim_form", "value": "MH12AB1234", "status": "found"},
    ]

    def test_a_flag_carries_its_comparison(self):
        flag = next(flag for flag in rules.run_rules(self.FIELDS, [])
                    if flag["rule"] == "insured_owner_mismatch")
        self.assertEqual(flag["comparison"], {
            "rows": [{"source": "Claim form name", "value": "R. K. Sharma"},
                     {"source": "RC owner", "value": "SURENDRA UTTAM ARAKH"}],
            "verdict": "MISMATCH"})

    def test_passed_checks_are_match_rows(self):
        checks = rules.passed_checks(self.FIELDS)
        self.assertEqual([check["rule"] for check in checks], ["registration_match"])
        self.assertEqual(checks[0]["comparison"]["verdict"], "MATCH")

    def test_old_claims_get_comparisons_and_lose_ela_and_stripped_exif(self):
        payload = {
            "claim_id": "CLM-OLD00001", "status": "analysed", "fields": self.FIELDS,
            "flags": [{"rule": "insured_owner_mismatch", "severity": "HIGH", "message": "x",
                       "evidence": ["insured_name_claim_form", "owner_name_rc"]}],
            "forensics": [{"check": "ela", "verdict": "informational"},
                          {"check": "exif", "verdict": "unverifiable"},
                          {"check": "exif", "verdict": "clean", "detail": "tags"}],
            "undetermined": [{"item": "Photo editing"}, {"item": "Photo capture time and location"},
                             {"item": "Driver identity"}],
        }
        with patch.object(main, "audit_log_for_claim", return_value=[]), \
                patch.object(main, "claim_events_for", return_value=[]):
            response = main.build_claim_response(payload)
        self.assertEqual(response["flags"][0]["comparison"]["verdict"], "MISMATCH")
        self.assertEqual(response["forensics"], [{"check": "exif", "verdict": "clean",
                                                  "detail": "tags"}])
        self.assertEqual([item["item"] for item in response["undetermined"]], ["Driver identity"])
        self.assertEqual(response["checks"][0]["rule"], "registration_match")


if __name__ == "__main__":
    unittest.main()
