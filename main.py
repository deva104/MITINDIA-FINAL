import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from db import get_conn, init_db, list_claims, save_claim, save_file
from gemini import assess_damage, extract_fields
from rules import build_estimate, run_rules
from vision import reset_cache_tracking, stage_was_cached

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
    from forensics import check_duplicates, run_deep_forensics
except ImportError:
    def check_duplicates(photo_paths, conn):
        return []

    def run_deep_forensics(photo_paths, conn=None, fields=None):
        return []

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
STATIC_DIR = os.path.join(BASE_DIR, "static")


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
    return {"ok": True}


@app.get("/api/claims")
def claims():
    return list_claims()


@app.get("/api/claims/{claim_id}")
def claim_detail(claim_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT payload_json FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="claim not found")
    import json
    return json.loads(row["payload_json"])


@app.post("/api/demo/reset")
def demo_reset():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM claims")
        conn.commit()
    finally:
        conn.close()
    print("[demo] database cleared", flush=True)
    return {"cleared": True}


def build_vehicle_groups(photos, damage, excluded):
    """Split photos, attributed damage, and estimates by model-assigned vehicle."""
    photo_entries = [photo for photo in (photos or []) if isinstance(photo, dict)]
    damage_items = [item for item in (damage or []) if isinstance(item, dict)]
    excluded_names = [os.path.basename(str(name)) for name in (excluded or []) if name]
    excluded_lookup = {name.lower() for name in excluded_names}

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
            label = " ".join(
                "SUV" if word.lower() == "suv" else word.title()
                for word in " ".join(label_parts).split()
            )
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
            "excluded_photos": group_excluded,
            "damage": group_damage,
            "estimate": build_estimate(group_damage),
            "status": status,
        })

    return vehicles


async def analyse_claim(claim_id, doc_paths, photo_paths):
    """Staged pipeline shared by the HTTP upload route and the WhatsApp webhook."""
    started = time.perf_counter()

    timings = []
    excluded_photos = []

    def stage_done(name, since, cached=False):
        elapsed = round(time.perf_counter() - since, 2)
        timings.append({"stage": name, "seconds": elapsed, "cached": bool(cached)})
        print(f"[analyze] {claim_id} {name}: {elapsed:.2f}s", flush=True)

    def make_payload(status, stopped_at, fields=None, damage=None, forensics=None,
                     flags=None, estimate=None, vehicle_description="", photos=None,
                     vehicles=None, claimable_count=0):
        return {
            "claim_id": claim_id,
            "created_at": datetime.now().isoformat(),
            "status": status,
            "stopped_at": stopped_at,
            "vehicle_description": vehicle_description,
            "photos": photos or [],
            "timings": list(timings),
            "excluded_photos": list(excluded_photos),
            "fields": fields or [],
            "damage": damage or [],
            "forensics": forensics or [],
            "flags": flags or [],
            "estimate": estimate if estimate is not None else build_estimate([]),
            "vehicles": vehicles or [],
            "claimable_count": claimable_count,
        }

    def finish(payload):
        save_claim(claim_id, payload)
        print(f"[analyze] {claim_id} status={payload['status']} "
              f"stopped_at={payload['stopped_at']} total {time.perf_counter() - started:.2f}s", flush=True)
        return payload

    conn = get_conn()
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

        # Only a bundle with nothing left to assess is rejected outright. A claim
        # with at least one fresh photo is still worth analysing.
        if dup_paths and not clean_paths:
            vehicles = build_vehicle_groups([], [], excluded_photos)
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

        # Stage 1 - Gemini extraction and damage assessment
        mark = time.perf_counter()
        reset_cache_tracking()
        fields = extract_fields(doc_paths) if doc_paths else []
        result = assess_damage(clean_paths)
        stage_done("stage1_gemini", mark, cached=stage_was_cached())

        description = result.get("vehicle_description") or ""
        photos = result.get("photos") or []
        raw_damage = result.get("damage") or []
        vehicles = build_vehicle_groups(photos, raw_damage, excluded_photos)
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
        deep = run_deep_forensics(clean_paths, conn, fields)
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
                                         claimable_count=claimable_count)
        stage_done("stage3_rules", mark)

        return finish(make_payload("analysed", None, fields=fields, damage=damage,
                                   forensics=dup + deep, flags=flags, estimate=estimate,
                                   vehicle_description=description, photos=photos,
                                   vehicles=vehicles, claimable_count=claimable_count))
    finally:
        conn.close()


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


async def _store_wa_media(wa_id, message_id, media_obj, bucket, fallback_ext=".jpg"):
    data = await download_media(media_obj)
    if not data:
        await send_text(wa_id, "I could not download that file. Please send it again.")
        return

    folder = os.path.join(UPLOAD_DIR, f"wa_{wa_id}")
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(str(media_obj.get("filename") or ""))[1] or fallback_ext
    dest = os.path.join(folder, f"{message_id}{ext}")
    with open(dest, "wb") as fh:
        fh.write(data)

    session = get_session(wa_id)
    session[bucket].append(dest)
    session["updated"] = time.time()

    if bucket == "photos":
        await send_text(
            wa_id,
            f"Photo received ({len(session['photos'])} total). Send any message when you are done.",
        )
    else:
        await send_text(
            wa_id,
            f"Document received ({len(session['docs'])} total). Send any message when you are done.",
        )


async def handle_wa_message(message):
    """Runs in the background so the webhook can answer immediately."""
    try:
        wa_id = message.get("from")
        if not wa_id:
            return
        message_id = message.get("id")
        message_type = message.get("type")

        if message_type == "image":
            await _store_wa_media(wa_id, message_id, message.get("image") or {}, "photos")
        elif message_type == "document":
            document = message.get("document") or {}
            if str(document.get("mime_type") or "").startswith("image/"):
                await _store_wa_media(wa_id, message_id, document, "photos")
            else:
                await _store_wa_media(wa_id, message_id, document, "docs", fallback_ext=".bin")
        elif message_type == "text":
            text = (message.get("text") or {}).get("body") or ""
            if await wa.handle_pending_choice(wa_id, text):
                return
            session = get_session(wa_id)
            if session["photos"] or session["docs"]:
                await process_session(wa_id)
            else:
                await send_text(
                    wa_id,
                    "Send a photo of the damaged vehicle to start a claim. "
                    "You can also attach the claim form, RC or driving licence. "
                    "Send any message when you are done and I will analyse it.",
                )
        else:
            await send_text(wa_id, "Please send a photo of the damaged vehicle to start a claim.")
    except Exception:
        print("[whatsapp] handle_wa_message failed:", flush=True)
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
