import io
import os
import traceback
from pathlib import Path

import fitz
from PIL import Image
from vision import generate_json

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    print("[gemini] pillow_heif unavailable - HEIC/HEIF photos cannot be opened", flush=True)

MAX_EDGE = 1568
JPEG_QUALITY = 85
MAX_PDF_PAGES = 8
PDF_DPI = 150


def _jpeg_image_part(data, path, page_number=None):
    # Anything past MAX_EDGE is downsampled by the model anyway - shrinking it here
    # cuts upload time without losing detail. The file on disk is untouched.
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        converted = image.convert("RGB")
        if max(width, height) > MAX_EDGE:
            scale = MAX_EDGE / max(width, height)
            converted = converted.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        converted.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        jpeg_bytes = buffer.getvalue()
        if converted.size != (width, height):
            page_label = f" page {page_number}" if page_number is not None else ""
            print(f"[gemini] {os.path.basename(path)}{page_label}: {width}x{height} "
                  f"{len(data) // 1024}KB -> {converted.width}x{converted.height} "
                  f"{len(jpeg_bytes) // 1024}KB", flush=True)

    return ("image", jpeg_bytes, "image/jpeg")


def _render_pdf(document, name):
    page_count = document.page_count
    if page_count > MAX_PDF_PAGES:
        print(
            f"[gemini] {name}: {page_count} pages, truncating to {MAX_PDF_PAGES}",
            flush=True,
        )
    image_parts = []
    for index in range(min(page_count, MAX_PDF_PAGES)):
        png_bytes = document.load_page(index).get_pixmap(
            dpi=PDF_DPI,
            alpha=False,
        ).tobytes("png")
        image_parts.append(_jpeg_image_part(png_bytes, name, page_number=index + 1))
    return image_parts, page_count


def render_pdf_pages(data, name="document.pdf"):
    """Render PDF bytes to the page JPEGs extract_fields sends (1568px, at most
    MAX_PDF_PAGES). Returns (list of JPEG bytes, total page count). Raises when
    the PDF cannot be opened or has no pages."""
    with fitz.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("PDF is password protected")
        image_parts, page_count = _render_pdf(document, name)
    if not image_parts:
        raise ValueError("PDF has no pages")
    return [part[1] for part in image_parts], page_count


def _image_part(path):
    if Path(path).suffix.lower() == ".pdf":
        with fitz.open(path) as document:
            image_parts, _ = _render_pdf(document, os.path.basename(path))
        return image_parts

    return _jpeg_image_part(Path(path).read_bytes(), path)


def _generate(parts, label):
    return generate_json(parts, label)


EXTRACT_PROMPT = """You are reading Indian motor insurance claim documents (claim form,
Registration Certificate, Driving Licence, policy schedule).
Extract these fields. Return JSON: {"fields":[{"name","value","source_doc","confidence","status"}]}
Field names to extract, exactly these keys:
  insured_name_claim_form, owner_name_rc, holder_name_dl,
  registration_number_claim_form, registration_number_rc,
  chassis_number, engine_number, vehicle_make_model,
  dl_number, dl_expiry_date, policy_number,
  policy_start_date, policy_end_date,
  accident_date, accident_time, accident_location, accident_description
Dates in ISO format YYYY-MM-DD. source_doc = the filename it came from.
confidence = 0.0 to 1.0.
If a field's label appears on the document but the value is blank, illegible or cut off,
return status 'unreadable'. If the field does not appear on this document type at all,
return 'not_present'. Only use 'found' when you read an actual value.
CRITICAL: if a field is not visible in any document, return it with
value null and confidence 0. Never guess or infer a value."""

DAMAGE_PROMPT = """You are a motor insurance damage assessor. For each visible damaged part
return JSON: {"is_vehicle":true,"vehicle_description":"<one short sentence>","photos":[{"source_photo","is_vehicle","vehicle_group","make_model","colour","body_type","description"}],"damage":[{"part","damage_type","severity","confidence","reasoning","source_photo"}]}
Return ONE top-level JSON object in this shape, never an array or one object per vehicle.
Put every vehicle group in the same photos array and every damage item in the same damage array.
is_vehicle: false if the image does not show a motor vehicle (car, van, SUV, truck, two-wheeler). If false return empty damage.
For EVERY photo provided, identify the vehicle separately: make and model if legible, dominant
body colour as a simple word (white, black, silver, red, blue, grey), and body type (hatchback,
sedan, SUV, van, truck, two-wheeler). Use null where you cannot tell. Report one entry per photo
even if a photo shows no damage.
Group the photos by vehicle. Photos of the SAME physical vehicle share a vehicle_group number,
starting at 0. A different vehicle gets the next number. Judge by colour, body type, model,
number plate and visible damage together — the same car photographed from two angles is ONE
group. Photos that are not vehicles get vehicle_group null.
source_photo MUST be the filename given for that photo.
part MUST be one of: front_bumper, rear_bumper, bonnet, boot,
  front_left_door, front_right_door, rear_left_door, rear_right_door,
  front_left_fender, front_right_fender, rear_left_quarter,
  rear_right_quarter, headlight_left, headlight_right, taillight_left,
  taillight_right, windshield, rear_windshield, mirror_left,
  mirror_right, grille, roof
Use EXACTLY these part names, never synonyms.
damage_type MUST be one of: scratch, dent, crack, shatter, detached, paint_damage
severity MUST be one of: minor, moderate, severe
reasoning = one short sentence on what you observed.
Only report damage you can actually see. Do not infer hidden damage."""

