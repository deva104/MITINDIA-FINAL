"""Reset an InsureIT database and load the demo claims through the real pipeline.

    python seed_demo.py --database-url URL --admin-token TOKEN \
        [--base-url https://mitindia-final.onrender.com] [--ai-image PATH] --yes

Everything runs through main.analyse_claim with the real vision providers,
duplicate gate, forensics and rules, so each claim shows what the system
actually decides. Nothing is forced: if a scenario does not land where it is
meant to, the script says so instead of editing the result.

Safety:
- The target database is only ever --database-url, never .env's DATABASE_URL.
- The admin token must be valid: with --base-url it is checked against that
  deployment (an admin endpoint must accept it); otherwise it must equal
  ADMIN_TOKEN in the environment.
- It wipes the target's claims, files, media and sessions, so --yes is required.
Re-runnable: every run starts from a reset.
"""
import argparse
import asyncio
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PHOTOS = ROOT / "test data" / "Photos"
FIXTURES = ROOT / "tests" / "fixtures"

# The synthetic documents for the clean claim. Values are consistent with each
# other so the cross-document rules have nothing to object to.
CLEAN_OWNER = "Priya Ramesh Iyer"
CLEAN_REGISTRATION = "MH-12-QZ-4821"
CLEAN_DOCUMENTS = {
    "rc.jpg": [
        "REGISTRATION CERTIFICATE (SYNTHETIC DEMO)",
        f"Registration No: {CLEAN_REGISTRATION}",
        f"Owner Name: {CLEAN_OWNER}",
        "Vehicle Make/Model: Honda City VX",
        "Chassis No: MA3EJKD1S00998877",
        "Engine No: L15Z9876543",
    ],
    "dl.jpg": [
        "DRIVING LICENCE (SYNTHETIC DEMO)",
        "DL No: MH12-20190123456",
        f"Name: {CLEAN_OWNER}",
        "Valid Till: 14-08-2034",
    ],
    "claim-form.jpg": [
        "MOTOR CLAIM FORM (SYNTHETIC DEMO)",
        f"Insured Name: {CLEAN_OWNER}",
        f"Registration No: {CLEAN_REGISTRATION}",
        "Date of Accident: 12-10-2026",
        "Time: 18:40",
        "Place: FC Road, Pune",
        "Description: Reversing in a parking lot, the rear bumper hit a pillar.",
    ],
}


def fail(message):
    print(f"seed_demo: {message}", file=sys.stderr)
    sys.exit(1)


def host(url):
    return (urlparse(url).hostname or "").lower()


def check_token(token, base_url):
    if not token:
        fail("an admin token is required (--admin-token or ADMIN_TOKEN)")
    if base_url:
        import httpx

        response = httpx.get(
            base_url.rstrip("/") + "/api/whatsapp/status",
            headers={"X-Admin-Token": token}, timeout=120,
        )
        if response.status_code != 200:
            fail(f"{base_url} rejected the admin token (HTTP {response.status_code})")
        return
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected or token != expected:
        fail("the admin token does not match ADMIN_TOKEN")


