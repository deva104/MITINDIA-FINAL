import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, List, Optional

from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from db import (
    audit_log_add,
    audit_log_for_claim,
    current_claim_id,
    database_backend,
    database_ok,
    delete_session_media,
    find_document_reuse,
    get_claim,
    get_conn,
    init_db,
    list_claims,
    list_session_media,
    media_get,
    media_put,
    save_claim,
    save_file,
    utc_now_iso,
    storage_stats,
)
from gemini import (
    DOCUMENT_FIELDS,
    MAX_PDF_PAGES,
    assign_damage_ids,
    assess_damage,
    classify_image,
    extract_fields,
    render_pdf_pages,
)
from rules import (
    DOCUMENT_LABELS,
    assess_confidence,
    build_estimate,
    establish_documents,
    reported_confidence,
    route_classification,
    identity_notes,
    run_rules,
)
from vision import (
    provenance,
    provider_status,
    reset_cache_tracking,
    stage_was_cached,
    summarise_provenance,
)

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
        ai_detection_status,
        build_photo_url_map,
        check_duplicates,
        fit_jpeg,
        prepare_uploaded_image,
        run_deep_forensics,
        warm_up,
    )
except ImportError:
    def build_photo_url_map(photo_paths):
        return {}

    def check_duplicates(photo_paths, conn, claim_id=None):
        return []

    def fit_jpeg(data):
        return data

    def ai_detection_status():
        return {"enabled": False, "keys_configured": False}

    def prepare_uploaded_image(data):
        return data or None

    def run_deep_forensics(photo_paths, conn=None, fields=None):
        return []

    def warm_up():
        return None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")
MAX_CLAIM_IMAGES = 8
# Analyses run in worker threads so the event loop keeps answering webhooks.
# This caps how many run at once on a 512 MB instance; the rest queue.
ANALYSIS_SLOTS = threading.BoundedSemaphore(
    max(1, int(os.getenv("ANALYSIS_CONCURRENCY", "2") or 2))
)
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff",
}
DOCUMENT_TYPES = {"rc", "driving_licence", "claim_form", "policy"}


def document_reuse_notes(claim_id, filenames):
    """Information, never an exclusion: a document byte-identical to one in an
    earlier claim (an RC or licence is reused across claims all the time)."""
    notes = []
    try:
        reused = find_document_reuse(claim_id, filenames)
    except Exception as error:
        print(f"[analyze] document reuse lookup failed: {type(error).__name__}", flush=True)
        return notes
    for filename, claims in reused.items():
        notes.append({
            "check": "document_reuse",
            "verdict": "info",
            "detail": f"{filename}: same file as submitted with claim {', '.join(claims)}",
            "heatmap_url": None,
            "source_photo": filename,
        })
    return notes


