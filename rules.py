import re
from datetime import date

# Seeded from published market service rates.
# In production an insurer plugs in their own network rates.
RATE_CARD = {
    ("bumper", "minor"): (1500, 3000),
    ("bumper", "moderate"): (4000, 8000),
    ("bumper", "severe"): (8000, 15000),
    ("bonnet", "minor"): (3000, 6000),
    ("bonnet", "moderate"): (7000, 12000),
    ("bonnet", "severe"): (15000, 25000),
    ("boot", "minor"): (3000, 6000),
    ("boot", "moderate"): (7000, 12000),
    ("boot", "severe"): (14000, 22000),
    ("door", "minor"): (2500, 5000),
    ("door", "moderate"): (6000, 11000),
    ("door", "severe"): (14000, 22000),
    ("fender", "minor"): (2000, 4000),
    ("fender", "moderate"): (5000, 9000),
    ("fender", "severe"): (10000, 16000),
    ("quarter", "minor"): (2500, 5000),
    ("quarter", "moderate"): (6000, 11000),
    ("quarter", "severe"): (13000, 20000),
    ("headlight", "minor"): (2000, 4000),
    ("headlight", "moderate"): (6000, 10000),
    ("headlight", "severe"): (9000, 18000),
    ("taillight", "minor"): (1500, 3000),
    ("taillight", "moderate"): (4000, 7000),
    ("taillight", "severe"): (7000, 13000),
    ("windshield", "minor"): (3000, 6000),
    ("windshield", "moderate"): (6000, 10000),
    ("windshield", "severe"): (8000, 15000),
    ("mirror", "minor"): (1500, 3000),
    ("mirror", "moderate"): (3000, 6000),
    ("mirror", "severe"): (5000, 9000),
    ("grille", "minor"): (1200, 2500),
    ("grille", "moderate"): (3000, 6000),
    ("grille", "severe"): (6000, 11000),
    ("roof", "minor"): (4000, 8000),
    ("roof", "moderate"): (10000, 18000),
    ("roof", "severe"): (20000, 35000),
}

SURVEY_THRESHOLD = 50000
SURVEY_REASON = (
    "IRDAI 2024 regulations permit waiver of mandatory survey only for "
    "losses below Rs 50,000."
)

NAME_FIELDS = ("insured_name_claim_form", "owner_name_rc", "holder_name_dl")
HONORIFICS = {"MR", "MRS", "MS", "SHRI", "SMT", "DR"}
DATE_FIELDS = (
    "dl_expiry_date",
    "accident_date",
    "policy_start_date",
    "policy_end_date",
)

NARRATIVE_GROUPS = (
    (("front", "head-on", "head on", "frontal"),
     ("front_bumper", "bonnet", "headlight_left", "headlight_right", "grille")),
    (("rear", "rear-end", "rear end", "behind"),
     ("rear_bumper", "boot", "taillight_left", "taillight_right")),
)


DOCUMENT_LABELS = {
    "rc": "Registration certificate",
    "driving_licence": "Driving licence",
    "claim_form": "Claim form",
    "policy": "Insurance policy",
}


def document_requirements(documents_received, required, unread_documents=0):
    """The one place that decides what a claim is missing.

    Every path (WhatsApp DONE or SKIP, console upload) calls this with the
    documents the pipeline established. unread_documents counts documents of
    unknown type that could not be read: a requirement they might satisfy is
    "not established", never "not provided".
    """
    received = documents_received or {}
    missing = [doc_type for doc_type in required or [] if not received.get(doc_type)]
    names = ", ".join(DOCUMENT_LABELS.get(doc_type, doc_type).lower() for doc_type in missing)
    if not missing:
        reason = None
    elif unread_documents:
        noun = "document" if unread_documents == 1 else "documents"
        reason = (
            f"required documents not established: {names} "
            f"({unread_documents} {noun} received could not be read)"
        )
    else:
        reason = f"documents not provided: {names}"
    return {
        "missing": missing,
        "unverified": bool(missing and unread_documents),
        "unread_documents": int(unread_documents or 0),
        "reason": reason,
    }


