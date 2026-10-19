import hashlib
import io
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
import imagehash
from dotenv import load_dotenv

load_dotenv()

from db import MAX_MEDIA_BYTES, cache_get, cache_put, current_claim_id, media_put
from PIL import ExifTags, Image, ImageChops, ImageEnhance
from rules import parse_date

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_ENABLED = True
except ImportError:
    HEIF_ENABLED = False
    print("[forensics] pillow_heif unavailable - HEIC/HEIF photos cannot be opened", flush=True)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "static_output"
OUTPUT_DIR.mkdir(exist_ok=True)

ELA_QUALITY = 90
# ELA is informational only. Calibration on 14 genuine photos, 42 WhatsApp-style
# recompressions (<=1600px, q70-80) and 28 edited copies (spliced patch, painted
# rectangle) found no threshold that separates them: the old "max difference >
# 25" rule flagged 6/14 genuine originals and 6/42 genuine recompressions while
# catching 0/28 edits, and the localised score overlapped too (genuine up to
# 8.14, edits up to 6.42). Edits saved at q95 without WhatsApp also overlapped
# genuine photos. These ceilings are the highest genuine values seen; a photo
# above both is noted for an assessor to inspect, never called suspicious.
ELA_GENUINE_MAX_DIFF = 41
ELA_GENUINE_MAX_LOCAL = 8.14
ELA_BLOCK = 16
ELA_INFORMATIONAL_DETAIL = (
    "informational only: recompression (for example by WhatsApp) and editing "
    "produce overlapping error levels"
)
ELA_BOOST = 30.0
ELA_CONTRAST = 2.5
DUPLICATE_DISTANCE = 5
EDITING_TOOLS = ["photoshop", "gimp", "lightroom", "snapseed", "picsart", "canva"]
JPEG_EXTENSIONS = (".jpg", ".jpeg")
NOT_APPLICABLE_DETAIL = "ELA requires JPEG input - PNG has no compression history to analyse"
MISSING_EXIF_DETAIL = (
    "capture time and location unavailable — image was re-encoded in transit "
    "(normal for WhatsApp photos)"
)
SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"
SIGHTENGINE_TIMEOUT = 8.0
AI_CACHE_LABEL = "ai_generated"


def _result(check, verdict, detail, heatmap_url=None):
    return {"check": check, "verdict": verdict, "detail": detail, "heatmap_url": heatmap_url}


def _open_image(path):
    data = Path(path).read_bytes()
    image = Image.open(io.BytesIO(data))
    return data, image, hashlib.md5(data).hexdigest()[:10]


MAX_STORED_EDGE = 2048
MAX_STORED_BYTES = MAX_MEDIA_BYTES
STORED_QUALITY = 90
# Tried in order only when the photo is still over the media cap at quality 90.
FALLBACK_ENCODINGS = ((2048, 80), (2048, 70), (1600, 70), (1280, 60), (1024, 50))


def _encode_within(image, max_bytes, encodings, exif=None):
    data = b""
    for edge, quality in encodings:
        converted = image.convert("RGB")
        converted.thumbnail((edge, edge), Image.LANCZOS)
        buffer = io.BytesIO()
        options = {"format": "JPEG", "quality": quality}
        if exif:
            options["exif"] = exif
        converted.save(buffer, **options)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
    return data


PAGE_ENCODINGS = ((1568, 70), (1568, 55), (1280, 50), (1024, 45))


def fit_jpeg(data, max_bytes=MAX_MEDIA_BYTES):
    """Return JPEG bytes within the media cap: unchanged when they already fit,
    otherwise re-encoded at falling quality. Used for rendered document pages."""
    if len(data) <= max_bytes:
        return data
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return _encode_within(image, max_bytes, PAGE_ENCODINGS)