NOT_EXTRACTED = {
    "status": "not_attempted", "fields": [], "documents": 0,
    "read": [], "failed": [], "error": None,
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


def require_admin(x_admin_token: Optional[str] = Header(default=None)):
    """Assessor and admin actions need the ADMIN_TOKEN header. Without a
    configured token every request is refused: the URL is public."""
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    supplied = (x_admin_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="admin token required")


ADMIN_ONLY = [Depends(require_admin)]

# Claim responses go to assessors only. The photos and document pages in them
# are loaded by <img>, which cannot send the admin header, so every /media URL
# in a response carries an expiring signature derived from ADMIN_TOKEN.
MEDIA_URL = re.compile(r"/media/([0-9a-f]{64})(?![0-9a-f?])")
MEDIA_URL_HOURS = 6


def media_signature(media_id, expires):
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        return None
    key = hashlib.sha256(b"claimpilot-media-url:" + token.encode("utf-8")).digest()
    message = f"{media_id}:{int(expires)}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def sign_media_urls(value, now=None):
    """Return value with every /media/<id> string signed. Expiry is rounded
    to the hour, so an image keeps one URL (and its cache entry) for an hour."""
    now = time.time() if now is None else now
    expires = (int(now) // 3600 + MEDIA_URL_HOURS + 1) * 3600

    def sign(match):
        signature = media_signature(match.group(1), expires)
        if signature is None:
            return match.group(0)
        return f"{match.group(0)}?exp={expires}&sig={signature}"

    def walk(node):
        if isinstance(node, str):
            return MEDIA_URL.sub(sign, node) if "/media/" in node else node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return walk(value)


WARM_UP = {"thread": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # In the background: the server answers at once, the first claim does not
    # pay for scipy's import.
    WARM_UP["thread"] = threading.Thread(target=warm_up, name="warm-up", daemon=True)
    WARM_UP["thread"].start()
    if not os.getenv("ADMIN_TOKEN", "").strip():
        print("[auth] ADMIN_TOKEN is not set: admin endpoints refuse every request", flush=True)
    if not os.getenv("WHATSAPP_APP_SECRET", "").strip():
        print("[auth] WARNING: WHATSAPP_APP_SECRET is not set: webhook signatures are "
              "not checked", flush=True)
    log_provider_chain()
    yield


def log_provider_chain():
    """Print the vision chain this process will use; warn about gaps. No secrets."""
    status = provider_status()
    def describe(item):
        if not item.get("supported", True):
            return f"{item['provider']} (unsupported)"
        labels = ", ".join(key["label"] for key in item.get("keys") or [])
        keys = f"{item.get('key_count', 0)} key(s): {labels}" if labels else "NO KEY"
        return f"{item['provider']} ({item.get('model')}, {keys}, <= {item.get('max_images')} images)"

    chain = " -> ".join(describe(item) for item in status["chain"])
    print(f"[vision] provider chain: {chain or 'none'}", flush=True)
    for warning in status["warnings"]:
        print(f"[vision] WARNING: {warning}", flush=True)
    return status


# The product name. The repo, env vars, tables, modules, storage keys and
# the CLM- prefix keep the old name, ClaimPilot.
SERVICE_NAME = "InsureIT"
app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    from psycopg_pool import PoolTimeout
except ImportError:  # SQLite-only installs
    PoolTimeout = None

if PoolTimeout is not None:
    @app.exception_handler(PoolTimeout)
    async def database_busy(request: Request, error):
        # Every connection stayed busy through the retry: transient. The
        # console polls again in 3 s.
        print(f"[db] {request.url.path}: pool still busy after a retry; answered 503",
              flush=True)
        return JSONResponse(
            {"detail": "The database is busy. Try again in a few seconds."},
            status_code=503, headers={"Retry-After": "3"},
        )


@app.get("/api/health")
def health():
    ok = database_ok()
    return {
        "service": SERVICE_NAME,
        "ok": ok,
        "db": database_backend(),
        "db_ok": ok,
        # Render sets RENDER_GIT_COMMIT on every deploy.
        "commit": (os.getenv("RENDER_GIT_COMMIT") or "")[:7] or None,
        # Configuration only: provider names, models and whether keys are set.
        "vision": provider_status(),
        "ai_detection": ai_detection_status(),
        # Whether inbound WhatsApp webhooks must carry Meta's signature.
        "webhook_signature": (
            "enforced" if os.getenv("WHATSAPP_APP_SECRET", "").strip() else "not_configured"
        ),
    }


@app.get("/api/storage", dependencies=ADMIN_ONLY)
def storage():
    return storage_stats()


@app.get("/media/{media_id}")
def media_file(media_id: str, exp: Optional[int] = None, sig: Optional[str] = None):
    if len(media_id) != 64 or any(character not in "0123456789abcdef" for character in media_id):
        raise HTTPException(status_code=404, detail="media not found")
    now = time.time()
    expected = media_signature(media_id, exp) if exp is not None else None
    if (
        not expected or not sig or exp < now
        or not hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="media link is invalid or has expired")
    found = media_get(media_id)
    if not found:
        raise HTTPException(status_code=404, detail="media not found")
    data, mime = found
    return Response(
        content=data,
        media_type=mime,
        # Personal documents: never stored by a shared cache.
        headers={"Cache-Control": f"private, max-age={max(0, int(exp - now))}"},
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


@app.get("/api/claims", dependencies=ADMIN_ONLY)
def claims():
    return sign_media_urls(list_claims())


@app.get("/api/claims/{claim_id}", dependencies=ADMIN_ONLY)
def claim_detail(claim_id: str):
    payload = get_claim(claim_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return sign_media_urls(build_claim_response(payload))


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
        last_inbound = await asyncio.to_thread(wa.last_inbound_at, wa_id)
        if (
            not wa_id
            or last_inbound is None
            or time.time() - last_inbound > WHATSAPP_SERVICE_WINDOW_SECONDS
        ):
            await asyncio.to_thread(
                audit_log_add,
                claim_id,
                actor,
                "notification_not_sent",
                "claim",
                None,
                {"channel": "whatsapp", "status": "not_sent"},
                "notification not sent: outside 24h service window",
            )
            return

        current = await asyncio.to_thread(build_claim_response, payload)
        estimate = current.get("effective_estimate") or {}
        if decision == "approved":
            # The estimate is a range from the rate card, not a settled amount.
            cost = (
                f" — estimated repair cost ₹{_rupees(estimate.get('total_low'))}–"
                f"₹{_rupees(estimate.get('total_high'))}"
                if (estimate.get("total_high") or 0) > 0 else ""
            )
            message = (
                f"*Claim {claim_id} approved*{cost}. "
                "The final amount is confirmed against the repair invoice."
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
            await asyncio.to_thread(
                audit_log_add,
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
            await asyncio.to_thread(
                audit_log_add,
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


@app.post("/api/claims/{claim_id}/override", dependencies=ADMIN_ONLY)
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

    payload = await asyncio.to_thread(get_claim, claim_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="claim not found")
    current = await asyncio.to_thread(build_claim_response, payload)

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

    await asyncio.to_thread(
        audit_log_add, claim_id, actor, action, target, before, after, reason
    )
    if action == "set_decision":
        await _notify_whatsapp_decision(
            claim_id, payload, actor, after["value"], reason
        )
    return sign_media_urls(await asyncio.to_thread(build_claim_response, payload))


@app.post("/api/demo/reset", dependencies=ADMIN_ONLY)
def demo_reset():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM claims")
        conn.execute("DELETE FROM media")
        # Sessions point at media ids; leaving them would strand ids whose
        # bytes were just deleted.
        conn.execute("DELETE FROM wa_session_media")
        conn.execute("DELETE FROM wa_sessions")
        conn.commit()
    finally:
        conn.close()
    print("[demo] database cleared", flush=True)
    return {"cleared": True}


RULE_FIELD_IMPACTS = {
    "insured_name_claim_form": (
        "Insured name", "identity consistency not verified"
    ),
    "driver_name_claim_form": (
        "Driver name", "driver not matched to the licence holder"
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


def build_undetermined(payload):
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
        entry = {
            "item": field_info[0],
            "reason": reason,
            "impact": field_info[1],
        }
        document = _document_label_for_field(field.get("name"))
        if status == "unreadable" and document:
            _ask(entry, f"please send a clearer photo of your {document}")
        entries.append(entry)

    for item in payload.get("damage") or []:
        if not isinstance(item, dict):
            continue
        confidence = reported_confidence(item)
        part = str(item.get("part") or "Unknown part").replace("_", " ")
        if confidence is None:
            entries.append({
                "item": f"{part.title()} damage",
                "reason": f"model did not report confidence for {part}",
                "impact": "excluded from the headline estimate until an assessor confirms it",
            })
        elif confidence < 0.6:
            entries.append({
                "item": f"{part.title()} damage",
                "reason": "low model confidence",
                "impact": "excluded from the headline estimate",
            })

    for result in payload.get("classifications") or []:
        if not isinstance(result, dict):
            continue
        confidence = reported_confidence(result)
        if confidence is None or confidence < 0.6:
            entries.append({
                "item": result.get("filename") or "Uploaded image",
                "reason": (
                    "classifier did not report confidence" if confidence is None
                    else f"low classification confidence ({confidence:.2f})"
                ),
                "impact": "kept as a vehicle photo; document type not established",
            })

    extraction = payload.get("extraction") or {}
    failed = [item for item in extraction.get("failed") or [] if isinstance(item, dict)]
    if extraction.get("status") == "failed":
        count = int(extraction.get("documents") or len(failed) or 0)
        noun = "document" if count == 1 else "documents"
        entries.append({
            "item": "Document details",
            "reason": (
                f"{count} {noun} received but could not be read: "
                f"{extraction.get('error') or 'unknown error'}"
            ),
            "impact": (
                "cross-document checks (name, registration, licence expiry) "
                "were not run"
            ),
        })
    elif extraction.get("status") == "partial":
        for item in failed:
            entry = {
                "item": f"Document details: {item.get('name') or 'document'}",
                "reason": f"could not be read: {item.get('error') or 'unknown error'}",
                "impact": "checks that need this document were not run",
            }
            if item.get("pages"):
                # Some pages of a document read in chunks: the rest was kept.
                pages = str(item["pages"])[2:]
                words = f"page {pages}" if "-" not in pages else f"pages {pages}"
                label = DOCUMENT_LABELS.get(item.get("doc_type"), "document").lower()
                entry["reason"] = (
                    f"{words} of {item.get('document') or 'the document'} could not be read: "
                    f"{item.get('error') or 'unknown error'}"
                )
                entry["impact"] = "fields on those pages were not read"
                _ask(entry, f"please send a clear photo of {words} of your {label}")
            entries.append(entry)

    document_check = payload.get("document_check") or {}
    received = document_check.get("received") or {}
    holder_read = any(
        isinstance(field, dict) and field.get("name") == "holder_name_dl"
        and field.get("status") == "found"
        for field in payload.get("fields") or []
    )
    if document_check and holder_read and not received.get("claim_form"):
        entries.append({
            "item": "Driver identity",
            "reason": "no claim form, so the licence cannot be matched to the driver",
            "impact": "driver identity not verified",
        })
    unread = int(document_check.get("unread_documents") or 0)
    for doc_type in document_check.get("missing") or []:
        label = DOCUMENT_LABELS.get(doc_type, doc_type.replace("_", " ").title())
        entries.append(_ask({
            "item": label,
            "reason": (
                f"not established: {unread} received document"
                f"{'' if unread == 1 else 's'} could not be read"
                if unread else "required document not provided"
            ),
            "impact": "claim documentation incomplete",
        }, (
            f"please send a clear photo of each page of your {label.lower()}"
            if unread else f"please send your {label.lower()}"
        )))

    vehicle_photos = [
        photo for photo in (payload.get("photos") or [])
        if isinstance(photo, dict) and photo.get("is_vehicle") is True
    ]
    if vehicle_photos and any(not photo.get("number_plate") for photo in vehicle_photos):
        entries.append(_ask({
            "item": "Number plate",
            "reason": "not legible in photos",
            "impact": "vehicle identity not verified from the photographs",
        }, "please send a clear photo of the number plate"))

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

    if any(
        isinstance(item, dict)
        and item.get("check") == "ela"
        and item.get("verdict") == "informational"
        for item in (payload.get("forensics") or [])
    ):
        entries.append({
            "item": "Photo editing",
            "reason": (
                "error-level analysis cannot tell recompression from editing "
                "on these photos"
            ),
            "impact": "editing neither confirmed nor ruled out",
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

    unique = []
    seen = set()
    for entry in entries:
        entry.setdefault("audience", "internal")
        key = (entry["item"], entry["reason"], entry["impact"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def _document_label_for_field(name):
    for doc_type, field_names in DOCUMENT_FIELDS.items():
        if name in field_names:
            return DOCUMENT_LABELS.get(doc_type, doc_type).lower()
    return None


def _ask(entry, request):
    """Mark an undetermined entry as something the customer can fix."""
    entry["audience"] = "customer_actionable"
    entry["request"] = request
    return entry


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
    forced_review_reasons=None, page_documents=None,
):
    """Staged pipeline shared by the HTTP upload route and the WhatsApp webhook.

    Everything in it is blocking (vision HTTP, Pillow, phash, DB), so it runs
    in a worker thread. The context is copied, so current_claim_id and the
    vision cache/fallback trackers behave as before.

    page_documents are pre-rendered PDFs (WhatsApp stores pages, not PDFs):
    [{"name", "paths"}], one entry per source document. They go straight to
    field extraction, like a PDF would, and are never classified or counted
    against the image cap.
    """
    return await asyncio.to_thread(
        _analyse_claim_in_slot,
        claim_id, doc_paths, photo_paths, source, from_number,
        forced_review_reasons, page_documents,
    )


def _analyse_claim_in_slot(*args):
    with ANALYSIS_SLOTS:
        return _analyse_claim_sync(*args)


def _analyse_claim_sync(
    claim_id, doc_paths, photo_paths, source="web", from_number=None,
    forced_review_reasons=None, page_documents=None,
):
    started = time.perf_counter()
    page_documents = [
        {"name": str(document.get("name") or "document"),
         "paths": [path for path in document.get("paths") or [] if path]}
        for document in page_documents or []
        if isinstance(document, dict)
    ]
    page_paths = [path for document in page_documents for path in document["paths"]]
    temp_inputs = [
        path for path in list(doc_paths or []) + list(photo_paths or []) + page_paths if path
    ]
    claim_token = current_claim_id.set(claim_id)

    original_doc_paths = list(doc_paths or [])
    photo_paths = list(photo_paths or [])
    # Documents of unknown type (PDFs and WhatsApp page sets): extracted with
    # every field, one call per document. Their type comes from what was read.
    direct_documents = [
        {"name": os.path.basename(path), "paths": [path], "doc_type": None}
        for path in original_doc_paths
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS
    ] + [dict(document, doc_type=None) for document in page_documents]
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
    photo_url_map = {}
    classifications = []
    unclassified = []
    documents_received = {doc_type: 0 for doc_type in DOCUMENT_TYPES}
    required_documents = required_document_types()
    extraction = dict(NOT_EXTRACTED)
    document_check = None
    forced_review_reasons = [
        str(reason).strip()
        for reason in (forced_review_reasons or [])
        if str(reason).strip()
    ]

    def stage_done(name, since, cached=False, calls=None):
        elapsed = round(time.perf_counter() - since, 2)
        entry = {"stage": name, "seconds": elapsed, "cached": bool(cached)}
        if calls:
            entry["providers"] = list(calls)
            entry["via"] = summarise_provenance(calls)
        timings.append(entry)
        print(f"[analyze] {claim_id} {name}: {elapsed:.2f}s", flush=True)

    def make_payload(status, stopped_at, fields=None, damage=None, forensics=None,
                     flags=None, estimate=None, vehicle_description="", photos=None,
                     vehicles=None, claimable_count=0):
        payload = {
            "claim_id": claim_id,
            "created_at": utc_now_iso(),
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
            "extraction": {
                key: value for key, value in extraction.items() if key != "fields"
            },
            "document_check": document_check,
            "notes": identity_notes(fields or []),
            "damage": damage or [],
            "forensics": forensics or [],
            "flags": flags or [],
            "estimate": estimate if estimate is not None else build_estimate([]),
            "vehicles": vehicles or [],
            "claimable_count": claimable_count,
        }
        payload["undetermined"] = build_undetermined(payload)
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
        # Stage 0 - classify every image first. Only vehicle photos go to the
        # duplicate gate: an RC or licence reused across claims is normal.
        photo_url_map = build_photo_url_map(kept_images)
        mark = time.perf_counter()
        reset_cache_tracking()
        vehicle_paths = []
        documents = list(direct_documents)
        for path in photo_paths + doc_image_paths:
            result = classify_image(path)
            entry = {
                "filename": os.path.basename(path),
                "doc_type": result.get("doc_type"),
                "confidence": reported_confidence(result),
                "number_plate": result.get("number_plate"),
                "url": photo_url_map.get(os.path.basename(path).lower()),
            }
            classifications.append(entry)
            route = route_classification(entry)
            if route == "vehicle_photo":
                vehicle_paths.append(path)
            elif route in DOCUMENT_TYPES:
                documents.append({
                    "name": os.path.basename(path), "paths": [path], "doc_type": route,
                })
            else:
                unclassified.append(entry)
        stage_done("stage0_5_classify", mark, cached=stage_was_cached(),
                   calls=provenance())

        # Stage 0.5 - duplicate screen on vehicle photos only, before any
        # damage assessment is paid for.
        mark = time.perf_counter()
        dup = check_duplicates(vehicle_paths, conn)  # excludes this claim via current_claim_id
        stage_done("stage0_duplicate", mark)
        reuse = document_reuse_notes(
            claim_id, [document["name"] for document in documents]
        )

        flagged = {d["photo_path"] for d in dup
                   if d.get("verdict") == "suspicious" and d.get("photo_path")}
        dup_paths = [p for p in vehicle_paths if os.path.abspath(p) in flagged]
        clean_paths = [p for p in vehicle_paths if os.path.abspath(p) not in flagged]
        excluded_photos = [os.path.basename(p) for p in dup_paths]

        # Only a bundle with no fresh vehicle photo is rejected outright.
        if dup_paths and not clean_paths:
            vehicles = build_vehicle_groups(
                [], [], excluded_photos, photo_url_map
            )
            return finish(make_payload("rejected", "duplicate", forensics=dup + reuse,
                                       vehicles=vehicles))

        bundle_flags = []
        if dup_paths:
            bundle_flags.append({
                "rule": "duplicate_photo_in_bundle",
                "severity": "HIGH",
                "message": (
                    f"{len(dup_paths)} of {len(vehicle_paths)} submitted vehicle photos were "
                    "already used in an earlier claim and have been excluded from this "
                    "assessment."
                ),
                "evidence": list(excluded_photos),
            })
            print(f"[analyze] {claim_id} excluding {len(dup_paths)} duplicate photo(s): "
                  f"{', '.join(excluded_photos)}", flush=True)

        # Stage 1 - Gemini extraction and damage assessment
        mark = time.perf_counter()
        reset_cache_tracking()
        extraction = extract_fields(documents) if documents else dict(NOT_EXTRACTED)
        fields = extraction["fields"]
        document_check = establish_documents(
            classifications,
            extraction,
            [document["name"] for document in direct_documents],
            required_documents,
        )
        documents_received.update(document_check["received"])
        result = assess_damage(clean_paths)
        stage_done("stage1_gemini", mark, cached=stage_was_cached(),
                   calls=provenance())

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
                                   forensics=dup + reuse + deep, flags=flags, estimate=estimate,
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


@app.post("/api/analyze", dependencies=ADMIN_ONLY)
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
        content = await upload.read()
        await asyncio.to_thread(_store_upload, claim_id, filename, kind, dest, content)
        bucket.append(dest)

    return sign_media_urls(await analyse_claim(claim_id, doc_paths, photo_paths))


def _store_upload(claim_id, filename, kind, dest, content):
    with open(dest, "wb") as fh:
        fh.write(content)
    save_file(
        claim_id, filename, kind, dest,
        hashlib.sha256(content).hexdigest(), len(content),
    )


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


def _photo_mime(media_obj, extension, bucket):
    mime = str((media_obj or {}).get("mime_type") or "").split(";")[0].strip().lower()
    if mime not in ("", "application/octet-stream"):
        return mime
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(extension, "image/jpeg")


def _is_pdf(media_obj, data):
    mime = str((media_obj or {}).get("mime_type") or "").split(";")[0].strip().lower()
    extension = _media_extension(media_obj, "")
    return mime == "application/pdf" or extension == ".pdf" or bytes(data[:5]) == b"%PDF-"


def _document_receipt(count, truncated_from=None):
    text = f"Document received ({count} total). Reply DONE when you are done."
    if truncated_from:
        text += (
            f" Only the first {MAX_PDF_PAGES} of its {truncated_from} pages were read."
        )
    return text


async def _store_wa_media(wa_id, message_id, media_obj, bucket, fallback_ext=".jpg"):
    media_obj = media_obj or {}
    session = get_session(wa_id)
    if bucket == "photos" and len(session["photos"]) >= wa.MAX_IMAGES_PER_CLAIM:
        if not session.get("limit_notified"):
            session["limit_notified"] = True
            await send_text(wa_id, wa.IMAGE_LIMIT_REPLY)
        return

    data = await download_media(media_obj)
    if data is None:
        await send_text(wa_id, "I could not download that file. Please send it again.")
        return

    source_sha = hashlib.sha256(data).hexdigest()
    source_bytes = len(data)
    truncated_from = None
    if bucket == "photos":
        prepared = await asyncio.to_thread(prepare_uploaded_image, data)
        if prepared is None:
            await send_text(wa_id, wa.UNREADABLE_IMAGE_REPLY)
            return
        if prepared is data:
            mime = _photo_mime(media_obj, _media_extension(media_obj, fallback_ext), bucket)
        else:
            mime = "image/jpeg"
        items = [(prepared, mime, "photo", 0)]
    else:
        if not _is_pdf(media_obj, data):
            await send_text(wa_id, wa.DOCUMENT_FORMAT_REPLY)
            return
        try:
            pages, page_count = await asyncio.to_thread(
                render_pdf_pages,
                data,
                os.path.basename(str(media_obj.get("filename") or "document.pdf")),
            )
        except Exception as error:
            print(f"[whatsapp] PDF render failed: {type(error).__name__}", flush=True)
            await send_text(wa_id, wa.UNREADABLE_PDF_REPLY)
            return
        if page_count > len(pages):
            truncated_from = page_count
        # Pages are stored, never the PDF: an original over the media cap would
        # otherwise be refused. Its hash and size go on the claim's files row.
        pages = await asyncio.to_thread(lambda: [fit_jpeg(page) for page in pages])
        items = [
            (page, "image/jpeg", "document_page", number)
            for number, page in enumerate(pages, 1)
        ]

    # Blobs are content-addressed and independent of session state, so they
    # can be written off the loop before the session is touched.
    media_ids = await asyncio.to_thread(
        lambda: [media_put(content, mime, kind, None) for content, mime, kind, _ in items]
    )
    if not all(media_ids):
        await send_text(
            wa_id,
            wa.UNREADABLE_IMAGE_REPLY if bucket == "photos" else wa.UNREADABLE_PDF_REPLY,
        )
        return

    # From the inserts to the replies there is no await: appending media and
    # reading the session happen together, so concurrent arrivals cannot
    # interleave, and the unique (wa_id, sha256, page) key absorbs a resend.
    row_ids = [
        wa.add_session_media(wa_id, media_id, kind, source_sha, page, source_bytes)
        for media_id, (_, _, kind, page) in zip(media_ids, items)
    ]
    added = any(row_id is not None for row_id in row_ids)
    session = get_session(wa_id)
    analysing = bool(session.get("analysing")) or wa.is_analysing(wa_id)

    if analysing:
        if not added:
            # The same bytes as rows being analysed: held, then the next claim.
            for media_id, (_, _, kind, page) in zip(media_ids, items):
                wa.defer_media(wa_id, media_id, kind, source_sha, page, source_bytes)
        # One notice per analysis, however many items arrive.
        if wa.take_busy_notice(wa_id):
            await send_text(
                wa_id,
                wa.NEW_CLAIM_WHILE_BUSY if bucket == "photos" else wa.NEW_DOCUMENT_WHILE_BUSY,
            )
        return

    if not added:
        if bucket == "photos":
            await send_text(wa_id, _photo_receipt(len(session["photos"])))
        else:
            await send_text(wa_id, _document_receipt(session["doc_count"]))
        return

    choice_cleared = False
    if session.get("pending_choice") and not analysing:
        session = wa.reset_session(wa_id)
        choice_cleared = True

    if bucket == "photos":
        pending = [
            row["id"] for row in wa._pending_rows(session, list_session_media(wa_id))
            if row["kind"] == "photo"
        ]
        if row_ids[0] in pending and pending.index(row_ids[0]) >= wa.MAX_IMAGES_PER_CLAIM:
            delete_session_media(wa_id, [row_ids[0]])
            if not session.get("limit_notified"):
                session["limit_notified"] = True
                await send_text(wa_id, wa.IMAGE_LIMIT_REPLY)
            return

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
    if bucket == "photos":
        await send_text(wa_id, _photo_receipt(len(session["photos"])))
        return
    await send_text(wa_id, _document_receipt(session["doc_count"], truncated_from))


# A multi-photo send delivers one extra webhook event besides the photos
# (production, 19 Oct: 4 events for 3 photos, twice). Such an event sent
# within this many seconds of media from the same number, before or after
# it, gets no reply. Times are the send times Meta stamps on each event, so
# a slow database cannot push two events of one album apart. Reactions,
# edits and similar events never get a reply.
STRAY_EVENT_WINDOW = 15.0
SILENT_TYPES = {"reaction", "edit", "revoke", "system", "ephemeral", "request_welcome"}
_MEDIA_SEEN = {}


def _sent_at(message):
    """Meta's send time for an event (epoch seconds), else the arrival time."""
    try:
        return float(message.get("timestamp"))
    except (TypeError, ValueError):
        return time.time()


def _note_media(wa_id, at):
    seen = _MEDIA_SEEN.setdefault(wa_id, [])
    seen.append(at)
    del seen[:-20]


def _media_near(wa_id, at):
    return any(abs(seen - at) <= STRAY_EVENT_WINDOW for seen in _MEDIA_SEEN.get(wa_id, []))


async def _answer_unhandled(wa_id, message_type, sent_at):
    """An event that is not a photo, document, text or a known unsupported
    type. Never the greeting to someone with media or an analysis running."""
    if message_type in SILENT_TYPES:
        return
    if not _media_near(wa_id, sent_at):
        # The extra event can arrive before the photos it belongs to.
        await asyncio.sleep(STRAY_EVENT_WINDOW)
    if _media_near(wa_id, sent_at):
        print(f"[whatsapp] ignored a {message_type} event within "
              f"{STRAY_EVENT_WINDOW:.0f}s of media from {wa.who(wa_id)}", flush=True)
        return
    if wa.is_analysing(wa_id):
        return
    session = get_session(wa_id)
    if session["photos"] or session["docs"]:
        await send_text(wa_id, wa.UNHANDLED_WITH_MEDIA_REPLY)
        return
    await send_text(
        wa_id,
        "Please send a photo of the damaged vehicle to start a claim. " + wa.ACCEPTED_MEDIA,
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
        sent_at = _sent_at(message)
        message_id = message.get("id")
        message_type = message.get("type")
        if message_type in ("image", "document"):
            # Before any database work: a stray event waiting on this number
            # must see the media even when the database is slow.
            _note_media(wa_id, sent_at)
        wa.record_inbound(wa_id, message.get("timestamp"))
        codes = ",".join(
            str(error.get("code")) for error in message.get("errors") or []
            if isinstance(error, dict)
        )
        # The type of every inbound message, never the number.
        print(f"[whatsapp] inbound {message_type}{f' errors={codes}' if codes else ''} "
              f"{wa.message_ref(message_id)} from {wa.who(wa_id)}", flush=True)
        wa.sweep_idle_sessions()
        expiry_notice = wa.take_expiry_notice(wa_id)
        if expiry_notice:
            await send_text(wa_id, expiry_notice)

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
            if wa.is_analysing(wa_id):
                # DONE (or anything) mid-analysis: one line, never the greeting.
                await send_text(wa_id, wa.ANALYSIS_BUSY_REPLY)
                return
            session = get_session(wa_id)
            if session["photos"] or session["docs"]:
                await process_session(
                    wa_id,
                    skip_missing=wa.is_skip_command(text),
                )
            elif expiry_notice:
                return
            else:
                await send_text(
                    wa_id,
                    "Send a photo of the damaged vehicle to start a claim. "
                    "You can also attach the claim form, RC or driving licence. "
                    "Reply DONE when you are done.",
                )
        elif message_type in wa.UNSUPPORTED_REPLIES:
            await send_text(wa_id, wa.UNSUPPORTED_REPLIES[message_type])
        else:
            await _answer_unhandled(wa_id, message_type, sent_at)
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


def webhook_signature_ok(raw_body, header):
    """Meta signs every webhook POST with the app secret:
    X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body>.
    None when WHATSAPP_APP_SECRET is not configured."""
    secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    if not secret:
        return None
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    supplied = (header or "").strip().lower()
    return bool(supplied) and hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


@app.post("/webhook")
async def webhook_receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    signed = webhook_signature_ok(raw, request.headers.get("X-Hub-Signature-256"))
    if signed is None:
        # Kept open until the secret is added, so production keeps working.
        print("[whatsapp] WARNING: WHATSAPP_APP_SECRET is not set; "
              "accepting an unsigned webhook", flush=True)
    elif not signed:
        print("[whatsapp] rejected a webhook: X-Hub-Signature-256 missing or wrong",
              flush=True)
        return PlainTextResponse("Forbidden", status_code=403)
    try:
        body = json.loads(raw)
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
            if not await asyncio.to_thread(remember_message, message.get("id")):
                print(f"[whatsapp] skipping already-processed "
                      f"{wa.message_ref(message.get('id'))}", flush=True)
                continue
            background.add_task(handle_wa_message, message)
    except Exception:
        print("[whatsapp] webhook parse failed:", flush=True)
        traceback.print_exc()

    return {"ok": True}


@app.post("/api/whatsapp/subscribe", dependencies=ADMIN_ONLY)
async def whatsapp_subscribe():
    return await subscribe_app()


@app.get("/api/whatsapp/status", dependencies=ADMIN_ONLY)
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
        fh.write("<!doctype html><title>InsureIT</title><h1>InsureIT API is running.</h1>\n")

STATIC_OUTPUT_DIR = os.path.join(BASE_DIR, "static_output")
os.makedirs(STATIC_OUTPUT_DIR, exist_ok=True)
app.mount("/static_output", StaticFiles(directory=STATIC_OUTPUT_DIR), name="static_output")

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
