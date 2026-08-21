import os
import time
import traceback
import uuid
from collections import deque

import httpx
from dotenv import load_dotenv

load_dotenv()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_WABA_ID = os.getenv("WHATSAPP_WABA_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "")

BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

HTTP_TIMEOUT = 30.0
MAX_PROCESSED = 500

SESSIONS = {}
PROCESSED = set()
_PROCESSED_ORDER = deque()


def _auth_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


def _safe_url(url):
    """Media CDN links carry a signed query string - strip it before logging."""
    return str(url or "").split("?")[0]


def verify(mode, token, challenge):
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return challenge
    return None


def get_session(wa_id):
    session = SESSIONS.get(wa_id)
    if session is None:
        session = {"photos": [], "docs": [], "updated": time.time()}
        SESSIONS[wa_id] = session
    return session


def remember_message(message_id):
    """Returns True the first time a message id is seen, False on redelivery."""
    if not message_id or message_id in PROCESSED:
        return False
    PROCESSED.add(message_id)
    _PROCESSED_ORDER.append(message_id)
    while len(_PROCESSED_ORDER) > MAX_PROCESSED:
        PROCESSED.discard(_PROCESSED_ORDER.popleft())
    return True


async def download_media(media_obj):
    """Returns raw bytes, or None on any failure. Never raises."""
    try:
        if not isinstance(media_obj, dict):
            print("[whatsapp] download_media: media object is not a dict", flush=True)
            return None

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            url = media_obj.get("url")

            if not url:
                media_id = media_obj.get("id")
                if not media_id:
                    print("[whatsapp] download_media: no url and no id", flush=True)
                    return None
                lookup = f"{BASE}/{media_id}"
                print(f"[whatsapp] GET {lookup}", flush=True)
                meta = await client.get(lookup, headers=_auth_headers())
                print(f"[whatsapp] media lookup status {meta.status_code}", flush=True)
                if meta.status_code != 200:
                    print(f"[whatsapp] media lookup failed: {meta.text[:300]}", flush=True)
                    return None
                try:
                    url = (meta.json() or {}).get("url")
                except Exception:
                    url = None
                if not url:
                    print("[whatsapp] media lookup returned no url", flush=True)
                    return None

            print(f"[whatsapp] GET {_safe_url(url)} (media bytes)", flush=True)
            resp = await client.get(url, headers=_auth_headers())
            print(f"[whatsapp] media download status {resp.status_code}", flush=True)
            if resp.status_code != 200:
                print(f"[whatsapp] media download failed: {resp.text[:300]}", flush=True)
                return None
            return resp.content
    except Exception:
        print("[whatsapp] download_media failed:", flush=True)
        traceback.print_exc()
        return None


async def send_text(to, body):
    url = f"{BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                url, headers={**_auth_headers(), "Content-Type": "application/json"}, json=payload
            )
        print(f"[whatsapp] send_text -> {resp.status_code}: {resp.text[:300]}", flush=True)
    except Exception:
        print("[whatsapp] send_text failed:", flush=True)
        traceback.print_exc()


async def subscribe_app():
    url = f"{BASE}/{WHATSAPP_WABA_ID}/subscribed_apps"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=_auth_headers())
        print(f"[whatsapp] subscribe_app -> {resp.status_code}: {resp.text[:300]}", flush=True)
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "body": resp.text[:500]}
    except Exception as e:
        print("[whatsapp] subscribe_app failed:", flush=True)
        traceback.print_exc()
        return {"error": str(e)}


def _pretty(name):
    text = str(name or "").replace("_", " ").strip()
    return text.capitalize() if text else "Unknown part"


def _rupees(value):
    try:
        return f"Rs {int(value):,}"
    except Exception:
        return "Rs 0"


def _stage_seconds(payload, stage):
    for entry in payload.get("timings") or []:
        if entry.get("stage") == stage:
            return entry.get("seconds")
    return None