CLASSIFY_PROMPT = """Classify this one uploaded image for an Indian motor insurance claim.
Return JSON exactly as:
{"doc_type":"vehicle_photo|rc|driving_licence|claim_form|policy|other_document|other","confidence":0.0,"number_plate":null}
Use vehicle_photo for an exterior or interior motor-vehicle photograph, including damage photos.
Use rc only for a Registration Certificate, driving_licence only for a driving licence,
claim_form only for a motor claim form, and policy only for an insurance policy document.
Use other_document for another readable document and other for everything else.
number_plate: read it only from a vehicle_photo when it is clearly legible. Otherwise null.
Never guess a number plate."""

CLASSIFICATION_TYPES = {
    "vehicle_photo",
    "rc",
    "driving_licence",
    "claim_form",
    "policy",
    "other_document",
    "other",
}


def classify_image(path):
    if Path(path).suffix.lower() == ".pdf":
        return {
            "doc_type": "other_document",
            "confidence": 1.0,
            "number_plate": None,
        }
    try:
        data = _generate(
            [_image_part(path), ("text", CLASSIFY_PROMPT)],
            "classify_image",
        )
        if not isinstance(data, dict):
            raise ValueError("classification response is not an object")
        doc_type = str(data.get("doc_type") or "").strip().lower()
        if doc_type not in CLASSIFICATION_TYPES:
            doc_type = "other"
        confidence = _coerce_confidence(data.get("confidence"))
        number_plate = _clean_text(data.get("number_plate"))
        if doc_type != "vehicle_photo":
            number_plate = None
        return {
            "doc_type": doc_type,
            "confidence": confidence,
            "number_plate": number_plate,
        }
    except Exception:
        print(
            f"[gemini] classify_image failed for {os.path.basename(path)}:",
            flush=True,
        )
        traceback.print_exc()
        # Low confidence is deliberately routed into the damage pipeline.
        return {
            "doc_type": "other",
            "confidence": 0.0,
            "number_plate": None,
        }


def extract_fields(doc_paths):
    if not doc_paths:
        return []
    try:
        parts = []
        for p in doc_paths:
            image_parts = _image_part(p)
            if isinstance(image_parts, list):
                with fitz.open(p) as document:
                    page_count = document.page_count
                for index, image_part in enumerate(image_parts, 1):
                    parts.append((
                        "text",
                        (
                            f"Filename: {os.path.basename(p)} "
                            f"(page {index} of {page_count})"
                        ),
                    ))
                    parts.append(image_part)
            else:
                parts.append(("text", f"Filename: {os.path.basename(p)}"))
                parts.append(image_parts)
        parts.append(("text", EXTRACT_PROMPT))
        data = _generate(parts, f"extract_fields({len(doc_paths)} docs)")
        fields = data.get("fields", []) if isinstance(data, dict) else []
        if not isinstance(fields, list):
            return []
        normalised = []
        for field in fields:
            if not isinstance(field, dict):
                continue
            entry = dict(field)
            value = entry.get("value")
            has_value = value is not None and str(value).strip().lower() not in NULL_WORDS
            status = str(entry.get("status") or "").strip().lower()
            if has_value:
                entry["status"] = "found"
            elif status in ("not_present", "unreadable"):
                entry["status"] = status
            else:
                # If a provider omits status, do not claim the label was absent.
                entry["status"] = "unreadable"
            normalised.append(entry)
        return normalised
    except Exception:
        print("[gemini] extract_fields failed:", flush=True)
        traceback.print_exc()
        return []


