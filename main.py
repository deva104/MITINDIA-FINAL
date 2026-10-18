import copy
import hashlib
import json
import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, List, Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import (
    audit_log_add,
    audit_log_for_claim,
    current_claim_id,
    database_backend,
    database_ok,
    get_claim,
    get_conn,
    init_db,
    list_claims,
    media_get,
    save_claim,
    save_file,
    storage_stats,
)
from gemini import assign_damage_ids, assess_damage, classify_image, extract_fields
from rules import assess_confidence, build_estimate, run_rules
from vision import fallbacks_used, reset_cache_tracking, stage_was_cached

import whatsapp as wa
from whatsapp import (
    download_media,
    get_session,
    process_session,
    remember_message,
    send_text,
    subscribe_app,
    verify,
)

try:
    from forensics import (
        build_photo_url_map,
        check_duplicates,
        prepare_uploaded_image,
        run_deep_forensics,
    )
except ImportError:
    def build_photo_url_map(photo_paths):
        return {}

    def check_duplicates(photo_paths, conn):
        return []

    def prepare_uploaded_image(data):
        return data or None

    def run_deep_forensics(photo_paths, conn=None, fields=None):
        return []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")
MAX_CLAIM_IMAGES = 8
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff",
}
DOCUMENT_TYPES = {"rc", "driving_licence", "claim_form", "policy"}
DOCUMENT_LABELS = {
    "rc": "Registration certificate",
    "driving_licence": "Driving licence",
    "claim_form": "Claim form",
    "policy": "Insurance policy",
}


def required_document_types():
    configured = os.getenv(
        "REQUIRED_DOCS", "rc,driving_licence,claim_form"
    )
    return [
        value.strip().lower()
        for value in configured.split(",")
        if value.strip().lower() in DOCUMENT_TYPES
    ]


def infer_document_types(fields):
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClaimPilot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    ok = database_ok()
    return {"ok": ok, "db": database_backend(), "db_ok": ok}


@app.get("/api/storage")
def storage():
    return storage_stats()


@app.get("/media/{media_id}")
def media_file(media_id: str):
    if len(media_id) != 64 or any(character not in "0123456789abcdef" for character in media_id):
        raise HTTPException(status_code=404, detail="media not found")
    found = media_get(media_id)
    if not found:
        raise HTTPException(status_code=404, detail="media not found")
    data, mime = found
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


class OverrideRequest(BaseModel):
    actor: Optional[str] = None
    action: Optional[str] = None
    target: Optional[str] = None
    value: Any = None
    reason: Optional[str] = None


OVERRIDE_ACTIONS = {
    "confirm_damage", "reject_damage", "set_severity", "set_decision",
}
SEVERITY_VALUES = {"minor", "moderate", "severe"}
DECISION_VALUES = {"approved", "rejected", "more_info"}
WHATSAPP_SERVICE_WINDOW_SECONDS = 24 * 60 * 60


def _damage_fingerprint(item):
    return tuple(
        str(item.get(key) or "").strip().lower()
        for key in (
            "source_photo", "part", "damage_type", "severity", "reasoning",
        )
    )


def _derive_legacy_damage_ids(payload):
    """Add deterministic response-only IDs to claims saved before damage IDs."""
    next_index = 0
    available = {}
    vehicles = [
        vehicle for vehicle in (payload.get("vehicles") or [])
        if isinstance(vehicle, dict)
    ]

    if vehicles:
        for vehicle in vehicles:
            for item in vehicle.get("damage") or []:
                if not isinstance(item, dict):
                    continue
                if not item.get("damage_id"):
                    item["damage_id"] = f"legacy-{next_index}"
                    next_index += 1
                available.setdefault(_damage_fingerprint(item), []).append(
                    item["damage_id"]
                )

    used_flat = set()
    for item in payload.get("damage") or []:
        if not isinstance(item, dict):
            continue
        if item.get("damage_id"):
            used_flat.add(item["damage_id"])
            continue
        match = next(
            (
                damage_id
                for damage_id in available.get(_damage_fingerprint(item), [])
                if damage_id not in used_flat
            ),
            None,
        )
        if match is None:
            match = f"legacy-{next_index}"
            next_index += 1
        item["damage_id"] = match
        used_flat.add(match)
    return payload


def _public_audit_row(row):
    public = dict(row)
    for source, target in (("before_json", "before"), ("after_json", "after")):
        try:
            public[target] = json.loads(public[source]) if public.get(source) else None
        except (TypeError, ValueError):
            public[target] = None
    return public


def _apply_damage_overrides(damage, audit_rows):
    effective = [
        copy.deepcopy(item) for item in (damage or []) if isinstance(item, dict)
    ]
    by_id = {
        item.get("damage_id"): item
        for item in effective
        if item.get("damage_id")
    }
    for row in audit_rows:
        item = by_id.get(row.get("target"))
        if not item:
            continue
        action = row.get("action")
        after = row.get("after") or {}
        if action == "confirm_damage":
            item["confirmed"] = True
            item["confirmed_by_override"] = True
        elif action == "reject_damage":
            item["rejected"] = True
        elif action == "set_severity" and after.get("severity") in SEVERITY_VALUES:
            item["severity"] = after["severity"]
    return effective