CLASSIFICATION_THRESHOLD = 0.6


def route_classification(result):
    """Where one classified image goes: "vehicle_photo", a document type, or
    "unclassified". Uncertain or unscored images are kept as vehicle photos
    so damage is never dropped; only a confident document is a document."""
    result = result or {}
    doc_type = result.get("doc_type")
    confidence = reported_confidence(result)
    if confidence is None or confidence < CLASSIFICATION_THRESHOLD or doc_type == "vehicle_photo":
        return "vehicle_photo"
    if doc_type in DOCUMENT_LABELS:
        return doc_type
    return "unclassified"


def infer_document_types(fields):
    """Document types a set of extracted fields shows was present."""
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


def establish_documents(classifications, extraction, unknown_documents, required):
    """The single source of truth for which documents a claim has.

    The WhatsApp checklist, the console Documents card, the undetermined list
    and the decision reason all read the object this returns, so they cannot
    disagree.

    classifications: one {"filename", "doc_type", "confidence"} per image.
    extraction: what gemini.extract_fields returned (fields carry source_doc).
    unknown_documents: names of documents of unknown type (PDFs, page sets),
    whose type is whatever was read from them.
    """
    received = {doc_type: 0 for doc_type in DOCUMENT_LABELS}
    sources = {doc_type: [] for doc_type in DOCUMENT_LABELS}
    for item in classifications or []:
        if not isinstance(item, dict):
            continue
        route = route_classification(item)
        if route in received:
            received[route] += 1
            sources[route].append(item.get("filename") or item.get("path"))
    extraction = extraction or {}
    failed = {item.get("name") for item in extraction.get("failed") or [] if isinstance(item, dict)}
    fields = extraction.get("fields") or []
    unread = 0
    for name in unknown_documents or []:
        if name in failed:
            unread += 1
            continue
        own = [field for field in fields if isinstance(field, dict) and field.get("source_doc") == name]
        for doc_type in infer_document_types(own):
            received[doc_type] = max(1, received[doc_type])
            sources[doc_type].append(name)
    check = document_requirements(received, required, unread)
    check["received"] = received
    check["sources"] = {key: value for key, value in sources.items() if value}
    check["required"] = list(required or [])
    return check


def get_field(fields, name):
    for f in fields or []:
        if isinstance(f, dict) and f.get("name") == name:
            v = f.get("value")
            if v is None:
                return None
            v = str(v).strip()
            return v if v and v.lower() not in ("null", "none", "n/a") else None
    return None


def _norm_name(v):
    text = re.sub(r"[^A-Z0-9\s]", " ", str(v or "").upper())
    tokens = [token for token in text.split() if token not in HONORIFICS]
    return " ".join(tokens)


def _token_compatible(a, b):
    return a == b or (
        (len(a) == 1 and b.startswith(a))
        or (len(b) == 1 and a.startswith(b))
    )


def _names_match(a, b):
    left = _norm_name(a).split()
    right = _norm_name(b).split()
    if not left or not right or left[-1] != right[-1]:
        return False

    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    available = list(longer[:-1])
    for token in shorter[:-1]:
        match = next(
            (index for index, candidate in enumerate(available)
             if _token_compatible(token, candidate)),
            None,
        )
        if match is None:
            return False
        available.pop(match)
    return True


def _norm_reg(v):
    return re.sub(r"[\s\-]", "", (v or "")).upper()


def parse_date(value):
    if not value:
        return None, False
    text = str(value).strip()
    formats = (
        (r"^(\d{4})-(\d{2})-(\d{2})$", "ymd"),
        (r"^(\d{2})-(\d{2})-(\d{4})$", "dmy"),
        (r"^(\d{2})/(\d{2})/(\d{4})$", "dmy"),
        (r"^(\d{4})/(\d{2})/(\d{2})$", "ymd"),
    )
    for pattern, order in formats:
        match = re.match(pattern, text)
        if not match:
            continue
        first, second, third = (int(part) for part in match.groups())
        if order == "ymd":
            year, month, day = first, second, third
            ambiguous = False
        else:
            day, month, year = first, second, third
            ambiguous = day <= 12 and month <= 12
        try:
            return date(year, month, day), ambiguous
        except ValueError:
            return None, False
    return None, False


