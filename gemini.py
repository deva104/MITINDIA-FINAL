import io
import json
import mimetypes
import os
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image

load_dotenv()

MODEL = "gemini-3.6-flash"
TEMPERATURE = 0.1

MAX_EDGE = 1568
JPEG_QUALITY = 85

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def _image_part(path):
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    data = Path(path).read_bytes()

    # Anything past MAX_EDGE is downsampled by the model anyway - shrinking it here
    # cuts upload time without losing detail. The file on disk is untouched.
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if max(width, height) > MAX_EDGE:
                scale = MAX_EDGE / max(width, height)
                resized = image.convert("RGB").resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.LANCZOS,
                )
                buffer = io.BytesIO()
                resized.save(buffer, format="JPEG", quality=JPEG_QUALITY)
                shrunk = buffer.getvalue()
                print(f"[gemini] {os.path.basename(path)}: {width}x{height} "
                      f"{len(data) // 1024}KB -> {resized.width}x{resized.height} "
                      f"{len(shrunk) // 1024}KB", flush=True)
                data, mime = shrunk, "image/jpeg"
    except Exception as e:
        print(f"[gemini] resize skipped for {os.path.basename(path)}: {e}", flush=True)

    return types.Part.from_bytes(data=data, mime_type=mime)


def _strip_fences(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _generate(parts, label):
    """Run one multimodal request, returning parsed JSON dict or None."""
    client = _get_client()
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=TEMPERATURE
    )
    delays = (2, 4)
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            resp = client.models.generate_content(model=MODEL, contents=parts, config=cfg)
            elapsed = time.perf_counter() - started
            print(f"[gemini] {label}: {elapsed:.2f}s via {MODEL} "
                  f"(attempt {attempt}/{attempts})", flush=True)
            return json.loads(_strip_fences(resp.text))
        except Exception as e:
            elapsed = time.perf_counter() - started
            print(f"[gemini] {label}: attempt {attempt}/{attempts} failed after "
                  f"{elapsed:.2f}s -> {e}", flush=True)
            if attempt == attempts:
                raise
            time.sleep(delays[attempt - 1])


EXTRACT_PROMPT = """You are reading Indian motor insurance claim documents (claim form,
Registration Certificate, Driving Licence, policy schedule).
Extract these fields. Return JSON: {"fields":[{"name","value","source_doc","confidence"}]}
Field names to extract, exactly these keys:
  insured_name_claim_form, owner_name_rc, holder_name_dl,
  registration_number_claim_form, registration_number_rc,
  chassis_number, engine_number, vehicle_make_model,
  dl_number, dl_expiry_date, policy_number,
  policy_start_date, policy_end_date,
  accident_date, accident_time, accident_location, accident_description
Dates in ISO format YYYY-MM-DD. source_doc = the filename it came from.
confidence = 0.0 to 1.0.
CRITICAL: if a field is not visible in any document, return it with
value null and confidence 0. Never guess or infer a value."""

DAMAGE_PROMPT = """You are a motor insurance damage assessor. For each visible damaged part
return JSON: {"is_vehicle":true,"vehicle_description":"<one short sentence>","photos":[{"source_photo","is_vehicle","make_model","colour","body_type","description"}],"damage":[{"part","damage_type","severity","confidence","reasoning","source_photo"}]}
is_vehicle: false if the image does not show a motor vehicle (car, van, SUV, truck, two-wheeler). If false return empty damage.
For EVERY photo provided, identify the vehicle separately: make and model if legible, dominant
body colour as a simple word (white, black, silver, red, blue, grey), and body type (hatchback,
sedan, SUV, van, truck, two-wheeler). Use null where you cannot tell. Report one entry per photo
even if a photo shows no damage.
source_photo MUST be the filename given for that photo.
part MUST be one of: front_bumper, rear_bumper, bonnet, boot,
  front_left_door, front_right_door, rear_left_door, rear_right_door,
  front_left_fender, front_right_fender, rear_left_quarter,
  rear_right_quarter, headlight_left, headlight_right, taillight_left,
  taillight_right, windshield, rear_windshield, mirror_left,
  mirror_right, grille, roof
damage_type MUST be one of: scratch, dent, crack, shatter, detached, paint_damage
severity MUST be one of: minor, moderate, severe
reasoning = one short sentence on what you observed.
Only report damage you can actually see. Do not infer hidden damage."""