def _damage_catalog(payload, effective=False):
    catalog = {}
    vehicle_key = "effective_vehicles" if effective else "vehicles"
    damage_key = "effective_damage" if effective else "damage"
    for vehicle in payload.get(vehicle_key) or []:
        if not isinstance(vehicle, dict):
            continue
        for item in vehicle.get("damage") or []:
            if isinstance(item, dict) and item.get("damage_id"):
                catalog[item["damage_id"]] = item
    for item in payload.get(damage_key) or []:
        if isinstance(item, dict) and item.get("damage_id"):
            catalog.setdefault(item["damage_id"], item)
    return catalog


def build_claim_response(payload):
    response = _derive_legacy_damage_ids(copy.deepcopy(payload))
    audit_rows = [
        _public_audit_row(row)
        for row in audit_log_for_claim(response.get("claim_id"))
    ]
    response["overrides"] = audit_rows
    response["effective_damage"] = _apply_damage_overrides(
        response.get("damage"), audit_rows
    )
    response["effective_estimate"] = build_estimate(
        response["effective_damage"]
    )

    effective_vehicles = []
    for vehicle in response.get("vehicles") or []:
        if not isinstance(vehicle, dict):
            continue
        effective_vehicle = copy.deepcopy(vehicle)
        effective_vehicle["damage"] = _apply_damage_overrides(
            vehicle.get("damage"), audit_rows
        )
        effective_vehicle["estimate"] = build_estimate(
            effective_vehicle["damage"]
        )
        effective_vehicles.append(effective_vehicle)
    response["effective_vehicles"] = effective_vehicles

    response["final_decision"] = None
    for row in audit_rows:
        if row.get("action") != "set_decision":
            continue
        after = row.get("after") or {}
        response["final_decision"] = {
            "value": after.get("value"),
            "actor": row.get("actor"),
            "at": row.get("created_at"),
            "reason": row.get("reason"),
        }
    return response


@app.get("/api/claims")
def claims():
    return list_claims()


