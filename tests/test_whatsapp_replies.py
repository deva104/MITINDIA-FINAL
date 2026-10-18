try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import time
import unittest
from unittest.mock import AsyncMock, patch

import main
import whatsapp


def estimate(low, high, part="rear_bumper", damage_type="crack", severity="severe"):
    return {
        "line_items": [{
            "part": part,
            "damage_type": damage_type,
            "severity": severity,
            "low": low,
            "high": high,
        }],
        "unpriced": [],
        "total_low": low,
        "total_high": high,
        "requires_survey": False,
        "survey_reason": "",
    }


def group(number, label, status, low=0, high=0, photos=None,
          part="rear_bumper", damage_type="crack", severity="severe"):
    damage = [] if status != "claimable" else [{
        "source_photo": (photos or [f"group-{number}.jpg"])[0],
        "part": part,
        "damage_type": damage_type,
        "severity": severity,
    }]
    group_estimate = (
        estimate(low, high, part, damage_type, severity)
        if status == "claimable"
        else estimate(0, 0)
    )
    if status != "claimable":
        group_estimate["line_items"] = []
    return {
        "group": number,
        "label": label,
        "photos": photos or [f"group-{number}.jpg"],
        "excluded_photos": (photos or [f"group-{number}.jpg"])
        if status == "all_duplicate"
        else [],
        "damage": damage,
        "estimate": group_estimate,
        "status": status,
    }


def payload(claim_id, vehicles, excluded=None, photos=None):
    claimable = [vehicle for vehicle in vehicles if vehicle["status"] == "claimable"]
    primary = claimable[0] if claimable else None
    return {
        "claim_id": claim_id,
        "status": "analysed",
        "stopped_at": None,
        "vehicles": vehicles,
        "claimable_count": len(claimable),
        "excluded_photos": excluded or [],
        "photos": photos or [],
        "damage": primary["damage"] if primary else [],
        "estimate": primary["estimate"] if primary else estimate(0, 0),
        "flags": [],
    }