def _part_group(part):
    p = (part or "").lower()
    if "bumper" in p:
        return "bumper"
    if "windshield" in p:
        return "windshield"
    if "headlight" in p:
        return "headlight"
    if "taillight" in p:
        return "taillight"
    if "door" in p:
        return "door"
    if "fender" in p:
        return "fender"
    if "quarter" in p:
        return "quarter"
    if "mirror" in p:
        return "mirror"
    if p == "bonnet":
        return "bonnet"
    if p == "boot":
        return "boot"
    if p == "grille":
        return "grille"
    if p == "roof":
        return "roof"
    return None


def run_rules(fields, damage, photo_count: int = 0, is_vehicle=None, photos=None,
              vehicles=None, claimable_count: int = 0, forensics=None):
    flags = []
    damage = damage or []

    # 1. name_mismatch
    names = {n: get_field(fields, n) for n in NAME_FIELDS}
    present_names = [(name, value) for name, value in names.items() if value]
    for index, (a, a_value) in enumerate(present_names):
        for b, b_value in present_names[index + 1:]:
            if not _names_match(a_value, b_value):
                flags.append({
                    "rule": "name_mismatch",
                    "severity": "HIGH",
                    "message": (
                        f"Name on {a} ('{a_value}') does not match "
                        f"{b} ('{b_value}')."
                    ),
                    "evidence": [a, b],
                })

    date_values = {name: get_field(fields, name) for name in DATE_FIELDS}
    parsed_dates = {}
    for field_name, raw_value in date_values.items():
        parsed, _ = parse_date(raw_value)
        parsed_dates[field_name] = parsed
        if raw_value is not None and parsed is None:
            flags.append({
                "rule": "date_undetermined",
                "severity": "MEDIUM",
                "message": (
                    f"Could not parse {field_name} value '{raw_value}'; "
                    "date-dependent checks are undetermined."
                ),
                "evidence": [field_name],
            })

    # 2. registration_mismatch
    reg_form = get_field(fields, "registration_number_claim_form")
    reg_rc = get_field(fields, "registration_number_rc")
    if reg_form and reg_rc and _norm_reg(reg_form) != _norm_reg(reg_rc):
        flags.append({
            "rule": "registration_mismatch",
            "severity": "HIGH",
            "message": (
                f"Registration number on the claim form ('{reg_form}') does not match "
                f"the Registration Certificate ('{reg_rc}')."
            ),
            "evidence": ["registration_number_claim_form", "registration_number_rc"],
        })

    # A legible plate in the submitted vehicle photo must belong to the RC.
    if reg_rc:
        normalised_rc = _norm_reg(reg_rc)
        for photo in photos or []:
            if not isinstance(photo, dict) or not photo.get("number_plate"):
                continue
            number_plate = str(photo["number_plate"])
            if _norm_reg(number_plate) != normalised_rc:
                source = photo.get("source_photo") or "vehicle photo"
                flags.append({
                    "rule": "vehicle_identity_mismatch",
                    "severity": "HIGH",
                    "message": (
                        f"Number plate '{number_plate}' in {source} does not match "
                        f"the RC registration '{reg_rc}'."
                    ),
                    "evidence": [source, number_plate, reg_rc],
                })

    # 3. dl_expired_at_loss
    dl_exp = parsed_dates["dl_expiry_date"]
    acc_date = parsed_dates["accident_date"]
    if dl_exp and acc_date and dl_exp < acc_date:
        flags.append({
            "rule": "dl_expired_at_loss",
            "severity": "HIGH",
            "message": (
                f"Driving licence expired on {dl_exp.isoformat()}, before the "
                f"accident date {acc_date.isoformat()}."
            ),
            "evidence": ["dl_expiry_date", "accident_date"],
        })

    # 4. policy_not_active
    p_start = parsed_dates["policy_start_date"]
    p_end = parsed_dates["policy_end_date"]
    if acc_date and (p_start or p_end):
        if (p_start and acc_date < p_start) or (p_end and acc_date > p_end):
            flags.append({
                "rule": "policy_not_active",
                "severity": "HIGH",
                "message": (
                    f"Accident date {acc_date.isoformat()} falls outside the policy period "
                    f"{p_start.isoformat() if p_start else '?'} to "
                    f"{p_end.isoformat() if p_end else '?'}."
                ),
                "evidence": ["accident_date", "policy_start_date", "policy_end_date"],
            })

    # 5. damage_narrative_mismatch
    desc = get_field(fields, "accident_description")
    damaged_parts = [d.get("part") for d in damage if isinstance(d, dict) and d.get("part")]
    if desc and damaged_parts:
        low = desc.lower()
        implied = set()
        matched_kw = []
        for keywords, parts in NARRATIVE_GROUPS:
            for kw in keywords:
                if kw in low:
                    implied.update(parts)
                    matched_kw.append(kw)
                    break
        for kw, side in (("left", "left"), ("driver side", "left"), ("right", "right")):
            if kw in low:
                implied.update(p for p in damaged_parts if side in p.lower())
                matched_kw.append(kw)
        if implied and not (set(damaged_parts) & implied):
            flags.append({
                "rule": "damage_narrative_mismatch",
                "severity": "MEDIUM",
                "message": (
                    f"Accident description mentions {', '.join(sorted(set(matched_kw)))} "
                    f"but the detected damage ({', '.join(sorted(set(damaged_parts)))}) "
                    f"is entirely outside the implied area."
                ),
                "evidence": sorted(set(damaged_parts)) + ["accident_description"],
            })

    # 6. no_damage_detected - only for a confirmed vehicle, so an AI failure
    #    or a non-vehicle photo is never reported as "no damage".
    if is_vehicle is True and photo_count > 0 and not damage:
        flags.append({
            "rule": "no_damage_detected",
            "severity": "HIGH",
            "message": (
                f"Claim submitted with {photo_count} photo(s) but no visible "
                "vehicle damage was detected. Manual review required."
            ),
            "evidence": [],
        })

    # 7. not_a_vehicle
    if is_vehicle is False:
        flags.append({
            "rule": "not_a_vehicle",
            "severity": "HIGH",
            "message": (
                f"The submitted photo(s) do not show a motor vehicle, so no damage "
                f"assessment could be performed on {photo_count} uploaded photo(s)."
            ),
            "evidence": [],
        })

    # 8. vehicle_mismatch - model grouping distinguishes another vehicle from
    #    another angle of the same vehicle.
    if claimable_count > 1:
        labels = [
            vehicle.get("label")
            for vehicle in (vehicles or [])
            if isinstance(vehicle, dict)
            and vehicle.get("status") == "claimable"
            and vehicle.get("label")
        ]
        flags.append({
            "rule": "vehicle_mismatch",
            "severity": "HIGH",
            "message": (
                f"Photos show {claimable_count} different vehicles. "
                "A claim covers one vehicle."
            ),
            "evidence": labels,
        })

    # 9. AI-generation detection is a review signal, never a rejection verdict.
    for check in forensics or []:
        if (
            not isinstance(check, dict)
            or check.get("check") != "ai_generated"
            or check.get("verdict") != "suspicious"
        ):
            continue
        photo = check.get("source_photo") or "submitted photo"
        try:
            score = f"{float(check.get('score')):.2f}"
        except (TypeError, ValueError):
            score = "unknown"
        flags.append({
            "rule": "ai_generated_photo",
            "severity": "HIGH",
            "message": (
                f"{photo} shows strong signs of AI generation "
                f"(score {score})."
            ),
            "evidence": [photo, score],
        })

    return flags


