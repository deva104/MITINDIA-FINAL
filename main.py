import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from db import get_conn, init_db, list_claims, save_claim, save_file
from gemini import assess_damage, extract_fields
from rules import build_estimate, run_rules

try:
    from forensics import check_duplicates, run_deep_forensics
except ImportError:
    def check_duplicates(photo_paths, conn):
        return []

    def run_deep_forensics(photo_paths, conn=None):
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

    started = time.perf_counter()

    timings = []

    def stage_done(name, since):
        elapsed = round(time.perf_counter() - since, 2)
        timings.append({"stage": name, "seconds": elapsed})
        print(f"[analyze] {claim_id} {name}: {elapsed:.2f}s", flush=True)

    def make_payload(status, stopped_at, fields=None, damage=None, forensics=None,
                     flags=None, estimate=None, vehicle_description=""):
        return {
            "claim_id": claim_id,
            "created_at": datetime.now().isoformat(),
            "status": status,
            "stopped_at": stopped_at,
            "vehicle_description": vehicle_description,
            "timings": list(timings),
            "fields": fields or [],
            "damage": damage or [],
            "forensics": forensics or [],
            "flags": flags or [],
            "estimate": estimate if estimate is not None else build_estimate([]),
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
        if any(d.get("verdict") == "suspicious" for d in dup):
            return finish(make_payload("rejected", "duplicate", forensics=dup))

        # Stage 1 - Gemini extraction and damage assessment
        mark = time.perf_counter()
        fields = extract_fields(doc_paths) if doc_paths else []
        result = assess_damage(photo_paths)
        stage_done("stage1_gemini", mark)

        description = result.get("vehicle_description") or ""
        if result.get("error"):
            return finish(make_payload("error", "ai_unavailable", fields=fields, forensics=dup,
                                       vehicle_description=description))
        if result.get("is_vehicle") is False:
            return finish(make_payload("rejected", "not_a_vehicle", fields=fields, forensics=dup,
                                       vehicle_description=description))
        damage = result.get("damage") or []

        # Stage 2 - ELA + EXIF
        mark = time.perf_counter()
        deep = run_deep_forensics(photo_paths, conn)
        stage_done("stage2_deep_forensics", mark)

        # Stage 3 - rules and estimate
        mark = time.perf_counter()
        flags = run_rules(fields, damage, photo_count=len(photo_paths),
                          is_vehicle=result.get("is_vehicle"))
        estimate = build_estimate(damage)
        stage_done("stage3_rules", mark)

        return finish(make_payload("analysed", None, fields=fields, damage=damage,
                                   forensics=dup + deep, flags=flags, estimate=estimate,
                                   vehicle_description=description))
    finally:
        conn.close()


os.makedirs(STATIC_DIR, exist_ok=True)
_index = os.path.join(STATIC_DIR, "index.html")
if not os.path.exists(_index):
    with open(_index, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><title>ClaimPilot</title><h1>ClaimPilot API is running.</h1>\n")

STATIC_OUTPUT_DIR = os.path.join(BASE_DIR, "static_output")
os.makedirs(STATIC_OUTPUT_DIR, exist_ok=True)
app.mount("/static_output", StaticFiles(directory=STATIC_OUTPUT_DIR), name="static_output")

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