def format_reply(payload):
    status = payload.get("status")
    stopped_at = payload.get("stopped_at")
    claim_id = payload.get("claim_id", "unknown")

    if status == "rejected" and stopped_at == "duplicate":
        match = ""
        for entry in payload.get("forensics") or []:
            if entry.get("check") == "duplicate" and entry.get("verdict") == "suspicious":
                match = entry.get("detail") or ""
                break
        seconds = _stage_seconds(payload, "stage0_duplicate")
        lines = [
            "*Claim rejected - duplicate photo*",
            "",
            "This photo has already been submitted with an earlier claim.",
        ]
        if match:
            lines.append(f"Match: {match}")
        lines += [
            f"Reference: *{claim_id}*",
            "",
            "Your claim has been sent for *manual review*.",
        ]
        if seconds is not None:
            lines.append(f"Detected at stage 0 in {seconds}s - no AI analysis was run.")
        return "\n".join(lines)

    if status == "rejected" and stopped_at == "not_a_vehicle":
        description = payload.get("vehicle_description") or ""
        lines = ["*Cannot process this image*", ""]
        if description:
            lines.append(f"What I saw: {description}")
        else:
            lines.append("This photo does not appear to show a motor vehicle.")
        lines += ["", "Please send a clear photo of the *damaged vehicle* to start your claim."]
        return "\n".join(lines)

    if status == "error" and stopped_at == "ai_unavailable":
        return "\n".join([
            "*Analysis unavailable*",
            "",
            "We could not complete the automated assessment right now.",
            f"Your claim *{claim_id}* has been escalated to a human assessor.",
            "",
            "You do not need to resend anything.",
        ])

    if status == "analysed":
        estimate = payload.get("estimate") or {}
        line_items = estimate.get("line_items") or []
        lines = [f"*Claim {claim_id}*"]
        description = payload.get("vehicle_description") or ""
        if description:
            lines.append(description)
        lines.append("")

        if line_items:
            lines.append("*Damage found:*")
            for item in line_items:
                lines.append(
                    f"- *{_pretty(item.get('part'))}* - {str(item.get('damage_type') or 'damage').replace('_', ' ')}"
                    f" ({item.get('severity')}): {_rupees(item.get('low'))} - {_rupees(item.get('high'))}"
                )
        else:
            lines.append("*No priced damage was identified.*")

        unpriced = estimate.get("unpriced") or []
        if unpriced:
            names = ", ".join(_pretty(u.get("part")) for u in unpriced)
            lines.append(f"- {names}: needs manual pricing")

        lines += [
            "",
            f"*Total estimate:* {_rupees(estimate.get('total_low'))} - {_rupees(estimate.get('total_high'))}",
        ]

        flags = payload.get("flags") or []
        if any(str(f.get("severity", "")).upper() == "HIGH" for f in flags):
            lines += ["", "*Flags:*"]
            for flag in flags:
                lines.append(f"- {flag.get('message')}")

        if estimate.get("requires_survey"):
            lines += ["", f"*Survey required:* {estimate.get('survey_reason')}"]

        return "\n".join(lines)

    return f"*Claim {claim_id}*\nStatus: {status}"


async def process_session(wa_id):
    try:
        await send_text(wa_id, "Analysing your claim. This takes about 30 seconds.")

        claim_id = "CLM-" + uuid.uuid4().hex[:8].upper()
        session = SESSIONS.get(wa_id) or {}
        photo_paths = list(session.get("photos") or [])
        doc_paths = list(session.get("docs") or [])

        # Registered here so the duplicate gate can write phash back against these rows,
        # exactly as the /api/analyze upload path does.
        from db import save_file

        for path in doc_paths:
            save_file(claim_id, os.path.basename(path), "document", path)
        for path in photo_paths:
            save_file(claim_id, os.path.basename(path), "photo", path)

        from main import analyse_claim  # deferred: main imports this module at startup

        payload = await analyse_claim(claim_id, doc_paths, photo_paths)
        await send_text(wa_id, format_reply(payload))
    except Exception:
        print("[whatsapp] process_session failed:", flush=True)
        traceback.print_exc()
        await send_text(wa_id, "Something went wrong processing your claim. Please try again.")
    finally:
        SESSIONS.pop(wa_id, None)