class WhatsAppReplyTests(unittest.TestCase):
    def test_one_vehicle_reply(self):
        vehicle = group(
            0, "White Kia Sedan", "claimable", 15000, 27000,
            photos=["rear.jpg", "boot.jpg"],
        )
        vehicle["estimate"]["line_items"] = [
            {
                "part": "rear_bumper",
                "damage_type": "crack",
                "severity": "severe",
                "low": 8000,
                "high": 15000,
            },
            {
                "part": "boot",
                "damage_type": "dent",
                "severity": "moderate",
                "low": 7000,
                "high": 12000,
            },
        ]

        self.assertEqual(
            whatsapp.format_reply(payload("CLM-XXXX", [vehicle], photos=[{}, {}])),
            "\n".join([
                "*Claim CLM-XXXX · White Kia Sedan*",
                "",
                "Damage found:",
                "- Rear bumper — crack (severe): Rs 8,000 – Rs 15,000",
                "- Boot — dent (moderate): Rs 7,000 – Rs 12,000",
                "",
                "*Estimate: Rs 15,000 – Rs 27,000*",
            ]),
        )

    def test_two_vehicle_selection_prompt(self):
        vehicles = [
            group(0, "White Kia Sedan", "claimable", 4000, 8000,
                  damage_type="detached"),
            group(1, "Black SUV", "claimable", 8000, 15000),
        ]
        self.assertEqual(
            whatsapp.format_reply(payload("CLM-TWO", vehicles)),
            "\n".join([
                "*Two different vehicles in one claim*",
                "",
                "Your photos show two vehicles. A claim covers one vehicle, so please choose:",
                "",
                "*1 · White Kia Sedan* — rear bumper detached, Rs 4,000 – Rs 8,000",
                "*2 · Black SUV* — rear bumper crack, Rs 8,000 – Rs 15,000",
                "",
                "Reply *1* or *2* to continue with that vehicle.",
            ]),
        )

    def test_claimable_plus_duplicate_confirmation(self):
        vehicles = [
            group(0, "White Kia Sedan", "claimable", 4000, 8000,
                  damage_type="detached"),
            group(1, "Black SUV", "all_duplicate"),
        ]
        self.assertEqual(
            whatsapp.format_reply(payload("CLM-DUP", vehicles, excluded=["black.jpg"])),
            "\n".join([
                "*One vehicle cannot be claimed*",
                "",
                "*Black SUV* — the photos of this vehicle were already submitted in an earlier claim. It cannot be claimed again.",
                "",
                "*White Kia Sedan* — assessed and claimable, Rs 4,000 – Rs 8,000.",
                "",
                "Reply *YES* to continue with the White Kia Sedan, or send fresh photos.",
            ]),
        )

    def test_claimable_plus_no_damage_note(self):
        vehicles = [
            group(0, "White Kia Sedan", "claimable", 4000, 8000),
            group(1, "Black SUV", "no_damage", photos=["black.jpg"]),
        ]
        reply = whatsapp.format_reply(payload("CLM-NOTE", vehicles))
        self.assertTrue(reply.endswith(
            "Note: your photo of the Black SUV shows no visible damage, "
            "so it has not been included."
        ))

    def test_all_no_damage_reply(self):
        vehicles = [
            group(0, "White Kia Sedan", "no_damage", photos=["white.jpg"]),
            group(1, "Black SUV", "no_damage", photos=["black.jpg"]),
        ]
        self.assertEqual(
            whatsapp.format_reply(payload("CLM-CLEAN", vehicles)),
            "\n".join([
                "*No visible damage found*",
                "",
                "We examined 2 photos and could not identify damage on either vehicle.",
                "Send clearer photos showing the damaged area, or reply HELP to speak to an assessor.",
            ]),
        )

    def test_excluded_photo_notice(self):
        vehicle = group(
            0, "White Kia Sedan", "claimable", 4000, 8000,
            photos=["clean-1.jpg", "clean-2.jpg"],
        )
        reply = whatsapp.format_reply(
            payload(
                "CLM-EXCLUDED",
                [vehicle],
                excluded=["duplicate.jpg"],
                photos=[{"source_photo": "clean-1.jpg"}, {"source_photo": "clean-2.jpg"}],
            )
        )
        self.assertTrue(reply.startswith(
            "1 of your 3 photos was already submitted in an earlier claim and has "
            "been excluded. The assessment below uses the remaining 2 photos."
        ))
        self.assertTrue(reply.endswith("Flagged for manual review."))

    def test_unconfirmed_damage_has_no_zero_estimate(self):
        vehicle = group(
            0, "White Kia Sedan", "claimable", 0, 0,
            photos=["uncertain.jpg"],
        )
        vehicle["estimate"]["line_items"] = []
        vehicle["estimate"]["unconfirmed"] = [{
            "part": "front_bumper",
            "damage_type": "crack",
            "severity": "severe",
            "confidence": 0.4,
            "low": 8000,
            "high": 15000,
        }]
        result = payload("CLM-UNCERTAIN", [vehicle])
        result["decision"] = "needs_review"
        result["decision_reasons"] = [
            "Low-confidence damage finding: front bumper"
        ]

        reply = whatsapp.format_reply(result)

        self.assertNotIn("*Estimate: Rs 0 – Rs 0*", reply)
        self.assertIn("*No confirmed estimate yet*", reply)
        self.assertIn(
            "Indicative range Rs 8,000 – Rs 15,000, pending assessor review.",
            reply,
        )
        self.assertIn(
            "No confirmed estimate yet; indicative range "
            "Rs 8,000 – Rs 15,000, pending assessor review",
            whatsapp._vehicle_summary(vehicle),
        )


class WhatsAppPendingChoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        whatsapp.SESSIONS.clear()

    async def test_numeric_choice_sends_vehicle_and_clears_session(self):
        vehicles = [
            group(0, "White Kia Sedan", "claimable", 4000, 8000),
            group(1, "Black SUV", "claimable", 8000, 15000),
        ]
        whatsapp.SESSIONS["user"] = {
            "photos": [],
            "docs": [],
            "pending_choice": {"claim_id": "CLM-TWO", "vehicles": vehicles},
            "updated": time.time(),
        }
        sent = AsyncMock()
        with patch.object(whatsapp, "send_text", sent):
            handled = await whatsapp.handle_pending_choice("user", "2")

        self.assertTrue(handled)
        self.assertNotIn("user", whatsapp.SESSIONS)
        self.assertIn("*Claim CLM-TWO · Black SUV*", sent.await_args.args[1])

    async def test_yes_choice_sends_only_claimable_vehicle(self):
        vehicle = group(0, "White Kia Sedan", "claimable", 4000, 8000)
        whatsapp.SESSIONS["user"] = {
            "photos": [],
            "docs": [],
            "pending_choice": {"claim_id": "CLM-DUP", "vehicles": [vehicle]},
            "updated": time.time(),
        }
        sent = AsyncMock()
        with patch.object(whatsapp, "send_text", sent):
            handled = await whatsapp.handle_pending_choice("user", "Y")

        self.assertTrue(handled)
        self.assertNotIn("user", whatsapp.SESSIONS)
        self.assertIn("*Claim CLM-DUP · White Kia Sedan*", sent.await_args.args[1])

    async def test_invalid_choice_resends_prompt(self):
        vehicles = [
            group(0, "White Kia Sedan", "claimable", 4000, 8000),
            group(1, "Black SUV", "claimable", 8000, 15000),
        ]
        prompt = whatsapp._selection_reply("CLM-TWO", vehicles)
        whatsapp.SESSIONS["user"] = {
            "photos": [],
            "docs": [],
            "pending_choice": {"claim_id": "CLM-TWO", "vehicles": vehicles},
            "pending_prompt": prompt,
            "updated": time.time(),
        }
        sent = AsyncMock()
        with patch.object(whatsapp, "send_text", sent):
            handled = await whatsapp.handle_pending_choice("user", "later")

        self.assertTrue(handled)
        self.assertIn("user", whatsapp.SESSIONS)
        sent.assert_awaited_once_with("user", prompt)

    async def test_process_session_keeps_pending_multi_vehicle_claim(self):
        vehicles = [
            group(0, "White Kia Sedan", "claimable", 4000, 8000),
            group(1, "Black SUV", "claimable", 8000, 15000),
        ]
        result = payload("CLM-TWO", vehicles)
        whatsapp.SESSIONS["user"] = {
            "photos": ["one.jpg", "two.jpg"],
            "docs": [],
            "pending_choice": None,
            "updated": time.time(),
        }
        sent = AsyncMock()
        with (
            patch.object(whatsapp, "send_text", sent),
            patch.object(whatsapp, "classify_session_intake", return_value={
                "classifications": [],
                "documents_received": {
                    "rc": 1,
                    "driving_licence": 1,
                    "claim_form": 1,
                    "policy": 0,
                },
                "vehicle_count": 2,
                "missing": [],
            }),
            patch.object(main, "analyse_claim", AsyncMock(return_value=result)),
            patch("db.save_file"),
        ):
            await whatsapp.process_session("user")

        pending = whatsapp.SESSIONS["user"]["pending_choice"]
        self.assertEqual(pending["claim_id"], "CLM-TWO")
        self.assertEqual(pending["vehicles"], vehicles)
        self.assertEqual(sent.await_count, 2)

    async def test_main_routes_pending_text_before_normal_flow(self):
        pending_handler = AsyncMock(return_value=True)
        process = AsyncMock()
        with (
            patch.object(whatsapp, "handle_pending_choice", pending_handler),
            patch.object(main, "process_session", process),
        ):
            await main.handle_wa_message({
                "from": "user",
                "id": "message-id",
                "type": "text",
                "text": {"body": "2"},
            })

        pending_handler.assert_awaited_once_with("user", "2")
        process.assert_not_awaited()

    def test_stale_pending_choice_resets_session(self):
        whatsapp.SESSIONS["user"] = {
            "photos": ["old.jpg"],
            "docs": [],
            "pending_choice": {"claim_id": "OLD", "vehicles": []},
            "updated": time.time() - whatsapp.PENDING_CHOICE_TIMEOUT - 1,
        }

        session = whatsapp.get_session("user")

        self.assertIsNone(session["pending_choice"])
        self.assertEqual(session["photos"], [])


if __name__ == "__main__":
    unittest.main()
