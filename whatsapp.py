import os
import time
import traceback
import uuid
from collections import deque

import httpx
from dotenv import load_dotenv
from gemini import classify_image, extract_fields

load_dotenv()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_WABA_ID = os.getenv("WHATSAPP_WABA_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "")

BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

HTTP_TIMEOUT = 30.0
MAX_PROCESSED = 500
PENDING_CHOICE_TIMEOUT = 30 * 60
MAX_IMAGES_PER_CLAIM = 8
DOCUMENT_LABELS = {
    "rc": "Registration certificate",
    "driving_licence": "Driving licence",
    "claim_form": "Claim form",
    "policy": "Insurance policy",
}

SESSIONS = {}
LAST_INBOUND = {}
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


def record_inbound(wa_id, timestamp=None):
    if wa_id:
        try:
            received_at = float(timestamp) if timestamp is not None else time.time()
        except (TypeError, ValueError):
            received_at = time.time()
        LAST_INBOUND[str(wa_id)] = received_at


def last_inbound_at(wa_id):
    return LAST_INBOUND.get(str(wa_id)) if wa_id else None


def get_session(wa_id):
    session = SESSIONS.get(wa_id)
    now = time.time()
    if (session is not None
            and (session.get("pending_choice") or session.get("awaiting_docs"))
            and now - session.get("updated", 0) > PENDING_CHOICE_TIMEOUT):
        SESSIONS.pop(wa_id, None)
        session = None
    if session is None:
        session = {
            "photos": [],
            "docs": [],
            "pending_choice": None,
            "awaiting_docs": False,
            "missing_docs": [],
            "limit_notified": False,
            "updated": now,
        }
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
        return resp.is_success
    except Exception:
        print("[whatsapp] send_text failed:", flush=True)
        traceback.print_exc()
        return False


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


def _estimate_range(estimate):
    estimate = estimate or {}
    unconfirmed = estimate.get("unconfirmed") or []
    if not estimate.get("total_high") and unconfirmed:
        low = sum(item.get("low") or 0 for item in unconfirmed)
        high = sum(item.get("high") or 0 for item in unconfirmed)
        return (
            f"No confirmed estimate yet; indicative range "
            f"{_rupees(low)} – {_rupees(high)}, pending assessor review"
        )
    return (
        f"{_rupees(estimate.get('total_low'))} "
        f"– {_rupees(estimate.get('total_high'))}"
    )


def _is_authenticity_signal(value):
    text = str(value or "").lower()
    return any(term in text for term in (
        "ai generation", "ai-generation", "ai_generated",
        "generated image", "artificially generated", "fraud",
    ))


def _undetermined_lines(items):
    if not items:
        return []
    reason_labels = {
        "value present but illegible": "illegible on the document",
        "metadata stripped in transit": "metadata removed in transit",
    }
    lines = ["", "*Could not determine:*"]
    for item in items:
        reason = reason_labels.get(item.get("reason"), item.get("reason"))
        label = item.get("item")
        if _is_authenticity_signal(label) or _is_authenticity_signal(reason):
            label = "Photo authenticity"
            reason = "independent verification unavailable"
        lines.append(f"- {label} — {reason}")
    return lines


def _single_vehicle_reply(claim_id, vehicle, excluded_count=0, total_photos=None,
                          undetermined=None, decision=None, decision_reasons=None):
    estimate = vehicle.get("estimate") or {}
    line_items = estimate.get("line_items") or []
    lines = []

    if excluded_count:
        total = total_photos if total_photos is not None else len(vehicle.get("photos") or [])
        remaining = max(0, total - excluded_count)
        submitted_verb = "was" if excluded_count == 1 else "were"
        excluded_verb = "has" if excluded_count == 1 else "have"
        remaining_word = "photo" if remaining == 1 else "photos"
        lines += [
            (
                f"{excluded_count} of your {total} photos {submitted_verb} already submitted "
                f"in an earlier claim and {excluded_verb} been excluded. The assessment "
                f"below uses the "
                f"remaining {remaining} {remaining_word}."
            ),
            "",
        ]

    lines += [
        f"*Claim {claim_id} · {vehicle.get('label') or 'Vehicle'}*",
        "",
        "Damage found:",
    ]
    for item in line_items:
        lines.append(
            f"- {_pretty(item.get('part'))} — "
            f"{str(item.get('damage_type') or 'damage').replace('_', ' ')} "
            f"({item.get('severity')}): {_rupees(item.get('low'))} "
            f"– {_rupees(item.get('high'))}"
        )

    unpriced = estimate.get("unpriced") or []
    for item in unpriced:
        lines.append(f"- {_pretty(item.get('part'))} — manual pricing required")

    if not line_items and not unpriced and estimate.get("unconfirmed"):
        lines.append("- Possible damage detected — excluded pending manual review")
    elif not line_items and not unpriced:
        lines.append("- Damage detected — manual pricing required")

    unconfirmed = estimate.get("unconfirmed") or []
    if not estimate.get("total_high") and unconfirmed:
        indicative_low = sum(item.get("low") or 0 for item in unconfirmed)
        indicative_high = sum(item.get("high") or 0 for item in unconfirmed)
        lines += [
            "",
            "*No confirmed estimate yet*",
            (
                f"Indicative range {_rupees(indicative_low)} – "
                f"{_rupees(indicative_high)}, pending assessor review."
            ),
        ]
    else:
        lines += ["", f"*Estimate: {_estimate_range(estimate)}*"]
    lines += _undetermined_lines(undetermined)
    if decision == "needs_review":
        reasons = decision_reasons or []
        if any(_is_authenticity_signal(reason) for reason in reasons):
            first_reason = "photo authenticity requires verification"
        else:
            first_reason = reasons[0] if reasons else "automated checks require review"
        lines += ["", f"Flagged for manual review: {first_reason.rstrip('.')}."]
    elif excluded_count:
        lines += ["", "Flagged for manual review."]
    return "\n".join(lines)


def _vehicle_summary(vehicle):
    estimate = vehicle.get("estimate") or {}
    line_items = estimate.get("line_items") or []
    first = line_items[0] if line_items else {}
    part = str(first.get("part") or "damage").replace("_", " ")
    damage_type = str(first.get("damage_type") or "detected").replace("_", " ")
    return f"{part} {damage_type}, {_estimate_range(estimate)}"


def _number_word(number, title=False):
    words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    value = words.get(number, str(number))
    return value.title() if title else value


def _selection_reply(claim_id, vehicles):
    count = len(vehicles)
    lines = [
        f"*{_number_word(count, title=True)} different vehicles in one claim*",
        "",
        (
            f"Your photos show {_number_word(count)} vehicles. A claim covers one vehicle, "
            "so please choose:"
        ),
        "",
    ]
    for index, vehicle in enumerate(vehicles, 1):
        lines.append(
            f"*{index} · {vehicle.get('label') or f'Vehicle {index}'}* "
            f"— {_vehicle_summary(vehicle)}"
        )
    choices = " or ".join(f"*{index}*" for index in range(1, count + 1))
    lines += ["", f"Reply {choices} to continue with that vehicle."]
    return "\n".join(lines)


def _not_vehicle_reply(payload):
    photos = {
        os.path.basename(str(photo.get("source_photo") or "")).lower(): photo
        for photo in (payload.get("photos") or [])
        if isinstance(photo, dict)
    }
    descriptions = []
    for vehicle in payload.get("vehicles") or []:
        if vehicle.get("status") != "not_vehicle":
            continue
        text = next(
            (
                photos.get(os.path.basename(str(name)).lower(), {}).get("description")
                for name in vehicle.get("photos") or []
                if photos.get(os.path.basename(str(name)).lower(), {}).get("description")
            ),
            None,
        )
        if text:
            descriptions.append((vehicle.get("label"), text))

    fallback = payload.get("vehicle_description") or ""
    lines = ["*Cannot process this image*", ""]
    if len(descriptions) == 1:
        lines.append(f"What I saw: {descriptions[0][1]}")
    elif descriptions:
        for label, description in descriptions:
            lines.append(f"What I saw in {label or 'this photo'}: {description}")
    elif fallback:
        lines.append(f"What I saw: {fallback}")
    else:
        lines.append("This photo does not appear to show a motor vehicle.")
    lines += ["", "Please send a clear photo of the *damaged vehicle* to start your claim."]
    return "\n".join(lines)


def _duplicate_reply(payload):
    claim_id = payload.get("claim_id", "unknown")
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


def format_reply(payload):
    status = payload.get("status")
    stopped_at = payload.get("stopped_at")
    claim_id = payload.get("claim_id", "unknown")

    if status == "rejected" and stopped_at == "duplicate":
        return _duplicate_reply(payload)

    if status == "rejected" and stopped_at == "not_a_vehicle":
        return _not_vehicle_reply(payload)

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
        vehicles = [
            vehicle for vehicle in (payload.get("vehicles") or [])
            if isinstance(vehicle, dict)
        ]
        claimable = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "claimable"
        ]
        claimable_count = payload.get("claimable_count")
        if not isinstance(claimable_count, int):
            claimable_count = len(claimable)
        duplicate = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "all_duplicate"
        ]
        no_damage = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "no_damage"
        ]
        not_vehicle = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "not_vehicle"
        ]

        if claimable_count > 1:
            return _selection_reply(claim_id, claimable)

        if claimable_count == 1 and duplicate:
            lines = ["*One vehicle cannot be claimed*", ""]
            for vehicle in duplicate:
                lines.append(
                    f"*{vehicle.get('label') or 'Vehicle'}* — the photos of this vehicle "
                    "were already submitted in an earlier claim. It cannot be claimed again."
                )
            vehicle = claimable[0]
            lines += [
                "",
                (
                    f"*{vehicle.get('label') or 'Vehicle'}* — assessed and claimable, "
                    f"{_estimate_range(vehicle.get('estimate'))}."
                ),
                "",
                (
                    f"Reply *YES* to continue with the "
                    f"{vehicle.get('label') or 'vehicle'}, or send fresh photos."
                ),
            ]
            return "\n".join(lines)

        if claimable_count == 1:
            excluded_count = len(payload.get("excluded_photos") or [])
            total_photos = len(payload.get("photos") or []) + excluded_count
            reply = _single_vehicle_reply(
                claim_id,
                claimable[0],
                excluded_count=excluded_count,
                total_photos=total_photos,
                undetermined=payload.get("undetermined"),
                decision=payload.get("decision"),
                decision_reasons=payload.get("decision_reasons"),
            )
            notes = []
            for vehicle in no_damage:
                count = len(vehicle.get("photos") or [])
                if count == 1:
                    notes.append(
                        f"Note: your photo of the {vehicle.get('label')} shows no visible "
                        "damage, so it has not been included."
                    )
                else:
                    notes.append(
                        f"Note: your photos of the {vehicle.get('label')} show no visible "
                        "damage, so they have not been included."
                    )
            return reply + (("\n\n" + "\n".join(notes)) if notes else "")

        statuses = [vehicle.get("status") for vehicle in vehicles]
        if vehicles and all(value == "no_damage" for value in statuses):
            photo_count = sum(len(vehicle.get("photos") or []) for vehicle in vehicles)
            target = "either vehicle" if len(vehicles) == 2 else "any vehicle"
            return "\n".join([
                "*No visible damage found*",
                "",
                (
                    f"We examined {photo_count} photos and could not identify damage "
                    f"on {target}."
                ),
                (
                    "Send clearer photos showing the damaged area, or reply HELP "
                    "to speak to an assessor."
                ),
            ])

        if vehicles and all(value == "all_duplicate" for value in statuses):
            return _duplicate_reply(payload)

        if vehicles and all(value == "not_vehicle" for value in statuses):
            return _not_vehicle_reply(payload)

        if not_vehicle and not claimable:
            return _not_vehicle_reply(payload)

    return f"*Claim {claim_id}*\nStatus: {status}"


