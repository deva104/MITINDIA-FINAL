import re
import string
from datetime import date
from difflib import SequenceMatcher

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

NARRATIVE_GROUPS = (
    (("front", "head-on", "head on", "frontal"),
     ("front_bumper", "bonnet", "headlight_left", "headlight_right", "grille")),
    (("rear", "rear-end", "rear end", "behind"),
     ("rear_bumper", "boot", "taillight_left", "taillight_right")),
)


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
    v = (v or "").lower()
    v = v.translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", v).strip()


def _norm_reg(v):
    return re.sub(r"[\s\-]", "", (v or "")).upper()


def _parse_date(v):
    if not v:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(v).strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


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


VEHICLE_MATCH_RATIO = 0.7


def _differs(a, b, fuzzy=False):
    """Nulls never count as a difference - only two stated values can disagree."""
    x, y = _norm_name(a), _norm_name(b)
    if not x or not y:
        return False
    if fuzzy:
        return SequenceMatcher(None, x, y).ratio() < VEHICLE_MATCH_RATIO
    return x != y


def _vehicle_mismatch(photos):
    entries = [p for p in (photos or [])
               if isinstance(p, dict) and p.get("is_vehicle") is True]
    if len(entries) < 2:
        return None

    for key, label, fuzzy in (
        ("colour", "colour", False),
        ("body_type", "body type", False),
        ("make_model", "make and model", True),
    ):
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                if not _differs(a.get(key), b.get(key), fuzzy):
                    continue
                evidence = [p.get("source_photo") for p in (a, b) if p.get("source_photo")]
                return {
                    "rule": "vehicle_mismatch",
                    "severity": "HIGH",
                    "message": (
                        f"Photos appear to show different vehicles: {label} "
                        f"'{a.get(key)}' vs '{b.get(key)}'. A claim covers a single vehicle."
                    ),
                    "evidence": evidence,
                }
    return None


def run_rules(fields, damage, photo_count: int = 0, is_vehicle=None, photos=None):
    flags = []
    damage = damage or []

    # 1. name_mismatch
    names = {n: get_field(fields, n) for n in NAME_FIELDS}
    if all(names[n] for n in NAME_FIELDS):
        worst_ratio = 1.0
        worst_pair = None
        keys = list(NAME_FIELDS)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                r = SequenceMatcher(None, _norm_name(names[keys[i]]), _norm_name(names[keys[j]])).ratio()
                if r < worst_ratio:
                    worst_ratio = r
                    worst_pair = (keys[i], keys[j])
        if worst_ratio < 0.85:
            a, b = worst_pair
            flags.append({
                "rule": "name_mismatch",
                "severity": "HIGH",
                "message": (
                    f"Name on {a} ('{names[a]}') does not match {b} ('{names[b]}') "
                    f"- similarity {worst_ratio:.2f}, below the 0.85 threshold."
                ),
                "evidence": list(NAME_FIELDS),
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

    # 3. dl_expired_at_loss
    dl_exp = _parse_date(get_field(fields, "dl_expiry_date"))
    acc_date = _parse_date(get_field(fields, "accident_date"))
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
    p_start = _parse_date(get_field(fields, "policy_start_date"))
    p_end = _parse_date(get_field(fields, "policy_end_date"))
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

    # 8. vehicle_mismatch - one claim covers one vehicle, so photos of two
    #    different cars mean the bundle is mixed.
    mismatch = _vehicle_mismatch(photos)
    if mismatch:
        flags.append(mismatch)

    return flags


def build_estimate(damage):
    line_items = []
    unpriced = []
    total_low = 0
    total_high = 0

    if not isinstance(damage, list):
        damage = []

    for d in damage:
        if not isinstance(d, dict):
            continue
        part = d.get("part")
        damage_type = d.get("damage_type")
        severity = d.get("severity")
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
                "reason": "no rate card entry",
            })
            continue
        low, high = rate
        line_items.append({
            "part": part,
            "damage_type": damage_type,
            "severity": severity,
            "low": low,
            "high": high,
        })
        total_low += low
        total_high += high

    requires_survey = total_high > SURVEY_THRESHOLD
    return {
        "line_items": line_items,
        "unpriced": unpriced,
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