def prepare_uploaded_image(data):
    """Return stored bytes, or None when the upload is not a readable image.

    Bytes at or below the media cap and edge limit are returned unchanged so a
    normal photo keeps the hash the duplicate gate already stored. Anything
    larger is re-encoded to a JPEG of at most 2048px at quality 90 (lower only
    if that is still over the cap), keeping its EXIF so the capture checks still
    have something to read. Never rejects a readable photo for size.
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if len(data) <= MAX_STORED_BYTES and max(width, height) <= 4096:
                return data
            exif = image.info.get("exif")
            return _encode_within(
                image,
                MAX_STORED_BYTES,
                ((MAX_STORED_EDGE, STORED_QUALITY),) + FALLBACK_ENCODINGS,
                exif=exif if isinstance(exif, (bytes, bytearray)) else None,
            )
    except Exception:
        return None


PROCESSED_EDGE = 1568
PROCESSED_QUALITY = 85


def _jpeg_bytes(image):
    """1568px JPEG, the same size the vision call sends. Never the original upload."""
    converted = image.convert("RGB")
    if max(converted.size) > PROCESSED_EDGE:
        converted.thumbnail((PROCESSED_EDGE, PROCESSED_EDGE), Image.LANCZOS)
    data = b""
    for quality in (PROCESSED_QUALITY, 60, 40):
        buffer = io.BytesIO()
        converted.save(buffer, format="JPEG", quality=quality)
        data = buffer.getvalue()
        if len(data) <= 2 * 1024 * 1024:
            return data
    return data


def _store_processed(image, kind):
    try:
        media_id = media_put(
            _jpeg_bytes(image),
            "image/jpeg",
            kind,
            current_claim_id.get(),
        )
    except Exception as error:
        print(
            f"[forensics] media store failed ({kind}): {type(error).__name__}",
            flush=True,
        )
        return None
    if not media_id:
        return None
    return f"/media/{media_id}"


def _save_original(image, unique_id=None):
    return _store_processed(image, "original")


def build_photo_url_map(photo_paths):
    """Store a processed preview for every submitted photo, including duplicates."""
    urls = {}
    for path in photo_paths or []:
        try:
            _, image, _ = _open_image(path)
            url = _store_processed(image, "photo")
            if url:
                urls[os.path.basename(path).lower()] = url
        except Exception as error:
            print(
                f"[forensics] original preview failed for {os.path.basename(path)}: "
                f"{type(error).__name__}",
                flush=True,
            )
    return urls


def _is_jpeg(path, image):
    if (getattr(image, "format", "") or "").upper() in ("JPEG", "MPO"):
        return True
    return os.path.splitext(path)[1].lower() in JPEG_EXTENSIONS


def _ai_detection_enabled():
    return os.getenv("AI_DETECTION_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def ai_detection_status():
    """Whether the AI-generation check can run. Booleans only, never the keys."""
    return {
        "enabled": _ai_detection_enabled(),
        "keys_configured": bool(
            os.getenv("SIGHTENGINE_API_USER", "").strip()
            and os.getenv("SIGHTENGINE_API_SECRET", "").strip()
        ),
    }


def _ai_score_result(score):
    if score >= 0.85:
        return _result(
            "ai_generated",
            "suspicious",
            f"image shows strong signs of AI generation (score {score:.2f})",
        )
    if score >= 0.50:
        return _result(
            "ai_generated",
            "inconclusive",
            f"AI-generation score is inconclusive (score {score:.2f})",
        )
    return _result(
        "ai_generated",
        "clean",
        f"image shows no strong signs of AI generation (score {score:.2f})",
    )


def check_ai_generated(data, name, mime_type="application/octet-stream"):
    """Classify original bytes; every failure becomes an unavailable signal."""
    if not _ai_detection_enabled():
        return _result(
            "ai_generated",
            "unavailable",
            "AI-generation check disabled by configuration",
        )

    content_hash = hashlib.sha256(data).hexdigest()
    try:
        cached = cache_get(content_hash, label=AI_CACHE_LABEL)
    except Exception as error:
        cached = None
        print(
            f"[forensics] ai_generated: cache read failed ({type(error).__name__})",
            flush=True,
        )
    if cached:
        try:
            score = float(cached["response"]["score"])
            print(
                f"[forensics] ai_generated: cache hit for {name}",
                flush=True,
            )
            result = _ai_score_result(score)
            result["score"] = score
            return result
        except (KeyError, TypeError, ValueError):
            pass

    api_user = os.getenv("SIGHTENGINE_API_USER", "").strip()
    api_secret = os.getenv("SIGHTENGINE_API_SECRET", "").strip()
    if not api_user or not api_secret:
        return _result(
            "ai_generated",
            "unavailable",
            "Sightengine credentials are not configured",
        )

    try:
        response = httpx.post(
            SIGHTENGINE_URL,
            files={"media": (name, data, mime_type)},
            data={
                "models": "genai",
                "api_user": api_user,
                "api_secret": api_secret,
            },
            timeout=SIGHTENGINE_TIMEOUT,
        )
        if response.status_code == 429:
            return _result(
                "ai_generated",
                "unavailable",
                "Sightengine quota exhausted (HTTP 429)",
            )
        if response.status_code >= 400:
            return _result(
                "ai_generated",
                "unavailable",
                f"Sightengine API error (HTTP {response.status_code})",
            )
        try:
            output = response.json()
        except Exception:
            return _result(
                "ai_generated",
                "unavailable",
                "Sightengine returned invalid JSON",
            )
        try:
            score = float(output["type"]["ai_generated"])
        except (KeyError, TypeError, ValueError):
            return _result(
                "ai_generated",
                "unavailable",
                "Sightengine response missing type.ai_generated",
            )
        if not 0.0 <= score <= 1.0:
            return _result(
                "ai_generated",
                "unavailable",
                "Sightengine returned an invalid AI-generation score",
            )

        result = _ai_score_result(score)
        result["score"] = score
        try:
            cache_put(
                content_hash,
                AI_CACHE_LABEL,
                {"score": score},
                "sightengine",
                "genai",
            )
        except Exception as error:
            print(
                f"[forensics] ai_generated: cache write failed "
                f"({type(error).__name__})",
                flush=True,
            )
        return result
    except httpx.TimeoutException:
        return _result(
            "ai_generated",
            "unavailable",
            "Sightengine request timed out after 8 seconds",
        )
    except Exception as error:
        return _result(
            "ai_generated",
            "unavailable",
            f"Sightengine request failed: {type(error).__name__}",
        )


def run_ela(image, quality=ELA_QUALITY, boost=ELA_BOOST):
    original = image.convert("RGB")
    buffer = io.BytesIO()
    original.save(buffer, "JPEG", quality=quality)
    buffer.seek(0)
    resaved = Image.open(buffer)

    diff = ImageChops.difference(original, resaved)
    extrema = diff.getextrema()
    max_diff = max([ex[1] for ex in extrema]) if extrema else 1
    if max_diff == 0:
        max_diff = 1

    # Fixed amplification rather than 255/max_diff: the old scaling shrank as
    # max_diff grew, so a heavier edit rendered a fainter patch.
    ela_image = diff.point(lambda p: min(255, int(p * boost)))
    ela_image = ImageEnhance.Contrast(ela_image).enhance(ELA_CONTRAST)
    return ela_image, max_diff


def ela_local_score(image, quality=ELA_QUALITY):
    """How much the most error-heavy 0.5% of 16px blocks stand out from the
    median block. A splice raises a few blocks; recompression raises all."""
    original = image.convert("RGB")
    buffer = io.BytesIO()
    original.save(buffer, "JPEG", quality=quality)
    buffer.seek(0)
    diff = ImageChops.difference(original, Image.open(buffer)).convert("L")
    width, height = diff.size
    columns, rows = max(1, width // ELA_BLOCK), max(1, height // ELA_BLOCK)
    means = sorted(diff.resize((columns, rows), Image.BOX).getdata())
    median = means[len(means) // 2]
    top = means[-max(1, len(means) // 200):]
    return round((sum(top) / len(top)) / max(median, 0.5), 2)


def _to_float(value):
    try:
        return float(value)
    except Exception:
        try:
            return float(value[0]) / float(value[1])
        except Exception:
            return None


def _dms_to_decimal(dms, ref):
    """EXIF (degrees, minutes, seconds) rationals -> signed decimal degrees."""
    if not dms:
        return None
    try:
        degrees, minutes, seconds = (_to_float(dms[0]), _to_float(dms[1]), _to_float(dms[2]))
    except Exception:
        return None
    if degrees is None or minutes is None or seconds is None:
        return None
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if str(ref).strip().upper().startswith(("S", "W")):
        decimal = -decimal
    return round(decimal, 6)


def extract_exif(image):
    exif_data = {}
    notes = []
    lat = lon = None

    try:
        raw_exif = image.getexif()
    except Exception:
        raw_exif = None

    if not raw_exif:
        return exif_data, [MISSING_EXIF_DETAIL], None, None

    # getexif() exposes only IFD0; the Exif sub-IFD (0x8769) holds capture tags
    # such as DateTimeOriginal, and GPS lives in its own sub-IFD (0x8825).
    merged = {}
    try:
        merged.update(dict(raw_exif))
    except Exception:
        pass
    try:
        merged.update(dict(raw_exif.get_ifd(0x8769)))
    except Exception:
        pass

    gps_raw = None
    try:
        gps_raw = raw_exif.get_ifd(0x8825) or None
    except Exception:
        gps_raw = None

    for tag_id, value in merged.items():
        tag_name = ExifTags.TAGS.get(tag_id, tag_id)
        exif_data[str(tag_name)] = str(value)

    software = exif_data.get("Software", "")
    if any(tool in software.lower() for tool in EDITING_TOOLS):
        notes.append(f"Editing software detected - Software tag shows: '{software}'")

    if gps_raw:
        try:
            gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in dict(gps_raw).items()}
            lat = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
            lon = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
        except Exception:
            lat = lon = None

    if lat is None or lon is None:
        notes.append("No GPS location embedded in image")

    return exif_data, notes, lat, lon


def _field_value(fields, name):
    for field in fields or []:
        if isinstance(field, dict) and field.get("name") == name:
            value = field.get("value")
            return str(value).strip() if value is not None else None
    return None


def _capture_date(exif_data):
    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        value = exif_data.get(key)
        if not value:
            continue
        try:
            return datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S").date()
        except (TypeError, ValueError):
            continue
    return None


def _claim_coordinates(value):
    if not value:
        return None
    matches = re.findall(r"-?\d+(?:\.\d+)?", str(value))
    if len(matches) < 2:
        return None
    try:
        lat, lon = float(matches[0]), float(matches[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def check_duplicate(image, path, conn, claim_id=None):
    """Compare against phashes stored for every other claim, then record ours.

    Rows are excluded by claim id, never by path: temp paths can repeat across
    claims, and a path match must not hide an earlier claim's photo.
    """
    hash_image = image.copy()
    hash_image.thumbnail((256, 256), Image.LANCZOS)
    phash = str(imagehash.phash(hash_image))

    if claim_id:
        rows = conn.execute(
            """
            SELECT filename, path, phash, claim_id FROM files
            WHERE phash IS NOT NULL AND (claim_id IS NULL OR claim_id <> ?)
            """,
            (claim_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT filename, path, phash, claim_id FROM files WHERE phash IS NOT NULL",
        ).fetchall()

    match = None
    for row in rows:
        stored_name, stored_path, stored_hash = row[0], row[1], row[2]
        stored_claim = row[3]
        try:
            distance = imagehash.hex_to_hash(phash) - imagehash.hex_to_hash(stored_hash)
        except Exception:
            continue
        if distance <= DUPLICATE_DISTANCE:
            match = (stored_name, distance, stored_claim)
            break

    if claim_id:
        conn.execute(
            "UPDATE files SET phash = ? WHERE claim_id = ? AND path = ?",
            (phash, claim_id, path),
        )
    else:
        conn.execute("UPDATE files SET phash = ? WHERE path = ?", (phash, path))
    conn.commit()

    return phash, match, len(rows)


def check_duplicates(photo_paths, conn, claim_id=None):
    """Stage 0: perceptual-hash duplicate detection only.

    claim_id defaults to the claim being analysed, so its own rows are skipped.
    """
    if claim_id is None:
        claim_id = current_claim_id.get()
    results = []

    def add(verdict, detail, path):
        # photo_path lets callers map a result back to its photo without
        # relying on list order.
        entry = _result("duplicate", verdict, detail)
        entry["photo_path"] = os.path.abspath(path) if path else None
        results.append(entry)

    try:
        for path in photo_paths or []:
            name = os.path.basename(path)
            try:
                _, image, _ = _open_image(path)
            except Exception as e:
                add("fail", f"{name}: could not open image - {e}", path)
                continue

            try:
                phash, match, compared = check_duplicate(image, path, conn, claim_id)
                if match:
                    stored_name, distance, stored_claim = match
                    # The stored filename is an upload blob name, meaningless to a
                    # user - name the earlier claim instead where we can.
                    if stored_claim:
                        detail = (f"{name}: matches a photo already submitted in claim "
                                  f"{stored_claim} (hash distance {distance})")
                    else:
                        detail = (f"{name}: matches previously uploaded photo "
                                  f"'{stored_name}' (hash distance {distance})")
                    add("suspicious", detail, path)
                else:
                    add("clean", f"{name}: phash {phash}, no match among {compared} stored photo(s)", path)
            except Exception as e:
                add("fail", f"{name}: duplicate check failed - {e}", path)
    except Exception as e:
        add("fail", f"duplicate stage aborted - {e}", None)
    return results


def run_deep_forensics(photo_paths, conn=None, fields=None):
    """ELA + EXIF + AI-generation signal. No duplicate check."""
    results = []
    try:
        for path in photo_paths or []:
            name = os.path.basename(path)
            try:
                data, image, unique_id = _open_image(path)
            except Exception as e:
                for check in ("ela", "exif"):
                    results.append(_result(check, "fail", f"{name}: could not open image - {e}"))
                results.append(_result(
                    "ai_generated",
                    "unavailable",
                    f"{name}: original image bytes could not be read",
                ))
                continue

            # 1. ELA - only meaningful on JPEG, which carries compression history
            try:
                original_url = None
                try:
                    original_url = _save_original(image, unique_id)
                except Exception:
                    original_url = None

                if _is_jpeg(path, image):
                    ela_image, max_diff = run_ela(image)
                    local = ela_local_score(image)
                    detail = (
                        f"{name}: error level max {max_diff}, localised {local} - "
                        f"{ELA_INFORMATIONAL_DETAIL}"
                    )
                    if max_diff > ELA_GENUINE_MAX_DIFF and local > ELA_GENUINE_MAX_LOCAL:
                        detail += (
                            "; both scores are above the range seen in genuine "
                            "test photos, inspect the heatmap"
                        )
                    entry = _result(
                        "ela",
                        "informational",
                        detail,
                        _store_processed(ela_image, "ela"),
                    )
                    entry["score"] = local
                    entry["max_diff"] = max_diff
                else:
                    entry = _result("ela", "not_applicable", NOT_APPLICABLE_DETAIL, None)
                entry["original_url"] = original_url
                entry["source_photo"] = name
                results.append(entry)
            except Exception as e:
                results.append(_result("ela", "fail", f"{name}: ELA failed - {e}"))

            # 2. EXIF
            try:
                exif_data, notes, lat, lon = extract_exif(image)
                if not exif_data:
                    verdict = "unverifiable"
                    detail = MISSING_EXIF_DETAIL
                else:
                    accident_date, _ = parse_date(_field_value(fields, "accident_date"))
                    capture_date = _capture_date(exif_data)
                    if accident_date and capture_date and capture_date != accident_date:
                        notes.append(
                            f"Capture date {capture_date.isoformat()} conflicts with "
                            f"claim accident date {accident_date.isoformat()}"
                        )

                    claim_coordinates = _claim_coordinates(
                        _field_value(fields, "accident_location")
                    )
                    if (lat is not None and lon is not None and claim_coordinates
                            and (abs(lat - claim_coordinates[0]) > 0.01
                                 or abs(lon - claim_coordinates[1]) > 0.01)):
                        notes.append(
                            f"GPS {lat}, {lon} conflicts with claim location "
                            f"{claim_coordinates[0]}, {claim_coordinates[1]}"
                        )

                    suspicious_notes = [
                        note for note in notes
                        if note.startswith("Editing software detected")
                        or "conflicts with claim" in note
                    ]
                    verdict = "suspicious" if suspicious_notes else "clean"
                    segments = list(notes)
                    if lat is not None and lon is not None:
                        segments.append(f"GPS {lat}, {lon}")
                    if not segments:
                        segments = [
                            f"{len(exif_data)} EXIF tags present, no editing software"
                        ]
                    detail = f"{name}: " + "; ".join(segments)
                entry = _result("exif", verdict, detail)
                entry["source_photo"] = name
                entry["lat"] = lat
                entry["lon"] = lon
                results.append(entry)
            except Exception as e:
                results.append(_result("exif", "fail", f"{name}: EXIF check failed - {e}"))

            # 3. AI-generation classifier - the original upload bytes are
            # intentional; resizing or recompression can erase its signal.
            try:
                mime_type = Image.MIME.get(
                    str(getattr(image, "format", "") or "").upper(),
                    "application/octet-stream",
                )
                entry = check_ai_generated(data, name, mime_type)
                entry["source_photo"] = name
                results.append(entry)
            except Exception as error:
                entry = _result(
                    "ai_generated",
                    "unavailable",
                    f"AI-generation check failed: {type(error).__name__}",
                )
                entry["source_photo"] = name
                results.append(entry)
    except Exception as e:
        results.append(_result("ela", "fail", f"forensics aborted - {e}"))
    return results


def run_forensics(photo_paths, conn):
    """Backwards-compatible wrapper: duplicate check followed by ELA + EXIF."""
    return check_duplicates(photo_paths, conn) + run_deep_forensics(photo_paths, conn)