def required_docs():
    configured = os.getenv(
        "REQUIRED_DOCS", "rc,driving_licence,claim_form"
    )
    return [
        value.strip().lower()
        for value in configured.split(",")
        if value.strip().lower() in DOCUMENT_LABELS
    ]


def strict_docs_enabled():
    return os.getenv("STRICT_DOCS", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _infer_document_types(fields):
    found = {
        field.get("name")
        for field in (fields or [])
        if isinstance(field, dict)
        and field.get("status") == "found"
        and field.get("value") not in (None, "")
    }
    inferred = set()
    if found & {
        "owner_name_rc", "registration_number_rc",
        "chassis_number", "engine_number", "vehicle_make_model",
    }:
        inferred.add("rc")
    if found & {"holder_name_dl", "dl_number", "dl_expiry_date"}:
        inferred.add("driving_licence")
    if found & {
        "insured_name_claim_form", "registration_number_claim_form",
        "accident_date", "accident_time", "accident_location",
        "accident_description",
    }:
        inferred.add("claim_form")
    if found & {"policy_number", "policy_start_date", "policy_end_date"}:
        inferred.add("policy")
    return inferred


def classify_session_intake(session):
    classifications = []
    received = {doc_type: 0 for doc_type in DOCUMENT_LABELS}
    vehicle_count = 0
    for path in list(session.get("photos") or [])[:MAX_IMAGES_PER_CLAIM]:
        result = classify_image(path)
        entry = {"path": path, **result}
        classifications.append(entry)
        try:
            confidence = float(result.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        doc_type = result.get("doc_type")
        if confidence < 0.6 or doc_type == "vehicle_photo":
            vehicle_count += 1
        elif doc_type in received:
            received[doc_type] += 1

    document_paths = list(session.get("docs") or [])
    if document_paths:
        for doc_type in _infer_document_types(extract_fields(document_paths)):
            received[doc_type] = max(1, received[doc_type])

    required = required_docs()
    missing = [doc_type for doc_type in required if not received[doc_type]]
    result = {
        "classifications": classifications,
        "documents_received": received,
        "vehicle_count": vehicle_count,
        "missing": missing,
    }
    session["intake"] = result
    session["missing_docs"] = missing
    return result


def checklist_reply(intake):
    vehicle_count = intake.get("vehicle_count", 0)
    if not vehicle_count:
        return "\n".join([
            "*Vehicle photo needed*",
            "",
            "I received your documents, but no vehicle photo was found.",
            "Send at least one clear photo of the damaged vehicle.",
            "SKIP is not available until a vehicle photo is provided.",
        ])

    lines = ["*Almost there — documents needed*", "", "Received:"]
    lines.append(f"✓ Damage photos ({vehicle_count})")
    received = intake.get("documents_received") or {}
    for doc_type in required_docs():
        if received.get(doc_type):
            lines.append(f"✓ {DOCUMENT_LABELS[doc_type]}")
    lines += ["", "Still needed:"]
    for doc_type in intake.get("missing") or []:
        lines.append(f"- {DOCUMENT_LABELS[doc_type]}")
    lines += ["", "Send them here."]
    if not strict_docs_enabled():
        lines[-1] += (
            " Or reply *SKIP* to submit now — your claim will go to an "
            "assessor, who will request them."
        )
    return "\n".join(lines)


async def handle_pending_choice(wa_id, text):
    """Handle a numeric/YES response, returning True when a choice was pending."""
    session = get_session(wa_id)
    pending = session.get("pending_choice")
    if not pending:
        return False

    vehicles = [
        vehicle for vehicle in (pending.get("vehicles") or [])
        if isinstance(vehicle, dict)
    ]
    answer = str(text or "").strip()
    selected = None
    try:
        number = int(answer)
    except (TypeError, ValueError):
        number = None
    if number is not None and 1 <= number <= len(vehicles):
        selected = vehicles[number - 1]
    elif answer.upper() in ("YES", "Y") and len(vehicles) == 1:
        selected = vehicles[0]

    if selected is not None:
        reply = _single_vehicle_reply(
            pending.get("claim_id", "unknown"),
            selected,
            excluded_count=len(selected.get("excluded_photos") or []),
            total_photos=len(selected.get("photos") or []),
            undetermined=pending.get("undetermined"),
            decision=pending.get("decision"),
            decision_reasons=pending.get("decision_reasons"),
        )
        await send_text(wa_id, reply)
        session["pending_choice"] = None
        SESSIONS.pop(wa_id, None)
        return True

    session["updated"] = time.time()
    prompt = session.get("pending_prompt")
    if not prompt:
        prompt = (
            _selection_reply(pending.get("claim_id", "unknown"), vehicles)
            if len(vehicles) > 1
            else (
                f"Reply *YES* to continue with the "
                f"{vehicles[0].get('label') if vehicles else 'vehicle'}, "
                "or send fresh photos."
            )
        )
    await send_text(wa_id, prompt)
    return True


async def process_session(wa_id, skip_missing=False):
    keep_session = False
    try:
        session = SESSIONS.get(wa_id) or {}
        intake = classify_session_intake(session)
        missing = list(intake.get("missing") or [])
        if not intake.get("vehicle_count"):
            session["awaiting_docs"] = True
            session["updated"] = time.time()
            keep_session = True
            await send_text(wa_id, checklist_reply(intake))
            return
        if missing and (not skip_missing or strict_docs_enabled()):
            session["awaiting_docs"] = True
            session["updated"] = time.time()
            keep_session = True
            await send_text(wa_id, checklist_reply(intake))
            return

        forced_review_reasons = []
        if missing:
            missing_names = ", ".join(
                DOCUMENT_LABELS[doc_type].lower() for doc_type in missing
            )
            forced_review_reasons.append(
                f"documents not provided: {missing_names}"
            )

        session["awaiting_docs"] = False
        await send_text(wa_id, "Analysing your claim. This takes about 30 seconds.")

        claim_id = "CLM-" + uuid.uuid4().hex[:8].upper()
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

        payload = await analyse_claim(
            claim_id,
            doc_paths,
            photo_paths,
            source="whatsapp",
            from_number=wa_id,
            forced_review_reasons=forced_review_reasons,
        )
        reply = format_reply(payload)
        vehicles = [
            vehicle for vehicle in (payload.get("vehicles") or [])
            if isinstance(vehicle, dict) and vehicle.get("status") == "claimable"
        ]
        has_duplicate_group = any(
            isinstance(vehicle, dict) and vehicle.get("status") == "all_duplicate"
            for vehicle in (payload.get("vehicles") or [])
        )
        needs_choice = (
            payload.get("status") == "analysed"
            and (
                payload.get("claimable_count", len(vehicles)) > 1
                or (len(vehicles) == 1 and has_duplicate_group)
            )
        )
        if needs_choice:
            session["pending_choice"] = {
                "claim_id": payload.get("claim_id", claim_id),
                "vehicles": vehicles,
                "undetermined": payload.get("undetermined") or [],
                "decision": payload.get("decision"),
                "decision_reasons": payload.get("decision_reasons") or [],
            }
            session["pending_prompt"] = reply
            session["updated"] = time.time()
            keep_session = True
        await send_text(wa_id, reply)
    except Exception:
        print("[whatsapp] process_session failed:", flush=True)
        traceback.print_exc()
        await send_text(wa_id, "Something went wrong processing your claim. Please try again.")
    finally:
        if not keep_session:
            SESSIONS.pop(wa_id, None)
