import asyncio
import copy
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import db
import main
import whatsapp
from gemini import assign_damage_ids
from rules import build_estimate


def damage(part, severity="moderate", confidence=0.9, damage_id=None):
    item = {
        "part": part,
        "damage_type": "dent",
        "severity": severity,
        "confidence": confidence,
        "source_photo": "car.jpg",
        "reasoning": "Visible damage.",
    }
    if damage_id:
        item["damage_id"] = damage_id
    return item


def payload_for(claim_id, items, source="web", from_number=None, vehicles=True):
    original_damage = copy.deepcopy(items)
    estimate = build_estimate(original_damage)
    payload = {
        "claim_id": claim_id,
        "created_at": "2026-10-18T10:00:00",
        "source": source,
        "from_number": from_number,
        "status": "analysed",
        "stopped_at": None,
        "decision": "needs_review",
        "decision_reasons": ["Manual review requested"],
        "damage": original_damage,
        "estimate": estimate,
        "vehicles": [],
        "claimable_count": 1,
        "fields": [],
        "forensics": [],
        "flags": [],
        "timings": [],
        "undetermined": [],
    }
    if vehicles:
        vehicle_damage = copy.deepcopy(original_damage)
        payload["vehicles"] = [{
            "group": 0,
            "label": "White Kia Sedan",
            "photos": ["car.jpg"],
            "photo_urls": [],
            "excluded_photos": [],
            "excluded_photo_urls": [],
            "damage": vehicle_damage,
            "estimate": build_estimate(vehicle_damage),
            "status": "claimable",
        }]
    return payload


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = os.path.join(self.folder.name, "overrides.db")
        db.init_db()
        whatsapp.LAST_INBOUND.clear()

    def tearDown(self):
        whatsapp.LAST_INBOUND.clear()
        db.DB_PATH = self.original_db
        self.folder.cleanup()

    def override(self, claim_id, action, target, value=None, reason="Reviewed"):
        request = main.OverrideRequest(
            actor="Deva",
            action=action,
            target=target,
            value=value,
            reason=reason,
        )
        return asyncio.run(main.override_claim(claim_id, request))

    def test_stable_damage_ids_suffix_collisions(self):
        items = [
            damage("front_bumper"),
            damage("front_bumper", severity="severe"),
        ]
        assign_damage_ids(
            [{"source_photo": "car.jpg", "vehicle_group": 0}],
            items,
        )
        self.assertEqual(
            [item["damage_id"] for item in items],
            ["0-front_bumper", "0-front_bumper-2"],
        )

    def test_confirm_unconfirmed_changes_only_effective_estimate(self):
        claim_id = "CLM-CONFIRM"
        item = damage("front_bumper", confidence=0.4, damage_id="0-front_bumper")
        db.save_claim(claim_id, payload_for(claim_id, [item]))

        response = self.override(
            claim_id, "confirm_damage", "0-front_bumper"
        )

        self.assertEqual(response["estimate"]["total_high"], 0)
        self.assertEqual(response["effective_estimate"]["total_low"], 4000)
        self.assertEqual(response["effective_estimate"]["total_high"], 8000)
        self.assertTrue(response["effective_damage"][0]["confirmed"])
        stored = db.get_claim(claim_id)
        self.assertEqual(stored["estimate"]["total_high"], 0)
        self.assertFalse(stored["damage"][0]["confirmed"])

    def test_reject_damage_drops_exact_line_range(self):
        claim_id = "CLM-REJECT-DAMAGE"
        items = [
            damage("front_bumper", damage_id="0-front_bumper"),
            damage("bonnet", severity="severe", damage_id="0-bonnet"),
        ]
        db.save_claim(claim_id, payload_for(claim_id, items))
        before = main.claim_detail(claim_id)
        rejected_line = next(
            line for line in before["effective_estimate"]["line_items"]
            if line["damage_id"] == "0-front_bumper"
        )

        response = self.override(
            claim_id, "reject_damage", "0-front_bumper"
        )

        self.assertEqual(
            response["effective_estimate"]["total_low"],
            before["effective_estimate"]["total_low"] - rejected_line["low"],
        )
        self.assertEqual(
            response["effective_estimate"]["total_high"],
            before["effective_estimate"]["total_high"] - rejected_line["high"],
        )

    def test_set_severity_reprices_effective_damage(self):
        claim_id = "CLM-SEVERITY"
        item = damage("front_bumper", damage_id="0-front_bumper")
        db.save_claim(claim_id, payload_for(claim_id, [item]))
        before = main.claim_detail(claim_id)["effective_estimate"]

        response = self.override(
            claim_id,
            "set_severity",
            "0-front_bumper",
            value="severe",
        )

        self.assertGreater(
            response["effective_estimate"]["total_low"], before["total_low"]
        )
        self.assertGreater(
            response["effective_estimate"]["total_high"], before["total_high"]
        )
        self.assertEqual(response["effective_damage"][0]["severity"], "severe")
        self.assertEqual(db.get_claim(claim_id)["damage"][0]["severity"], "moderate")

    def test_empty_reason_is_400_and_writes_nothing(self):
        claim_id = "CLM-EMPTY-REASON"
        item = damage("front_bumper", damage_id="0-front_bumper")
        db.save_claim(claim_id, payload_for(claim_id, [item]))

        with self.assertRaises(HTTPException) as caught:
            self.override(
                claim_id,
                "reject_damage",
                "0-front_bumper",
                reason="   ",
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(db.audit_log_for_claim(claim_id), [])

    def test_whatsapp_approval_inside_window_sends_message(self):
        claim_id = "CLM-WHATSAPP"
        wa_id = "919876546464"
        item = damage("front_bumper", damage_id="0-front_bumper")
        db.save_claim(
            claim_id,
            payload_for(
                claim_id, [item], source="whatsapp", from_number=wa_id
            ),
        )
        whatsapp.record_inbound(wa_id)

        with patch.object(
            main, "send_text", new=AsyncMock(return_value=True)
        ) as mocked_send:
            response = self.override(
                claim_id,
                "set_decision",
                "claim",
                value="approved",
                reason="Documents and damage reviewed",
            )

        message = (
            "*Claim CLM-WHATSAPP approved* — Rs 4,000 – Rs 8,000. "
            "An assessor has reviewed your claim."
        )
        mocked_send.assert_awaited_once_with(wa_id, message)
        self.assertEqual(response["final_decision"]["value"], "approved")
        self.assertEqual(
            response["overrides"][-1]["action"], "notification_sent"
        )

    def test_whatsapp_approval_outside_window_is_audited_not_sent(self):
        claim_id = "CLM-WHATSAPP-OLD"
        wa_id = "919876540000"
        item = damage("front_bumper", damage_id="0-front_bumper")
        db.save_claim(
            claim_id,
            payload_for(
                claim_id, [item], source="whatsapp", from_number=wa_id
            ),
        )
        whatsapp.record_inbound(wa_id, time.time() - (25 * 60 * 60))

        with patch.object(
            main, "send_text", new=AsyncMock(return_value=True)
        ) as mocked_send:
            response = self.override(
                claim_id,
                "set_decision",
                "claim",
                value="approved",
            )

        mocked_send.assert_not_awaited()
        notification = response["overrides"][-1]
        self.assertEqual(notification["action"], "notification_not_sent")
        self.assertEqual(
            notification["reason"],
            "notification not sent: outside 24h service window",
        )

    def test_legacy_claim_uses_response_only_id_and_can_be_overridden(self):
        claim_id = "CLM-LEGACY"
        item = damage("front_bumper")
        payload = payload_for(claim_id, [item], vehicles=False)
        db.save_claim(claim_id, payload)

        read = main.claim_detail(claim_id)
        self.assertEqual(read["damage"][0]["damage_id"], "legacy-0")
        self.assertNotIn("damage_id", db.get_claim(claim_id)["damage"][0])

        response = self.override(
            claim_id, "reject_damage", "legacy-0"
        )

        self.assertTrue(response["effective_damage"][0]["rejected"])
        self.assertEqual(response["effective_estimate"]["total_high"], 0)
        self.assertNotIn("damage_id", db.get_claim(claim_id)["damage"][0])


if __name__ == "__main__":
    unittest.main()