VALID_SEVERITIES = ("minor", "moderate", "severe")
VALID_DAMAGE_PARTS = {
    "front_bumper", "rear_bumper", "bonnet", "boot",
    "front_left_door", "front_right_door", "rear_left_door", "rear_right_door",
    "front_left_fender", "front_right_fender", "rear_left_quarter",
    "rear_right_quarter", "headlight_left", "headlight_right", "taillight_left",
    "taillight_right", "windshield", "rear_windshield", "mirror_left",
    "mirror_right", "grille", "roof",
}
PART_SYNONYMS = {
    "hood": "bonnet",
    "trunk": "boot",
    "tailgate": "boot",
    "headlight": "headlight_left",
    "headlamp": "headlight_left",
    "taillight": "taillight_left",
    "tail_lamp": "taillight_left",
    "rear_light": "taillight_left",
    "wing": "mirror_left",
    "wing_mirror": "mirror_left",
    "side_mirror": "mirror_left",
    "front_fender": "front_left_fender",
    "windscreen": "windshield",
    "bumper": "front_bumper",
}
CONFIDENCE_WORDS = {
    "very high": 0.9,
    "high": 0.9,
    "medium": 0.6,
    "moderate": 0.6,
    "low": 0.3,
}


def _normalise_part(value):
    if not isinstance(value, str):
        return value
    name = value.strip().lower().replace(" ", "_")
    if name in VALID_DAMAGE_PARTS:
        return name
    if name in PART_SYNONYMS:
        return PART_SYNONYMS[name]
    if name.endswith("s"):
        singular = name[:-1]
        if singular in VALID_DAMAGE_PARTS:
            return singular
        if singular in PART_SYNONYMS:
            return PART_SYNONYMS[singular]
    return name


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
        entry["part"] = _normalise_part(entry.get("part"))
        source_photo = entry.get("source_photo")
        entry["source_photo"] = (
            os.path.basename(source_photo.strip())
            if isinstance(source_photo, str) and source_photo.strip()
            else None
        )
        cleaned.append(entry)
    return cleaned


def assign_damage_ids(photos, damage):
    """Assign deterministic IDs, suffixing repeated group/part pairs in input order."""
    photo_groups = {
        os.path.basename(str(photo.get("source_photo") or "")).lower(): (
            photo.get("vehicle_group")
            if isinstance(photo.get("vehicle_group"), int)
            and not isinstance(photo.get("vehicle_group"), bool)
            else 0
        )
        for photo in (photos or [])
        if isinstance(photo, dict)
    }
    counts = {}
    for item in damage or []:
        if not isinstance(item, dict):
            continue
        source = os.path.basename(str(item.get("source_photo") or "")).lower()
        group = photo_groups.get(source, 0)
        part = str(item.get("part") or "unknown_part")
        base = f"{group}-{part}"
        counts[base] = counts.get(base, 0) + 1
        item["damage_id"] = (
            base if counts[base] == 1 else f"{base}-{counts[base]}"
        )
    return damage


NULL_WORDS = ("null", "none", "n/a", "na", "unknown", "not visible", "not legible")


def _clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() not in NULL_WORDS else None


def _clean_word(value):
    text = _clean_text(value)
    return text.lower() if text else None


def _clean_vehicle_group(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        group = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return group if group >= 0 else None


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
        is_vehicle = is_vehicle if isinstance(is_vehicle, bool) else None
        photos.append({
            "source_photo": os.path.basename(path),
            "is_vehicle": is_vehicle,
            "vehicle_group": (
                _clean_vehicle_group(item.get("vehicle_group"))
                if is_vehicle is not False
                else None
            ),
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
            parts.append(("text", f"Filename: {os.path.basename(p)}"))
            parts.append(_image_part(p))
        parts.append(("text", DAMAGE_PROMPT))
        data = _generate(parts, f"assess_damage({len(photo_paths)} photos)")
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")

        is_vehicle = data.get("is_vehicle")
        if not isinstance(is_vehicle, bool):
            is_vehicle = None
        photos = _normalise_photos(data.get("photos"), photo_paths)
        damage = data.get("damage", [])
        if (not isinstance(damage, list)
                or (is_vehicle is False
                    and not any(photo.get("is_vehicle") is True for photo in photos))):
            damage = []
        normalised_damage = _normalise_damage(damage)
        assign_damage_ids(photos, normalised_damage)
        return {
            "is_vehicle": is_vehicle,
            "vehicle_description": str(data.get("vehicle_description") or ""),
            "photos": photos,
            "damage": normalised_damage,
            "error": None,
        }
    except Exception as e:
        print("[gemini] assess_damage failed:", flush=True)
        traceback.print_exc()
        # is_vehicle stays None so a failure is never read as a clean result.
        return {"is_vehicle": None, "vehicle_description": "", "photos": [],
                "damage": [], "error": str(e)}