def extract_fields(doc_paths):
    if not doc_paths:
        return []
    try:
        parts = []
        for p in doc_paths:
            parts.append(f"Filename: {os.path.basename(p)}")
            parts.append(_image_part(p))
        parts.append(EXTRACT_PROMPT)
        data = _generate(parts, f"extract_fields({len(doc_paths)} docs)")
        fields = data.get("fields", []) if isinstance(data, dict) else []
        return fields if isinstance(fields, list) else []
    except Exception:
        print("[gemini] extract_fields failed:", flush=True)
        traceback.print_exc()
        return []


VALID_SEVERITIES = ("minor", "moderate", "severe")
CONFIDENCE_WORDS = {
    "very high": 0.9,
    "high": 0.9,
    "medium": 0.6,
    "moderate": 0.6,
    "low": 0.3,
}


def _coerce_confidence(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        word = str(value).strip().lower().replace("_", " ") if value is not None else ""
        number = CONFIDENCE_WORDS.get(word, 0.5)
    if number != number:  # NaN
        return 0.5
    return max(0.0, min(1.0, number))


def _normalise_damage(items):
    """Models drift on these fields, so pin them down before anything downstream reads them."""
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry["confidence"] = _coerce_confidence(entry.get("confidence"))
        severity = str(entry.get("severity") or "").strip().lower()
        entry["severity"] = severity if severity in VALID_SEVERITIES else "moderate"
        for key in ("part", "damage_type"):
            value = entry.get(key)
            if isinstance(value, str):
                entry[key] = value.strip().lower()
        cleaned.append(entry)
    return cleaned


NULL_WORDS = ("null", "none", "n/a", "na", "unknown", "not visible", "not legible")


def _clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() not in NULL_WORDS else None


def _clean_word(value):
    text = _clean_text(value)
    return text.lower() if text else None


def _normalise_photos(items, photo_paths):
    """Exactly one entry per input photo, whatever shape the model returned.

    Matched on source_photo where the model echoed a filename back, falling
    back to input order for anything it renamed or omitted.
    """
    pool = [i for i in (items if isinstance(items, list) else []) if isinstance(i, dict)]
    matched = {}
    for index, path in enumerate(photo_paths):
        name = os.path.basename(path).lower()
        for item in pool:
            if os.path.basename(str(item.get("source_photo") or "")).strip().lower() == name:
                matched[index] = item
                pool.remove(item)
                break

    photos = []
    for index, path in enumerate(photo_paths):
        item = matched.get(index)
        if item is None and pool:
            item = pool.pop(0)
        item = item or {}
        is_vehicle = item.get("is_vehicle")
        photos.append({
            "source_photo": os.path.basename(path),
            "is_vehicle": is_vehicle if isinstance(is_vehicle, bool) else None,
            "make_model": _clean_text(item.get("make_model")),
            "colour": _clean_word(item.get("colour")),
            "body_type": _clean_word(item.get("body_type")),
            "description": _clean_text(item.get("description")) or "",
        })
    return photos


def assess_damage(photo_paths):
    if not photo_paths:
        return {"is_vehicle": None, "vehicle_description": "", "photos": [],
                "damage": [], "error": None}
    try:
        parts = []
        for p in photo_paths:
            parts.append(f"Filename: {os.path.basename(p)}")
            parts.append(_image_part(p))
        parts.append(DAMAGE_PROMPT)
        data = _generate(parts, f"assess_damage({len(photo_paths)} photos)")
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")

        is_vehicle = data.get("is_vehicle")
        if not isinstance(is_vehicle, bool):
            is_vehicle = None
        damage = data.get("damage", [])
        if not isinstance(damage, list) or is_vehicle is False:
            damage = []
        return {
            "is_vehicle": is_vehicle,
            "vehicle_description": str(data.get("vehicle_description") or ""),
            "photos": _normalise_photos(data.get("photos"), photo_paths),
            "damage": _normalise_damage(damage),
            "error": None,
        }
    except Exception as e:
        print("[gemini] assess_damage failed:", flush=True)
        traceback.print_exc()
        # is_vehicle stays None so a failure is never read as a clean result.
        return {"is_vehicle": None, "vehicle_description": "", "photos": [],
                "damage": [], "error": str(e)}