@app.get("/api/claims/{claim_id}")
def claim_detail(claim_id: str):
    payload = get_claim(claim_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return build_claim_response(payload)


def _rupees(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


async def _notify_whatsapp_decision(
    claim_id, payload, actor, decision, reason
):
    if payload.get("source") != "whatsapp":
        return
    wa_id = payload.get("from_number")
    try:
        last_inbound = wa.last_inbound_at(wa_id)
        if (
            not wa_id
            or last_inbound is None
            or time.time() - last_inbound > WHATSAPP_SERVICE_WINDOW_SECONDS
        ):
            audit_log_add(
                claim_id,
                actor,
                "notification_not_sent",
                "claim",
                None,
                {"channel": "whatsapp", "status": "not_sent"},
                "notification not sent: outside 24h service window",
            )
            return

        current = build_claim_response(payload)
        estimate = current.get("effective_estimate") or {}
        if decision == "approved":
            message = (
                f"*Claim {claim_id} approved* — "
                f"Rs {_rupees(estimate.get('total_low'))} – "
                f"Rs {_rupees(estimate.get('total_high'))}. "
                "An assessor has reviewed your claim."
            )
        elif decision == "rejected":
            message = f"*Claim {claim_id} not approved* — {reason.rstrip('.')}."
        else:
            message = (
                f"*More information needed for {claim_id}* — "
                f"{reason.rstrip('.')}.\n"
                "Reply here with the requested photos or documents."
            )

        sent = await send_text(wa_id, message)
        if sent:
            audit_log_add(
                claim_id,
                actor,
                "notification_sent",
                "claim",
                None,
                {
                    "channel": "whatsapp",
                    "status": "sent",
                    "message": message,
                },
                "customer notified inside 24h service window",
            )
        else:
            audit_log_add(
                claim_id,
                actor,
                "notification_not_sent",
                "claim",
                None,
                {"channel": "whatsapp", "status": "delivery_failed"},
                "notification not sent: WhatsApp delivery failed",
            )
    except Exception:
        print(
            f"[override] {claim_id}: WhatsApp notification failed",
            flush=True,
        )
        traceback.print_exc()


@app.post("/api/claims/{claim_id}/override")
async def override_claim(claim_id: str, request: OverrideRequest):
    actor = str(request.actor or "").strip()
    action = str(request.action or "").strip()
    target = str(request.target or "").strip()
    reason = str(request.reason or "").strip()
    if not actor:
        raise HTTPException(status_code=400, detail="actor is required")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    if action not in OVERRIDE_ACTIONS:
        raise HTTPException(status_code=400, detail="invalid override action")

    payload = get_claim(claim_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="claim not found")
    current = build_claim_response(payload)

    if action == "set_decision":
        value = str(request.value or "").strip().lower()
        if target != "claim":
            raise HTTPException(status_code=400, detail="target must be claim")
        if value not in DECISION_VALUES:
            raise HTTPException(status_code=400, detail="invalid decision value")
        before = (
            current.get("final_decision")
            or {"value": current.get("decision"), "source": "system"}
        )
        after = {"value": value}
    else:
        catalog = _damage_catalog(current, effective=True)
        before_item = catalog.get(target)
        if before_item is None:
            raise HTTPException(
                status_code=400, detail="target does not exist on this claim"
            )
        before = copy.deepcopy(before_item)
        after = copy.deepcopy(before_item)
        if action == "confirm_damage":
            after["confirmed"] = True
            after["confirmed_by_override"] = True
        elif action == "reject_damage":
            after["rejected"] = True
        else:
            value = str(request.value or "").strip().lower()
            if value not in SEVERITY_VALUES:
                raise HTTPException(status_code=400, detail="invalid severity value")
            after["severity"] = value

    audit_log_add(
        claim_id, actor, action, target, before, after, reason
    )
    if action == "set_decision":
        await _notify_whatsapp_decision(
            claim_id, payload, actor, after["value"], reason
        )
    return build_claim_response(payload)


@app.post("/api/demo/reset")
def demo_reset():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM claims")
        conn.execute("DELETE FROM media")
        conn.commit()
    finally:
        conn.close()
    print("[demo] database cleared", flush=True)
    return {"cleared": True}


RULE_FIELD_IMPACTS = {
    "insured_name_claim_form": (
        "Insured name", "identity consistency not verified"
    ),
    "owner_name_rc": (
        "Registered owner name", "identity consistency not verified"
    ),
    "holder_name_dl": (
        "Driving licence holder name", "identity consistency not verified"
    ),
    "registration_number_claim_form": (
        "Claim registration number", "vehicle registration match not verified"
    ),
    "registration_number_rc": (
        "RC registration number", "vehicle registration match not verified"
    ),
    "dl_expiry_date": (
        "Driving licence expiry", "licence validity at time of loss not verified"
    ),
    "accident_date": (
        "Accident date", "date-dependent validity checks not verified"
    ),
    "policy_start_date": (
        "Policy start date", "policy activity at time of loss not verified"
    ),
    "policy_end_date": (
        "Policy end date", "policy activity at time of loss not verified"
    ),
    "accident_description": (
        "Accident description",
        "reported narrative could not be compared with visible damage",
    ),
}
PRESERVE_LABEL_CASE = {
    "BMW", "SUV", "VXI", "ZXI", "LXI", "CR-V", "HR-V", "MG", "XUV",
    "GTI", "AMG",
}


def format_vehicle_label(parts):
    words = []
    for token in " ".join(str(part) for part in parts if part).split():
        upper = token.upper()
        if upper in PRESERVE_LABEL_CASE or token.isupper():
            words.append(upper)
        elif "-" in token:
            words.append("-".join(
                segment.upper() if len(segment) <= 2
                else segment[:1].upper() + segment[1:].lower()
                for segment in token.split("-")
            ))
        elif (
            len(token) <= 4
            and (
                any(character.isdigit() for character in token)
                or (
                    token.isalpha()
                    and not any(vowel in token.lower() for vowel in "aeiou")
                )
            )
        ):
            words.append(upper)
        else:
            words.append(token[:1].upper() + token[1:].lower())
    return " ".join(words)


def build_undetermined(payload, provider_fallbacks=None):
    entries = []
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        status = field.get("status")
        field_info = RULE_FIELD_IMPACTS.get(field.get("name"))
        if status == "unreadable":
            reason = "value present but illegible"
            if not field_info:
                item = str(field.get("name") or "Document field").replace("_", " ").title()
                field_info = (item, "submitted value not verified")
        elif status == "not_present":
            if not field_info:
                continue
            reason = "not present in submitted documents"
        else:
            continue
        entries.append({
            "item": field_info[0],
            "reason": reason,
            "impact": field_info[1],
        })

    for item in payload.get("damage") or []:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.6:
            part = str(item.get("part") or "Unknown part").replace("_", " ").title()
            entries.append({
                "item": f"{part} damage",
                "reason": "low model confidence",
                "impact": "excluded from the headline estimate",
            })

    for result in payload.get("classifications") or []:
        if not isinstance(result, dict):
            continue
        try:
            confidence = float(result.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.6:
            entries.append({
                "item": result.get("filename") or "Uploaded image",
                "reason": f"low classification confidence ({confidence:.2f})",
                "impact": "kept as a vehicle photo; document type not established",
            })

    received = payload.get("documents_received") or {}
    for doc_type in payload.get("required_documents") or []:
        if not received.get(doc_type):
            entries.append({
                "item": DOCUMENT_LABELS.get(doc_type, doc_type.replace("_", " ").title()),
                "reason": "required document not provided",
                "impact": "claim documentation incomplete",
            })

    vehicle_photos = [
        photo for photo in (payload.get("photos") or [])
        if isinstance(photo, dict) and photo.get("is_vehicle") is True
    ]
    if vehicle_photos and any(not photo.get("number_plate") for photo in vehicle_photos):
        entries.append({
            "item": "Number plate",
            "reason": "not legible in photos",
            "impact": "vehicle identity not verified from the photographs",
        })

    if any(
        isinstance(item, dict)
        and item.get("check") == "exif"
        and item.get("verdict") == "unverifiable"
        for item in (payload.get("forensics") or [])
    ):
        entries.append({
            "item": "Photo capture time and location",
            "reason": "metadata stripped in transit",
            "impact": "cannot confirm when or where photos were taken",
        })

    for check in payload.get("forensics") or []:
        if not isinstance(check, dict) or check.get("check") != "ai_generated":
            continue
        if check.get("verdict") == "inconclusive":
            try:
                score = f"{float(check.get('score')):.2f}"
            except (TypeError, ValueError):
                score = "unknown"
            entries.append({
                "item": "Photo authenticity",
                "reason": f"AI-generation score inconclusive ({score})",
                "impact": "authenticity not confirmed",
            })
        elif check.get("verdict") == "unavailable":
            entries.append({
                "item": "AI-generation check",
                "reason": check.get("detail") or "classifier unavailable",
                "impact": "photo authenticity not independently checked",
            })

    for fallback in provider_fallbacks or []:
        entries.append({
            "item": "Primary vision provider",
            "reason": (
                f"{fallback['from']} unavailable, fell back to {fallback['to']}"
            ),
            "impact": "none — result verified by the fallback",
        })

    unique = []
    seen = set()
    for entry in entries:
        key = (entry["item"], entry["reason"], entry["impact"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def build_vehicle_groups(photos, damage, excluded, photo_urls=None):
    """Split photos, attributed damage, and estimates by model-assigned vehicle."""
    photo_entries = [photo for photo in (photos or []) if isinstance(photo, dict)]
    damage_items = [item for item in (damage or []) if isinstance(item, dict)]
    excluded_names = [os.path.basename(str(name)) for name in (excluded or []) if name]
    excluded_lookup = {name.lower() for name in excluded_names}
    photo_url_lookup = {
        str(name).lower(): url
        for name, url in (photo_urls or {}).items()
        if url
    }

    explicit_groups = []
    pending_vehicle = []
    pending_non_vehicle = []
    for photo in photo_entries:
        name = os.path.basename(str(photo.get("source_photo") or ""))
        if not name:
            continue
        group = photo.get("vehicle_group")
        if isinstance(group, int) and not isinstance(group, bool) and group >= 0:
            explicit_groups.append((group, photo, name))
        elif photo.get("is_vehicle") is False:
            pending_non_vehicle.append((photo, name))
        else:
            pending_vehicle.append((photo, name))

    grouped = {}
    for group, photo, name in explicit_groups:
        grouped.setdefault(group, []).append((photo, name))

    next_group = max(grouped, default=-1) + 1
    for pending in (pending_vehicle, pending_non_vehicle):
        if pending:
            grouped[next_group] = pending
            next_group += 1

    assigned_names = {
        name.lower()
        for entries in grouped.values()
        for _, name in entries
    }
    unassigned_excluded = [
        name for name in excluded_names if name.lower() not in assigned_names
    ]
    if unassigned_excluded:
        grouped[next_group] = [
            ({"is_vehicle": None}, name) for name in unassigned_excluded
        ]

    vehicles = []
    for group in sorted(grouped):
        entries = grouped[group]
        group_photos = [name for _, name in entries]
        group_lookup = {name.lower() for name in group_photos}
        group_excluded = [
            name for name in group_photos if name.lower() in excluded_lookup
        ]
        group_photo_urls = [
            photo_url_lookup.get(name.lower()) for name in group_photos
        ]
        group_excluded_urls = [
            photo_url_lookup.get(name.lower()) for name in group_excluded
        ]
        group_damage = [
            item for item in damage_items
            if os.path.basename(str(item.get("source_photo") or "")).lower() in group_lookup
        ]

        label_parts = []
        for key in ("colour", "make_model", "body_type"):
            value = next(
                (
                    str(photo.get(key)).strip()
                    for photo, _ in entries
                    if photo.get(key) is not None and str(photo.get(key)).strip()
                ),
                None,
            )
            if value:
                label_parts.append(value)
        if label_parts:
            label = format_vehicle_label(label_parts)
        else:
            label = f"Vehicle {group + 1}"

        if entries and all(photo.get("is_vehicle") is False for photo, _ in entries):
            status = "not_vehicle"
        elif group_photos and len(group_excluded) == len(group_photos):
            status = "all_duplicate"
        elif any(name.lower() not in excluded_lookup for name in group_photos) and not group_damage:
            status = "no_damage"
        else:
            status = "claimable"

        vehicles.append({
            "group": group,
            "label": label,
            "photos": group_photos,
            "photo_urls": group_photo_urls,
            "excluded_photos": group_excluded,
            "excluded_photo_urls": group_excluded_urls,
            "damage": group_damage,
            "estimate": build_estimate(group_damage),
            "status": status,
        })

    return vehicles


async def analyse_claim(
    claim_id, doc_paths, photo_paths, source="web", from_number=None,
    forced_review_reasons=None,
):
    """Staged pipeline shared by the HTTP upload route and the WhatsApp webhook."""
    started = time.perf_counter()
    temp_inputs = [path for path in list(doc_paths or []) + list(photo_paths or []) if path]
    claim_token = current_claim_id.set(claim_id)

    original_doc_paths = list(doc_paths or [])
    photo_paths = list(photo_paths or [])
    direct_doc_paths = [
        path for path in original_doc_paths
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS
    ]
    doc_image_paths = [
        path for path in original_doc_paths
        if os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS
    ]
    all_images = photo_paths + doc_image_paths
    kept_images = all_images[:MAX_CLAIM_IMAGES]
    intake_excluded = [
        os.path.basename(path) for path in all_images[MAX_CLAIM_IMAGES:]
    ]
    kept_lookup = {os.path.abspath(path) for path in kept_images}
    photo_paths = [
        path for path in photo_paths if os.path.abspath(path) in kept_lookup
    ]
    doc_image_paths = [
        path for path in doc_image_paths if os.path.abspath(path) in kept_lookup
    ]

    timings = []
    excluded_photos = []
    provider_fallbacks = []
    photo_url_map = {}
    classifications = []
    unclassified = []
    documents_received = {doc_type: 0 for doc_type in DOCUMENT_TYPES}
    required_documents = required_document_types()
    forced_review_reasons = [
        str(reason).strip()
        for reason in (forced_review_reasons or [])
        if str(reason).strip()
    ]

    def stage_done(name, since, cached=False):
        elapsed = round(time.perf_counter() - since, 2)
        timings.append({"stage": name, "seconds": elapsed, "cached": bool(cached)})
        print(f"[analyze] {claim_id} {name}: {elapsed:.2f}s", flush=True)

    def make_payload(status, stopped_at, fields=None, damage=None, forensics=None,
                     flags=None, estimate=None, vehicle_description="", photos=None,
                     vehicles=None, claimable_count=0):
        payload = {
            "claim_id": claim_id,
            "created_at": datetime.now().isoformat(),
            "source": source,
            "from_number": from_number,
            "status": status,
            "stopped_at": stopped_at,
            "vehicle_description": vehicle_description,
            "photos": photos or [],
            "timings": list(timings),
            "excluded_photos": list(excluded_photos),
            "intake_excluded": list(intake_excluded),
            "classifications": list(classifications),
            "documents_received": dict(documents_received),
            "required_documents": list(required_documents),
            "unclassified": list(unclassified),
            "fields": fields or [],
            "damage": damage or [],
            "forensics": forensics or [],
            "flags": flags or [],
            "estimate": estimate if estimate is not None else build_estimate([]),
            "vehicles": vehicles or [],
            "claimable_count": claimable_count,
        }
        payload["undetermined"] = build_undetermined(
            payload, provider_fallbacks
        )
        assessment = assess_confidence(payload)
        payload["decision"] = assessment["decision"]
        payload["decision_reasons"] = assessment["reasons"]
        payload["confidence"] = assessment["confidence"]
        if forced_review_reasons and payload["decision"] != "reject":
            payload["decision"] = "needs_review"
            payload["decision_reasons"] = list(dict.fromkeys(
                forced_review_reasons + payload["decision_reasons"]
            ))
        return payload

    def finish(payload):
        save_claim(claim_id, payload)
        print(f"[analyze] {claim_id} status={payload['status']} "
              f"stopped_at={payload['stopped_at']} total {time.perf_counter() - started:.2f}s", flush=True)
        return payload

    try:
        conn = get_conn()
    except Exception:
        current_claim_id.reset(claim_token)
        _release_temp_uploads(temp_inputs)
        raise
    try:
        # Stage 0 - duplicate screen, before spending any Gemini quota
        mark = time.perf_counter()
        dup = check_duplicates(photo_paths, conn)
        stage_done("stage0_duplicate", mark)

        flagged = {d["photo_path"] for d in dup
                   if d.get("verdict") == "suspicious" and d.get("photo_path")}
        dup_paths = [p for p in photo_paths if os.path.abspath(p) in flagged]
        clean_paths = [p for p in photo_paths if os.path.abspath(p) not in flagged]
        excluded_photos = [os.path.basename(p) for p in dup_paths]
        photo_url_map = build_photo_url_map(kept_images)

        # Only a bundle with nothing left to assess is rejected outright. A claim
        # with at least one fresh photo is still worth analysing.
        if dup_paths and not clean_paths:
            vehicles = build_vehicle_groups(
                [], [], excluded_photos, photo_url_map
            )
            return finish(make_payload("rejected", "duplicate", forensics=dup,
                                       vehicles=vehicles))

        bundle_flags = []
        if dup_paths:
            bundle_flags.append({
                "rule": "duplicate_photo_in_bundle",
                "severity": "HIGH",
                "message": (
                    f"{len(dup_paths)} of {len(photo_paths)} submitted photos were already "
                    "used in an earlier claim and have been excluded from this assessment."
                ),
                "evidence": list(excluded_photos),
            })
            print(f"[analyze] {claim_id} excluding {len(dup_paths)} duplicate photo(s): "
                  f"{', '.join(excluded_photos)}", flush=True)

        # Stage 0.5 - classify each image independently, then route it.
        mark = time.perf_counter()
        reset_cache_tracking()
        routed_photo_paths = []
        routed_doc_paths = list(direct_doc_paths)
        for path in clean_paths + doc_image_paths:
            result = classify_image(path)
            doc_type = result.get("doc_type")
            try:
                confidence = float(result.get("confidence"))
            except (TypeError, ValueError):
                confidence = 0.0
            entry = {
                "filename": os.path.basename(path),
                "doc_type": doc_type,
                "confidence": confidence,
                "number_plate": result.get("number_plate"),
                "url": photo_url_map.get(os.path.basename(path).lower()),
            }
            classifications.append(entry)
            if confidence < 0.6 or doc_type == "vehicle_photo":
                routed_photo_paths.append(path)
            elif doc_type in DOCUMENT_TYPES:
                routed_doc_paths.append(path)
                documents_received[doc_type] += 1
            else:
                unclassified.append(entry)
        clean_paths = routed_photo_paths
        doc_paths = routed_doc_paths
        stage_done("stage0_5_classify", mark, cached=stage_was_cached())

        # Stage 1 - Gemini extraction and damage assessment
        mark = time.perf_counter()
        reset_cache_tracking()
        fields = extract_fields(doc_paths) if doc_paths else []
        if direct_doc_paths:
            for doc_type in infer_document_types(fields):
                documents_received[doc_type] = max(
                    1, documents_received.get(doc_type, 0)
                )
        result = assess_damage(clean_paths)
        provider_fallbacks = list(fallbacks_used())
        stage_done("stage1_gemini", mark, cached=stage_was_cached())

        description = result.get("vehicle_description") or ""
        photos = result.get("photos") or []
        plates = {
            str(item.get("filename") or "").lower(): item.get("number_plate")
            for item in classifications
            if item.get("number_plate")
        }
        for photo in photos:
            if isinstance(photo, dict):
                photo["number_plate"] = plates.get(
                    os.path.basename(str(photo.get("source_photo") or "")).lower()
                )
        raw_damage = result.get("damage") or []
        assign_damage_ids(photos, raw_damage)
        vehicles = build_vehicle_groups(
            photos, raw_damage, excluded_photos, photo_url_map
        )
        claimable_count = sum(
            vehicle.get("status") == "claimable" for vehicle in vehicles
        )
        if result.get("error"):
            return finish(make_payload("error", "ai_unavailable", fields=fields, forensics=dup,
                                       flags=bundle_flags, vehicle_description=description,
                                       photos=photos, vehicles=vehicles,
                                       claimable_count=claimable_count))
        if (result.get("is_vehicle") is False
                and not any(photo.get("is_vehicle") is True for photo in photos)):
            return finish(make_payload("rejected", "not_a_vehicle", fields=fields, forensics=dup,
                                       flags=bundle_flags, vehicle_description=description,
                                       photos=photos, vehicles=vehicles,
                                       claimable_count=claimable_count))

        # Stage 2 - ELA + EXIF
        mark = time.perf_counter()
        vehicle_photo_names = {
            os.path.basename(str(photo.get("source_photo") or "")).lower()
            for photo in photos
            if isinstance(photo, dict) and photo.get("is_vehicle") is True
        }
        vehicle_paths = [
            path for path in clean_paths
            if os.path.basename(path).lower() in vehicle_photo_names
        ]
        deep = run_deep_forensics(vehicle_paths, conn, fields)
        stage_done("stage2_deep_forensics", mark)

        # Stage 3 - rules and estimate
        mark = time.perf_counter()
        primary_vehicle = next(
            (vehicle for vehicle in vehicles if vehicle.get("status") == "claimable"),
            None,
        )
        damage = list(primary_vehicle.get("damage") or []) if primary_vehicle else []
        estimate = (
            primary_vehicle.get("estimate")
            if primary_vehicle
            else build_estimate([])
        )
        flags = bundle_flags + run_rules(fields, damage, photo_count=len(clean_paths),
                                         is_vehicle=result.get("is_vehicle"), photos=photos,
                                         vehicles=vehicles,
                                         claimable_count=claimable_count,
                                         forensics=deep)
        stage_done("stage3_rules", mark)

        return finish(make_payload("analysed", None, fields=fields, damage=damage,
                                   forensics=dup + deep, flags=flags, estimate=estimate,
                                   vehicle_description=description, photos=photos,
                                   vehicles=vehicles, claimable_count=claimable_count))
    finally:
        conn.close()
        current_claim_id.reset(claim_token)
        _release_temp_uploads(temp_inputs)


def _release_temp_uploads(paths):
    """Delete request files under uploads/. Fixtures and other paths stay."""
    root = os.path.abspath(UPLOAD_DIR)
    parents = set()
    for path in paths or []:
        if not path:
            continue
        absolute = os.path.abspath(path)
        try:
            if os.path.commonpath([root, absolute]) != root:
                continue
        except ValueError:
            continue
        parents.add(os.path.dirname(absolute))
        if not os.path.isfile(absolute):
            continue
        try:
            os.remove(absolute)
        except OSError as error:
            print(f"[storage] could not delete temp file: {type(error).__name__}", flush=True)
    for directory in sorted(parents, key=len, reverse=True):
        if directory == root:
            continue
        try:
            os.rmdir(directory)
        except OSError:
            pass


@app.post("/api/analyze")
async def analyze(
    documents: List[UploadFile] = File(default=[]),
    photos: List[UploadFile] = File(default=[]),
):
    claim_id = "CLM-" + uuid.uuid4().hex[:8].upper()
    claim_dir = os.path.join(UPLOAD_DIR, claim_id)
    os.makedirs(claim_dir, exist_ok=True)

    doc_paths, photo_paths = [], []
    for upload, kind, bucket in (
        [(u, "document", doc_paths) for u in documents]
        + [(u, "photo", photo_paths) for u in photos]
    ):
        filename = os.path.basename(upload.filename or "unnamed")
        dest = os.path.join(claim_dir, filename)
        with open(dest, "wb") as fh:
            fh.write(await upload.read())
        save_file(claim_id, filename, kind, dest)
        bucket.append(dest)

    return await analyse_claim(claim_id, doc_paths, photo_paths)


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/tif": ".tiff",
    "application/pdf": ".pdf",
}


def _media_extension(media_obj, fallback):
    filename = str((media_obj or {}).get("filename") or "")
    extension = os.path.splitext(filename)[1].lower()
    if extension:
        return extension
    mime = str((media_obj or {}).get("mime_type") or "").lower()
    return MIME_EXTENSIONS.get(mime, fallback)


def _is_image_media(media_obj, fallback_ext=""):
    mime = str((media_obj or {}).get("mime_type") or "").lower()
    extension = _media_extension(media_obj, fallback_ext).lower()
    return mime.startswith("image/") or extension in IMAGE_EXTENSIONS


def _photo_receipt(count):
    return (
        f"Photo received ({count} total). Reply DONE when you are done."
    )


async def _store_wa_media(wa_id, message_id, media_obj, bucket, fallback_ext=".jpg"):
    session = get_session(wa_id)
    media_obj = media_obj or {}
    analysing = bool(session.get("analysing"))
    active_photos = session["next_photos"] if analysing else session["photos"]
    if bucket == "photos" and len(active_photos) >= wa.MAX_IMAGES_PER_CLAIM:
        if not session.get("limit_notified"):
            await send_text(
                wa_id,
                "Maximum 8 images per claim — keeping the first 8.",
            )
            session["limit_notified"] = True
        session["updated"] = time.time()
        return

    data = await download_media(media_obj)
    if data is None:
        await send_text(wa_id, "I could not download that file. Please send it again.")
        return

    extension = _media_extension(media_obj, fallback_ext)
    if bucket == "photos":
        digest = hashlib.sha256(data).hexdigest()
        if digest in set(session.get("next_hashes") or []):
            await send_text(wa_id, _photo_receipt(len(session.get("next_photos") or [])))
            return
        if digest in set(session.get("content_hashes") or []):
            await send_text(wa_id, _photo_receipt(len(session.get("photos") or [])))
            return
        prepared = prepare_uploaded_image(data)
        if prepared is None:
            await send_text(wa_id, wa.UNREADABLE_IMAGE_REPLY)
            return
        if prepared is not data:
            extension = ".jpg"
            data = prepared
    else:
        digest = None

    choice_cleared = False
    if session.get("pending_choice") and not analysing:
        session = wa._blank_session()
        wa.SESSIONS[wa_id] = session
        analysing = False
        choice_cleared = True

    folder = os.path.join(UPLOAD_DIR, f"wa_{wa_id}")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"{message_id}{extension}")
    with open(dest, "wb") as fh:
        fh.write(data)

    if analysing:
        target = session["next_photos"] if bucket == "photos" else session["next_docs"]
        hashes = session.setdefault("next_hashes", set())
    else:
        target = session[bucket]
        hashes = session.setdefault("content_hashes", set())
    target.append(dest)
    if digest is not None:
        hashes.add(digest)
    session["updated"] = time.time()

    if choice_cleared:
        if bucket == "photos":
            await send_text(wa_id, wa.CHOICE_CLEARED_REPLY)
        else:
            await send_text(
                wa_id,
                "The previous vehicle choice has been cleared. This document starts a new claim. "
                "Reply DONE when you are done.",
            )
        return
    if analysing and bucket == "photos":
        await send_text(wa_id, wa.NEW_CLAIM_WHILE_BUSY)
        return
    if analysing:
        await send_text(
            wa_id,
            "Your previous claim is still being analysed — this document will start a new claim.",
        )
        return
    if bucket == "photos":
        await send_text(wa_id, _photo_receipt(len(session["photos"])))
        return
    await send_text(
        wa_id,
        f"Document received ({len(session['docs'])} total). Reply DONE when you are done.",
    )


async def handle_wa_message(message):
    """Runs in the background so the webhook can answer immediately."""
    wa_id = None
    try:
        if not isinstance(message, dict):
            return
        wa_id = message.get("from")
        if not wa_id:
            return
        wa.record_inbound(wa_id, message.get("timestamp"))
        message_id = message.get("id")
        message_type = message.get("type")

        if message_type == "image":
            await _store_wa_media(wa_id, message_id, message.get("image") or {}, "photos")
        elif message_type == "document":
            document = message.get("document") or {}
            if _is_image_media(document):
                await _store_wa_media(
                    wa_id,
                    message_id,
                    document,
                    "photos",
                    fallback_ext=_media_extension(document, ".jpg"),
                )
            else:
                await _store_wa_media(wa_id, message_id, document, "docs", fallback_ext=".bin")
        elif message_type == "text":
            text = (message.get("text") or {}).get("body") or ""
            if await wa.handle_pending_choice(wa_id, text):
                return
            session = get_session(wa_id)
            if session["photos"] or session["docs"]:
                await process_session(
                    wa_id,
                    skip_missing=wa.is_skip_command(text),
                )
            else:
                await send_text(
                    wa_id,
                    "Send a photo of the damaged vehicle to start a claim. "
                    "You can also attach the claim form, RC or driving licence. "
                    "Send any message when you are done and I will analyse it.",
                )
        elif message_type in wa.UNSUPPORTED_REPLIES:
            await send_text(wa_id, wa.UNSUPPORTED_REPLIES[message_type])
        else:
            await send_text(
                wa_id,
                "Please send a photo of the damaged vehicle to start a claim. "
                + wa.ACCEPTED_MEDIA,
            )
    except Exception:
        print("[whatsapp] handle_wa_message failed:", flush=True)
        traceback.print_exc()
        if wa_id:
            try:
                await send_text(
                    wa_id,
                    "Something went wrong reading that message. Please send it again.",
                )
            except Exception:
                traceback.print_exc()


@app.get("/webhook")
def webhook_verify(request: Request):
    params = request.query_params
    challenge = verify(
        params.get("hub.mode"), params.get("hub.verify_token"), params.get("hub.challenge")
    )
    if challenge is not None:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook")
async def webhook_receive(request: Request, background: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    try:
        value = body["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return {"ok": True}

        for message in messages:
            if not isinstance(message, dict):
                continue
            if not remember_message(message.get("id")):
                print(f"[whatsapp] skipping already-processed {message.get('id')}", flush=True)
                continue
            background.add_task(handle_wa_message, message)
    except Exception:
        print("[whatsapp] webhook parse failed:", flush=True)
        traceback.print_exc()

    return {"ok": True}


@app.post("/api/whatsapp/subscribe")
async def whatsapp_subscribe():
    return await subscribe_app()


@app.get("/api/whatsapp/status")
def whatsapp_status():
    return {
        "whatsapp_token_configured": bool(wa.WHATSAPP_TOKEN),
        "phone_number_id_configured": bool(wa.WHATSAPP_PHONE_NUMBER_ID),
        "waba_id_configured": bool(wa.WHATSAPP_WABA_ID),
        "verify_token_configured": bool(wa.WHATSAPP_VERIFY_TOKEN),
        "graph_api_version_configured": bool(wa.GRAPH_API_VERSION),
        "base_url": wa.BASE,
        "sessions": [
            {
                "wa_id": key,
                "photos": len(value.get("photos") or []),
                "documents": len(value.get("docs") or []),
                "updated": value.get("updated"),
            }
            for key, value in wa.SESSIONS.items()
        ],
        "processed_message_ids": len(wa.PROCESSED),
    }


os.makedirs(STATIC_DIR, exist_ok=True)
_index = os.path.join(STATIC_DIR, "index.html")
if not os.path.exists(_index):
    with open(_index, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><title>ClaimPilot</title><h1>ClaimPilot API is running.</h1>\n")

STATIC_OUTPUT_DIR = os.path.join(BASE_DIR, "static_output")
os.makedirs(STATIC_OUTPUT_DIR, exist_ok=True)
app.mount("/static_output", StaticFiles(directory=STATIC_OUTPUT_DIR), name="static_output")

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