def reported_confidence(item):
    """The model's confidence, or None when it did not report one."""
    try:
        value = item.get("confidence")
    except AttributeError:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return max(0.0, min(1.0, value))


def _damage_confidence(item):
    try:
        value = float(item.get("confidence"))
    except (AttributeError, TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def assess_confidence(payload):
    damage = [
        item for item in (payload.get("damage") or [])
        if isinstance(item, dict)
    ]
    # Averaged over findings the model scored; None when it scored none.
    reported = [value for value in (reported_confidence(item) for item in damage)
                if value is not None]
    confidence = sum(reported) / len(reported) if reported else None

    if payload.get("status") == "rejected":
        stopped_at = payload.get("stopped_at") or "validation"
        return {
            "decision": "reject",
            "reasons": [f"Claim rejected at {stopped_at}"],
            "confidence": round(confidence, 3) if confidence is not None else None,
        }

    reasons = []
    if payload.get("status") == "error":
        reasons.append("Automated assessment unavailable: escalated to an assessor")
    for flag in payload.get("flags") or []:
        if isinstance(flag, dict) and str(flag.get("severity")).upper() == "HIGH":
            reasons.append(flag.get("message") or f"High-severity flag: {flag.get('rule')}")

    estimate = payload.get("estimate") or {}
    if estimate.get("requires_survey") is True:
        reasons.append(estimate.get("survey_reason") or "Estimate requires a survey")

    claimable_count = payload.get("claimable_count") or 0
    if claimable_count > 1:
        reasons.append(
            f"claimable_count is {claimable_count}; a claim covers one vehicle"
        )

    for item in damage:
        part = str(item.get("part") or "unknown part").replace("_", " ")
        value = reported_confidence(item)
        if value is None:
            reasons.append(f"Model did not report confidence for {part}")
        elif value < 0.6:
            reasons.append(f"Low-confidence damage finding: {part}")

    for field in payload.get("fields") or []:
        if isinstance(field, dict) and field.get("status") == "unreadable":
            reasons.append(f"Unreadable field: {field.get('name')}")

    reasons.extend(extraction_reasons(payload.get("extraction")))
    document_check = payload.get("document_check") or {}
    if document_check.get("reason"):
        reasons.append(document_check["reason"])

    if estimate.get("unpriced"):
        reasons.append("One or more confirmed damage items require manual pricing")

    # Keep stable ordering while suppressing duplicate reasons raised by
    # overlapping checks.
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    return {
        "decision": "needs_review" if reasons else "auto_assess",
        "reasons": reasons,
        "confidence": round(confidence, 3) if confidence is not None else None,
    }


def extraction_reasons(extraction):
    """Decision reasons for documents that were received but not read."""
    extraction = extraction or {}
    failed = [item for item in extraction.get("failed") or [] if isinstance(item, dict)]
    if extraction.get("status") == "failed":
        count = int(extraction.get("documents") or len(failed) or 0)
        noun = "document" if count == 1 else "documents"
        return [
            f"{count} {noun} received but could not be read: "
            f"{extraction.get('error') or 'unknown error'}"
        ]
    if extraction.get("status") == "partial":
        return [
            f"{item.get('name') or 'A document'} could not be read: "
            f"{item.get('error') or 'unknown error'}"
            for item in failed
        ]
    return []


def build_estimate(damage):
    line_items = []
    unpriced = []
    unconfirmed = []
    total_low = 0
    total_high = 0

    if not isinstance(damage, list):
        damage = []

    severity_order = {"minor": 1, "moderate": 2, "severe": 3}
    by_part = {}
    for d in damage:
        if not isinstance(d, dict):
            continue
        if not isinstance(d.get("confirmed"), bool):
            d["confirmed"] = _damage_confidence(d) >= 0.6
        if d.get("rejected") is True:
            continue
        by_part.setdefault(d.get("part"), []).append(d)

    # A panel is repaired or replaced once, regardless of how many damage types
    # appear on it.
    for part, reports in by_part.items():
        for report in reports:
            if report.get("confirmed"):
                continue
            report_part = report.get("part")
            report_severity = report.get("severity")
            group = _part_group(str(report_part) if report_part is not None else "")
            severity = (
                str(report_severity).strip().lower()
                if report_severity is not None
                else ""
            )
            rate = RATE_CARD.get((group, severity)) if group else None
            entry = {
                "damage_id": report.get("damage_id"),
                "part": report_part,
                "damage_type": report.get("damage_type"),
                "severity": report_severity,
                "confidence": reported_confidence(report),
            }
            if rate:
                entry["low"], entry["high"] = rate
            else:
                entry["low"] = None
                entry["high"] = None
                entry["reason"] = "no rate card entry"
            unconfirmed.append(entry)

        confirmed_reports = [
            report for report in reports if report.get("confirmed")
        ]
        if not confirmed_reports:
            continue

        # An insurer must not be given a total that includes findings the system
        # is unsure about.
        d = max(
            confirmed_reports,
            key=lambda item: severity_order.get(
                str(item.get("severity") or "").strip().lower(),
                0,
            ),
        )
        part = d.get("part")
        damage_type = d.get("damage_type")
        severity = d.get("severity")
        also_reported = []
        for report in confirmed_reports:
            if report is d:
                continue
            other_type = report.get("damage_type")
            if other_type and other_type != damage_type and other_type not in also_reported:
                also_reported.append(other_type)
        try:
            group = _part_group(str(part) if part is not None else "")
            sev = str(severity).strip().lower() if severity is not None else ""
            rate = RATE_CARD.get((group, sev)) if group else None
        except Exception:
            rate = None
        if not rate:
            unpriced.append({
                "part": part,
                "damage_type": damage_type,
                "severity": severity,
                "also_reported": also_reported,
                "reason": "no rate card entry",
            })
            continue
        low, high = rate
        line_items.append({
            "damage_id": d.get("damage_id"),
            "part": part,
            "damage_type": damage_type,
            "severity": severity,
            "also_reported": also_reported,
            "low": low,
            "high": high,
        })
        total_low += low
        total_high += high

    # The indicative range follows the same rule as the confirmed one: a panel
    # is priced once. One line per part, the most severe report wins, and a part
    # that already has a confirmed line adds nothing.
    confirmed_parts = {item.get("part") for item in line_items + unpriced}
    chosen = {}
    for entry in unconfirmed:
        part = entry.get("part")
        if part in confirmed_parts:
            entry["counts_toward_indicative"] = False
            entry["covered_by"] = "confirmed line for this part"
            continue
        if entry.get("low") is None:
            entry["counts_toward_indicative"] = False
            continue
        current = chosen.get(part)
        rank = (
            severity_order.get(str(entry.get("severity") or "").strip().lower(), 0),
            entry.get("high") or 0,
        )
        if current is None or rank > (
            severity_order.get(str(current.get("severity") or "").strip().lower(), 0),
            current.get("high") or 0,
        ):
            chosen[part] = entry
    for entry in unconfirmed:
        if entry.get("part") in confirmed_parts or entry.get("low") is None:
            continue
        counts = chosen.get(entry.get("part")) is entry
        entry["counts_toward_indicative"] = counts
        if not counts:
            entry["covered_by"] = "more severe finding on this part"
    indicative_low = sum(entry["low"] for entry in chosen.values())
    indicative_high = sum(entry["high"] for entry in chosen.values())

    requires_survey = total_high > SURVEY_THRESHOLD
    return {
        "line_items": line_items,
        "unpriced": unpriced,
        "unconfirmed": unconfirmed,
        "indicative_low": indicative_low,
        "indicative_high": indicative_high,
        "total_low": total_low,
        "total_high": total_high,
        "requires_survey": requires_survey,
        "survey_reason": SURVEY_REASON if requires_survey else "",
    }


if __name__ == "__main__":
    import json as _json
    sample = [
        {"part": "spoiler", "damage_type": "crack", "severity": "catastrophic"},
        {"part": "front_bumper", "damage_type": "dent", "severity": "moderate"},
    ]
    print(_json.dumps(build_estimate(sample), indent=2))
    print("empty ->", _json.dumps(build_estimate([])))
    print("garbage ->", _json.dumps(build_estimate([{"part": None, "severity": 7}, "junk", 42])))