def document_image(lines):
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    image = Image.new("RGB", (1400, 120 + 70 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 60 + index * 70), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN", ""))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--ai-image", default="", help="an AI-generated damage photo")
    parser.add_argument("--yes", action="store_true", help="confirm the reset")
    args = parser.parse_args()

    if not args.yes:
        fail(f"this wipes every claim on {host(args.database_url) or 'that database'}; "
             "pass --yes to proceed")
    check_token(args.admin_token.strip(), args.base_url.strip())

    # The backend is chosen once, at db import: set it before anything imports
    # db. sqlite:///path is accepted for a local dry run.
    target = args.database_url.strip()
    sqlite_path = target[len("sqlite:///"):] if target.startswith("sqlite:///") else None
    os.environ["DATABASE_URL"] = "" if sqlite_path else target
    sys.path.insert(0, str(ROOT))
    import db

    if sqlite_path:
        db.DB_PATH = sqlite_path
    elif db.BACKEND != "postgres":
        fail("the database URL was not accepted as Postgres")
    import main
    import vision

    # A batch job can afford to wait: when every key of every provider is
    # rate-limited, wait for the soonest key (at most 60 s at a time) and
    # retry. The live pipeline never waits.
    vision.wait_for_keys(True)
    work = Path(tempfile.mkdtemp(prefix="claimpilot-seed-"))
    main.UPLOAD_DIR = str(work / "uploads")
    db.init_db()
    main.demo_reset()
    print(f"reset {sqlite_path or host(target)}")

    results = []

    def stage(claim_id, sources):
        """Copy inputs into uploads/ (analyse_claim deletes them afterwards)
        and register their files rows, as /api/analyze does."""
        import hashlib

        folder = Path(main.UPLOAD_DIR) / claim_id
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, data, kind in sources:
            path = folder / name
            path.write_bytes(data)
            db.save_file(claim_id, name, kind, str(path),
                         hashlib.sha256(data).hexdigest(), len(data))
            paths.append(str(path))
        return paths

    def run(label, claim_id, photos=(), docs=(), expect=None):
        photo_paths = stage(claim_id, [(name, data, "photo") for name, data in photos])
        doc_paths = stage(claim_id, [(name, data, "document") for name, data in docs])
        payload = asyncio.run(main.analyse_claim(claim_id, doc_paths, photo_paths))
        landed = payload.get("decision")
        status = f"{payload.get('status')}/{payload.get('stopped_at') or '-'}"
        note = "" if not expect or expect(payload) else "  <-- did not land as intended"
        results.append((label, claim_id, status, landed, note))
        print(f"{claim_id}  {label}: {status}, {landed}{note}", flush=True)
        return payload

    read = lambda path: Path(path).read_bytes()  # noqa: E731

    # 1. A clean single-car claim with consistent synthetic documents.
    run(
        "clean single car", "CLM-DEMO-CLEAN",
        photos=[("rear.jpg", read(PHOTOS / "CAR B PIC1.jpeg")),
                ("rear-close.jpg", read(PHOTOS / "CAR B PIC2.jpeg"))],
        docs=[(name, document_image(lines)) for name, lines in CLEAN_DOCUMENTS.items()],
        expect=lambda p: p.get("decision") == "auto_assess",
    )
    # 2. Two different vehicles in one claim.
    run(
        "two vehicles", "CLM-DEMO-TWOCARS",
        photos=[("car-one.jpg", read(PHOTOS / "CAR A PIC1.jpeg")),
                ("car-two.jpg", read(PHOTOS / "CAR C FRONT.jpeg"))],
        expect=lambda p: (p.get("claimable_count") or 0) > 1,
    )
    # 3. The clean claim's photo submitted again.
    run(
        "duplicate photo", "CLM-DEMO-DUPLICATE",
        photos=[("resubmitted.jpg", read(PHOTOS / "CAR B PIC1.jpeg"))],
        expect=lambda p: p.get("stopped_at") == "duplicate",
    )
    # 4. The motor bundle: the licence expired before the accident.
    bundle = run(
        "motor bundle, expired licence", "CLM-DEMO-BUNDLE",
        photos=[("front.jpg", read(FIXTURES / "claimed_panel.jpeg"))],
        docs=[("motor-bundle.pdf", read(FIXTURES / "Synthetic Motor Claim Test Bundle.pdf"))],
        expect=lambda p: any(f.get("rule") == "dl_expired_at_loss" for f in p.get("flags") or []),
    )
    del bundle
    # 5. An assessor override in the audit trail. The finding overridden is the
    # least certain one; if the model was confident about everything, that is
    # reported and the override revises a severity instead. Nothing is faked.
    low = run(
        "assessor override", "CLM-DEMO-OVERRIDE",
        photos=[("side.jpg", read(PHOTOS / "CAR D TOYATA(SAME color n look).jpeg")),
                ("low-light.jpg", read(PHOTOS / "Low light car pic.jpeg"))],
    )
    findings = [item for item in low.get("damage") or [] if item.get("damage_id")]
    uncertain = [item for item in findings
                 if item.get("confidence") is None or item["confidence"] < 0.6]
    if uncertain:
        target, action, value = uncertain[0], "confirm_damage", None
        reason = "Assessor inspected the photo: the damage is visible on the panel."
    elif findings:
        target, action = findings[0], "set_severity"
        value = "moderate" if target.get("severity") != "moderate" else "severe"
        reason = "Assessor inspection: severity revised after a closer look at the panel."
        print("  note: every finding was above 0.6 confidence; "
              "overriding a severity instead of confirming a low-confidence finding")
    else:
        target = None
        print("  note: no damage finding to override")
    if target is not None:
        request = main.OverrideRequest(
            actor="demo-assessor", action=action, target=target["damage_id"],
            value=value, reason=reason,
        )
        asyncio.run(main.override_claim("CLM-DEMO-OVERRIDE", request))
        print(f"  override recorded: {action} on {target['damage_id']}")
    # 6. An AI-generated photo, only when the AI-generation check can run.
    keys = main.ai_detection_status()
    if args.ai_image and keys.get("enabled") and keys.get("keys_configured"):
        run(
            "AI-generated photo", "CLM-DEMO-AIGEN",
            photos=[("ai-damage.jpg", read(args.ai_image))],
            expect=lambda p: any(f.get("rule") == "ai_generated_photo"
                                 for f in p.get("flags") or []),
        )
    else:
        print("  skipped AI-generated photo: needs --ai-image and Sightengine keys")

    shutil.rmtree(work, ignore_errors=True)
    print("\nclaim id               scenario                              status            decision")
    for label, claim_id, status, decision, note in results:
        print(f"{claim_id:22} {label:37} {status:17} {decision}{note}")


if __name__ == "__main__":
    main_cli()
