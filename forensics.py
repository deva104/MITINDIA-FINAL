import hashlib
import io
import os
from pathlib import Path

import imagehash
from PIL import ExifTags, Image, ImageChops, ImageEnhance

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
ELA_HIGH = 60
ELA_MEDIUM = 25
ELA_BOOST = 30.0
ELA_CONTRAST = 2.5
DUPLICATE_DISTANCE = 5
EDITING_TOOLS = ["photoshop", "gimp", "lightroom", "snapseed", "picsart", "canva"]
JPEG_EXTENSIONS = (".jpg", ".jpeg")
NOT_APPLICABLE_DETAIL = "ELA requires JPEG input - PNG has no compression history to analyse"


def _result(check, verdict, detail, heatmap_url=None):
    return {"check": check, "verdict": verdict, "detail": detail, "heatmap_url": heatmap_url}


def _open_image(path):
    data = Path(path).read_bytes()
    image = Image.open(io.BytesIO(data))
    return data, image, hashlib.md5(data).hexdigest()[:10]


def _is_jpeg(path, image):
    if (getattr(image, "format", "") or "").upper() in ("JPEG", "MPO"):
        return True
    return os.path.splitext(path)[1].lower() in JPEG_EXTENSIONS


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
        return exif_data, ["No EXIF metadata found - could indicate a screenshot, re-saved, or scraped image"], None, None

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


def check_duplicate(image, path, conn):
    """Compare against phashes already stored in our files table, then record ours."""
    phash = str(imagehash.phash(image))

    rows = conn.execute(
        "SELECT filename, path, phash, claim_id FROM files WHERE phash IS NOT NULL AND path != ?",
        (path,),
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

    conn.execute("UPDATE files SET phash = ? WHERE path = ?", (phash, path))
    conn.commit()

    return phash, match, len(rows)


def check_duplicates(photo_paths, conn):
    """Stage 0: perceptual-hash duplicate detection only."""
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
                phash, match, compared = check_duplicate(image, path, conn)
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


def run_deep_forensics(photo_paths, conn=None):
    """ELA + EXIF. No duplicate check."""
    results = []
    try:
        for path in photo_paths or []:
            name = os.path.basename(path)
            try:
                data, image, unique_id = _open_image(path)
            except Exception as e:
                for check in ("ela", "exif"):
                    results.append(_result(check, "fail", f"{name}: could not open image - {e}"))
                continue

            # 1. ELA - only meaningful on JPEG, which carries compression history
            try:
                original_url = None
                try:
                    image.convert("RGB").save(OUTPUT_DIR / f"{unique_id}_original.jpg", "JPEG")
                    original_url = f"/static_output/{unique_id}_original.jpg"
                except Exception:
                    original_url = None

                if _is_jpeg(path, image):
                    ela_image, max_diff = run_ela(image)
                    ela_image.save(OUTPUT_DIR / f"{unique_id}_ela.jpg", "JPEG")
                    level = "high" if max_diff > ELA_HIGH else ("medium" if max_diff > ELA_MEDIUM else "low")
                    entry = _result(
                        "ela",
                        "suspicious" if max_diff > ELA_MEDIUM else "clean",
                        f"{name}: error level max difference {max_diff} ({level})",
                        f"/static_output/{unique_id}_ela.jpg",
                    )
                else:
                    entry = _result("ela", "not_applicable", NOT_APPLICABLE_DETAIL, None)
                entry["original_url"] = original_url
                results.append(entry)
            except Exception as e:
                results.append(_result("ela", "fail", f"{name}: ELA failed - {e}"))

            # 2. EXIF
            try:
                exif_data, notes, lat, lon = extract_exif(image)
                segments = list(notes)
                if lat is not None and lon is not None:
                    segments.append(f"GPS {lat}, {lon}")
                if not segments:
                    segments = [f"{len(exif_data)} EXIF tags present, no editing software"]
                entry = _result("exif", "suspicious" if notes else "clean", f"{name}: " + "; ".join(segments))
                entry["lat"] = lat
                entry["lon"] = lon
                results.append(entry)
            except Exception as e:
                results.append(_result("exif", "fail", f"{name}: EXIF check failed - {e}"))
    except Exception as e:
        results.append(_result("ela", "fail", f"forensics aborted - {e}"))
    return results


def run_forensics(photo_paths, conn):
    """Backwards-compatible wrapper: duplicate check followed by ELA + EXIF."""
    return check_duplicates(photo_paths, conn) + run_deep_forensics(photo_paths, conn)
