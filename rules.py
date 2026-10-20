import os
import re

import vehicle_terms
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
    "IRDAI Master Circular on Protection of Policyholders' Interests, 2024: "
    "motor losses below Rs 50,000 need not be surveyed by a registered surveyor."
)

# In Indian motor insurance the driver need not be the owner. The insured is
# matched to the RC owner and the driver to the licence holder; owner versus
# licence holder on its own is information, never a flag.
# Fields a check uses only when read: missing or unreadable, they are skipped
# without a decision reason or a "could not determine" entry.
SILENT_FIELDS = {"registration_valid_until", "licence_expiry_nt", "licence_expiry_tr"}

NAME_CHECKS = (
    (
        "insured_owner_mismatch", "insured_name_claim_form", "owner_name_rc",
        "Insured name on the claim form ('{a}') does not match the registered "
        "owner on the RC ('{b}').",
    ),
    (
        "driver_licence_mismatch", "driver_name_claim_form", "holder_name_dl",
        "Driver named on the claim form ('{a}') does not match the driving "
        "licence holder ('{b}').",
    ),
)
HONORIFICS = {"MR", "MRS", "MS", "MISS", "SHRI", "SRI", "SHREE", "SMT", "KUMARI", "DR"}
# Spellings of one name part, as they appear across Indian documents.
NAME_ALIASES = {
    "MD": "MOHAMMED", "MOHD": "MOHAMMED", "MOHAMMAD": "MOHAMMED", "MOHAMED": "MOHAMMED",
    "MUHAMMAD": "MOHAMMED", "MUHAMMED": "MOHAMMED",
    "PVT": "PRIVATE", "PRIVATE": "PRIVATE", "LTD": "LIMITED", "LIMITED": "LIMITED",
    "DEPTT": "DEPARTMENT", "DEPT": "DEPARTMENT", "DEPARTMENT": "DEPARTMENT",
    "GOVT": "GOVERNMENT", "CO": "COMPANY",
}
# Words that make a name an organisation's (a department, an office, a
# company), not a person's: no initials, no reordering into a person's name.
ORGANISATION_WORDS = {
    "DEPARTMENT", "DIRECTOR", "DIRECTORATE", "COMMISSIONER", "COMMISSION", "OFFICER",
    "OFFICE", "GOVERNMENT", "MINISTRY", "SECRETARY", "SECRETARIAT", "COLLECTOR",
    "CORPORATION", "COMPANY", "LIMITED", "PRIVATE", "LLP", "BANK", "TRUST", "SOCIETY",
    "UNIVERSITY", "COLLEGE", "SCHOOL", "HOSPITAL", "MUNICIPAL", "COUNCIL", "BOARD",
    "AUTHORITY", "POLICE", "DISTRICT", "INDUSTRIES", "ENTERPRISES", "TRADERS", "AGENCIES",
    "SERVICES", "SOLUTIONS", "TECHNOLOGIES", "ASSOCIATION", "FOUNDATION", "INSTITUTE",
}
ORGANISATION_FILLERS = {"THE", "OF", "AND", "FOR"}
# "Ramesh S/O Krishnan": the relation and the father's or husband's name that
# follows are not part of the person's name.
RELATION_SUFFIX = re.compile(
    r"\b(?:[SWDC]\s*/\s*O|[SWDC]\s*\.\s*O\s*\.?|SON\s+OF|WIFE\s+OF|DAUGHTER\s+OF|CARE\s+OF)\b.*$",
    re.IGNORECASE,
)
FIRM_PREFIX = re.compile(r"\bM\s*/\s*S\b\.?", re.IGNORECASE)
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
        "insured_name_claim_form", "driver_name_claim_form",
        "registration_number_claim_form", "accident_date", "accident_time",
        "accident_location", "accident_description",
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
        # A document read in chunks names its source as "<name> p.1-3".
        own = [
            field for field in fields
            if isinstance(field, dict) and (
                field.get("source_doc") == name
                or str(field.get("source_doc") or "").startswith(f"{name} p.")
            )
        ]
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


def _name_tokens(value):
    text = RELATION_SUFFIX.sub(" ", str(value or ""))
    text = FIRM_PREFIX.sub(" ", text)
    text = re.sub(r"[^A-Z0-9\s]", " ", text.upper())
    return [NAME_ALIASES.get(token, token) for token in text.split() if token not in HONORIFICS]


def _token_compatible(a, b):
    """Same word, or an initial standing for the other word."""
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def is_organisation(tokens):
    return bool(set(tokens) & ORGANISATION_WORDS)


def _organisation_match(left, right):
    """Organisation names: the same words in any order ("The Deputy
    Commissioner, Transport Department" = "THE DEPUTY COMMISSIONER , TRANSPORT
    DEPTT"), with THE/OF/AND/FOR ignored. Never initials."""
    left = [token for token in left if token not in ORGANISATION_FILLERS]
    right = [token for token in right if token not in ORGANISATION_FILLERS]
    if not left or not right:
        return "mismatch"
    if sorted(left) == sorted(right):
        return "match"
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    remaining = list(longer)
    for token in shorter:
        if token not in remaining:
            return "mismatch"
        remaining.remove(token)
    return "partial"


def name_match(a, b):
    """"match", "partial" or "mismatch" for two renderings of a name.

    Order does not matter ("S Karthik" is "Karthik S"), an initial stands for
    a word ("K R Priya" is "Priya Krishnan Ramesh"), relation suffixes and
    honorifics are ignored, and MD/MOHD, PVT/LTD are spellings of one word.
    "partial" means every part of one name is in the other, which has more
    ("Rajesh Kumar" in "Rajesh Kumar Singh"). Initials alone never match.
    """
    left, right = _name_tokens(a), _name_tokens(b)
    if not left or not right:
        return "mismatch"
    if is_organisation(left) or is_organisation(right):
        return _organisation_match(left, right)
    # A leading "The" is not part of a person's name either.
    left = left[1:] if left[0] == "THE" and len(left) > 1 else left
    right = right[1:] if right[0] == "THE" and len(right) > 1 else right
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    remaining = list(longer)
    pending, words_equal = [], 0
    # Whole words first, so an initial cannot take a word another word needs.
    for token in shorter:
        if len(token) > 1 and token in remaining:
            remaining.remove(token)
            words_equal += 1
        else:
            pending.append(token)
    for token in pending:
        index = next(
            (i for i, other in enumerate(remaining) if _token_compatible(token, other)), None
        )
        if index is None:
            return "mismatch"
        remaining.pop(index)
    if not words_equal:
        return "mismatch"
    return "match" if not remaining else "partial"


def _names_match(a, b):
    """The same person for the purpose of a flag: a match or a partial match."""
    return name_match(a, b) != "mismatch"


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


def _claimed_photo_names(vehicles):
    """Lower-case photo names of the vehicle the claim is for: the first
    claimable group, as stage 3 prices it. None when no vehicle is claimable,
    so every legible plate is still checked."""
    claimed = next(
        (vehicle for vehicle in vehicles or []
         if isinstance(vehicle, dict) and vehicle.get("status") == "claimable"),
        None,
    )
    if claimed is None:
        return None
    return {
        str(name).replace("\\", "/").rsplit("/", 1)[-1].lower()
        for name in claimed.get("photos") or []
    }


def _registered_vehicle(registry):
    return f"{registry.get('make') or ''} {registry.get('model') or ''}".strip()


def appearance_mismatch(registry, seen):
    """("HIGH"|"MEDIUM", message) when the photographed vehicle differs from
    the registered one, else None. Make AND body type differing is HIGH; make
    alone, body type alone or colour alone is MEDIUM (models misread makes,
    and light changes colours). Every value goes through vehicle_terms: a
    value it does not know, on either side, is not compared; a near colour
    (silver/grey, blue/green) is not a difference."""
    seen = seen or {}
    seen_text = str(seen.get("make_model") or "")
    make_differs = vehicle_terms.make_relation(seen_text, _registered_vehicle(registry)) == "different"
    body_differs = vehicle_terms.body_relation(seen.get("body_type"),
                                               registry.get("body_type")) == "different"
    colour_differs = vehicle_terms.colour_relation(seen.get("colour"),
                                                   registry.get("colour")) == "different"
    if not (make_differs or body_differs or colour_differs):
        return None
    shown = " ".join(str(part) for part in (seen.get("colour"), seen_text or seen.get("body_type"))
                     if part)
    on_record = " ".join(str(part) for part in (registry.get("colour"), registry.get("make"),
                                                registry.get("model")) if part)
    if make_differs and body_differs:
        return "HIGH", f"Photos show a {shown}; this plate is registered to a {on_record}."
    # A model misreads makes (a non-Subaru was labelled "Subaru"): a make or a
    # body type alone is not proof, and colour depends on the light.
    if make_differs:
        return "MEDIUM", (f"Vehicle make could not be confirmed from photos: they suggest a "
                          f"{seen_text}; this plate is registered to a {on_record}.")
    if body_differs:
        return "MEDIUM", (f"Vehicle body type could not be confirmed from photos: they suggest a "
                          f"{seen.get('body_type')}; this plate is registered to a {on_record}.")
    return "MEDIUM", f"Photos show a {shown}; this plate is registered to a {on_record}."


def _tail5(value):
    """The last five characters of a chassis or engine number, or None when
    fewer than five are legible or one of them was unreadable ("?"). VINs
    never use I, O or Q, so those are read as 1, 0, 0."""
    text = re.sub(r"[^A-Z0-9?]", "", str(value or "").upper())
    if len(text) < 5 or "?" in text[-5:]:
        return None
    return text[-5:].translate(str.maketrans("OIQ", "010"))


def rc_identity_flags(fields, registry):
    """The RC's printed make, model, chassis and engine against the registry.
    A value missing or unknown on either side is skipped without a word."""
    flags = []
    rc_vehicle = get_field(fields, "vehicle_make_model")
    on_record = _registered_vehicle(registry)
    if rc_vehicle and on_record:
        rows = [{"source": "RC make and model", "value": rc_vehicle},
                {"source": "Registered vehicle (VAHAN)", "value": on_record}]
        if vehicle_terms.make_relation(rc_vehicle, on_record) == "different":
            flags.append({
                "rule": "registry_rc_make_mismatch", "severity": "HIGH",
                "message": f"The RC names a {rc_vehicle}; this plate is registered to a {on_record}.",
                "evidence": ["vehicle_make_model", "registry.make"],
                "comparison": {"rows": rows, "verdict": "MISMATCH"},
            })
        elif vehicle_terms.model_relation(rc_vehicle, registry.get("model")) == "different":
            flags.append({
                "rule": "registry_rc_model_mismatch", "severity": "MEDIUM", "review": True,
                "message": f"The RC names a {rc_vehicle}; this plate is registered to a {on_record}.",
                "evidence": ["vehicle_make_model", "registry.model"],
                "comparison": {"rows": rows, "verdict": "MISMATCH"},
            })
    for field, column, label in (("chassis_number", "chassis_last5", "Chassis"),
                                 ("engine_number", "engine_last5", "Engine")):
        printed, registered = _tail5(get_field(fields, field)), _tail5(registry.get(column))
        if printed and registered and printed != registered:
            flags.append({
                "rule": f"registry_{label.lower()}_mismatch", "severity": "MEDIUM", "review": True,
                "message": (f"{label} number on the RC ends {printed}; the registry has "
                            f"{registered}. Check the RC image before deciding."),
                "evidence": [field, f"registry.{column}"],
                "comparison": {"rows": [
                    {"source": f"RC {label.lower()} number (last 5)", "value": printed},
                    {"source": f"Registered {label.lower()} number (last 5)", "value": registered},
                ], "verdict": "MISMATCH"},
            })
    return flags


def registry_flags(fields, registry, seen=None, accident_date=None):
    """Cross-checks against the vehicle's registration record."""
    flags = []
    plate = _norm_reg(registry.get("plate"))
    reg_rc = get_field(fields, "registration_number_rc")
    if reg_rc and _norm_reg(reg_rc) != plate:
        flags.append({
            "rule": "registry_rc_mismatch", "severity": "HIGH",
            "message": f"The RC shows registration '{reg_rc}', but this claim is for {plate}.",
            "evidence": ["registration_number_rc", "registry.plate"],
        })
    owner_rc = get_field(fields, "owner_name_rc")
    owner = registry.get("owner_name")
    if owner_rc and owner and name_match(owner_rc, owner) == "mismatch":
        flags.append({
            "rule": "registry_owner_mismatch", "severity": "HIGH",
            "message": f"The RC owner ('{owner_rc}') is not the registered owner of {plate}.",
            "evidence": ["owner_name_rc", "registry.owner_name"],
        })
    flags.extend(rc_identity_flags(fields, registry))
    mismatch = appearance_mismatch(registry, seen)
    if mismatch:
        severity, message = mismatch
        shown = " ".join(str(part) for part in (
            (seen or {}).get("colour"), (seen or {}).get("make_model"),
            (seen or {}).get("body_type")) if part)
        on_record = " ".join(str(part) for part in (
            registry.get("colour"), registry.get("make"), registry.get("model"),
            registry.get("body_type")) if part)
        flags.append({
            "rule": "registry_vehicle_mismatch", "severity": severity, "message": message,
            "evidence": ["photos", "registry.make", "registry.colour", "registry.body_type"],
            "comparison": {
                "rows": [{"source": "Photos show", "value": shown or "not described"},
                         {"source": "Registered vehicle (VAHAN)", "value": on_record}],
                "verdict": "MISMATCH" if severity == "HIGH" else "NOT CONFIRMED",
            },
        })
    if registry.get("policy_type") == "third_party_only":
        flags.append({
            "rule": "policy_third_party_only", "severity": "HIGH",
            "message": f"The policy on {plate} is third-party only; own damage is not covered.",
            "evidence": ["registry.policy_type"],
        })
    elif accident_date:
        start = parse_date(registry.get("policy_start"))[0]
        end = parse_date(registry.get("policy_end"))[0]
        if start and end and not (start <= accident_date <= end):
            flags.append({
                "rule": "policy_not_active", "severity": "HIGH",
                "message": (
                    f"Accident date {accident_date.isoformat()} falls outside the registered "
                    f"policy period {start.isoformat()} to {end.isoformat()} "
                    f"({registry.get('insurer')})."
                ),
                "evidence": ["accident_date", "registry.policy_start", "registry.policy_end"],
            })
    return flags


def registry_notes(fields, registry):
    """INFO about the registered owner: a partial name match."""
    owner_rc = get_field(fields, "owner_name_rc")
    owner = (registry or {}).get("owner_name")
    if owner_rc and owner and name_match(owner_rc, owner) == "partial":
        return [{
            "rule": "registry_owner_partial_match", "severity": "INFO",
            "message": (f"The RC owner ('{owner_rc}') matches the registered owner "
                        f"('{owner}') in part."),
            "evidence": ["owner_name_rc", "registry.owner_name"],
        }]
    return []


def vehicle_matches_registry(registry, seen):
    """How well a photographed vehicle matches the record: make +2 (-10 when
    it differs), model +1, body and colour +1 each (-1 when they differ; a
    near colour counts 0). Values vehicle_terms does not know count 0."""
    if not registry or not seen:
        return 0
    score = 0
    make = vehicle_terms.make_relation(seen.get("make_model"), _registered_vehicle(registry))
    if make == "different":
        score -= 10
    elif make == "same":
        score += 2
    if vehicle_terms.model_relation(seen.get("make_model"), registry.get("model")) == "same":
        score += 1
    body = vehicle_terms.body_relation(seen.get("body_type"), registry.get("body_type"))
    score += {"same": 1, "different": -1}.get(body, 0)
    colour = vehicle_terms.colour_relation(seen.get("colour"), registry.get("colour"))
    score += {"same": 1, "different": -1}.get(colour, 0)
    return score


TRANSPORT_WORDS = ("transport", "commercial", "goods", "taxi", "contract carriage", "bus")


def _is_transport(registry):
    """A transport (commercial) vehicle, when the registry says so. Every
    vehicle in the mock registry is a private car (non-transport)."""
    vehicle_class = str((registry or {}).get("vehicle_class") or "").lower()
    return bool(vehicle_class) and "non" not in vehicle_class and any(
        word in vehicle_class for word in TRANSPORT_WORDS)


def licence_validity(fields, registry=None):
    """(date, field) the driving licence must be valid until for this vehicle.

    Indian smart-card licences can print Non-Transport (NT) and Transport (TR)
    validity separately. A private (non-transport) vehicle needs NT validity;
    a transport vehicle needs TR. Two dates whose labels could not be read:
    the later one, for a non-transport vehicle. One date: that date."""
    nt = parse_date(get_field(fields, "licence_expiry_nt"))[0]
    tr = parse_date(get_field(fields, "licence_expiry_tr"))[0]
    printed = parse_date(get_field(fields, "dl_expiry_date"))[0]
    if _is_transport(registry):
        if tr:
            return tr, "licence_expiry_tr"
        return (printed, "dl_expiry_date") if printed else (None, None)
    if nt:
        return nt, "licence_expiry_nt"
    if tr and printed and printed != tr:
        return max(tr, printed), "dl_expiry_date" if printed > tr else "licence_expiry_tr"
    if printed:
        return printed, "dl_expiry_date"
    return None, None


def _ist_today():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def late_intimation_days():
    try:
        return max(0, int(os.getenv("LATE_INTIMATION_DAYS", "30")))
    except ValueError:
        return 30


# Policy wordings ask for immediate notice of an accident, but IRDAI circular
# IRDA/HLTH/MISC/CIR/216/09/2011 says a genuine claim must not be rejected for
# a delay in intimation alone. Late intimation therefore only sends the claim
# to an assessor (review), never to a rejection.
LATE_INTIMATION_CIRCULAR = "IRDA/HLTH/MISC/CIR/216/09/2011"


def run_rules(fields, damage, photo_count: int = 0, is_vehicle=None, photos=None,
              vehicles=None, claimable_count: int = 0, forensics=None, registry=None,
              seen=None, submitted_on=None):
    """registry: the plate's registration record (mock VAHAN), when known.
    seen: {"colour", "make_model", "body_type"} of the claimed vehicle in the photos.
    submitted_on: the date the claim was submitted (India time), for the
    accident-date checks; None skips them."""
    flags = []
    damage = damage or []

    # 1. insured vs RC owner, and claim-form driver vs licence holder
    for rule, field_a, field_b, template in NAME_CHECKS:
        a_value, b_value = get_field(fields, field_a), get_field(fields, field_b)
        # A partial match is an INFO note (identity_notes), never a flag.
        if a_value and b_value and name_match(a_value, b_value) == "mismatch":
            flags.append({
                "rule": rule,
                "severity": "HIGH",
                "message": template.format(a=a_value, b=b_value),
                "evidence": [field_a, field_b],
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

    # A legible plate on the claimed vehicle must belong to the RC. Photos of
    # another vehicle (the other party in a collision) are evidence, not a
    # mismatch; vehicle_mismatch already reports that they are there.
    if reg_rc:
        normalised_rc = _norm_reg(reg_rc)
        claimed = _claimed_photo_names(vehicles)
        for photo in photos or []:
            if not isinstance(photo, dict) or not photo.get("number_plate"):
                continue
            name = str(photo.get("source_photo") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if claimed is not None and name.lower() not in claimed:
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

    # 3. dl_expired_at_loss: the validity that applies to this vehicle
    #    (Non-Transport for a private car), not any date printed.
    dl_exp, dl_field = licence_validity(fields, registry)
    acc_date = parsed_dates["accident_date"]
    if dl_exp and acc_date and dl_exp < acc_date:
        kind = {"licence_expiry_nt": " (non-transport validity)",
                "licence_expiry_tr": " (transport validity)"}.get(dl_field, "")
        flags.append({
            "rule": "dl_expired_at_loss",
            "severity": "HIGH",
            "message": (
                f"Driving licence{kind} expired on {dl_exp.isoformat()}, before the "
                f"accident date {acc_date.isoformat()}."
            ),
            "evidence": [dl_field, "accident_date"],
        })

    # 3b. The RC's registration validity ("Regn. Validity", 15 years for a
    #     private vehicle) on the accident date. Not read: not checked.
    reg_until = parse_date(get_field(fields, "registration_valid_until"))[0]
    if reg_until and acc_date and acc_date > reg_until:
        flags.append({
            "rule": "registration_expired_at_loss",
            "severity": "HIGH",
            "message": (
                f"Registration had expired before the accident: the RC is valid until "
                f"{reg_until.isoformat()}, the accident was on {acc_date.isoformat()}."
            ),
            "evidence": ["accident_date", "registration_valid_until"],
        })

    # 3c. The accident date against the day the claim was submitted.
    if acc_date and submitted_on:
        if acc_date > submitted_on:
            flags.append({
                "rule": "accident_after_submission",
                "severity": "HIGH",
                "message": (
                    f"The accident date {acc_date.isoformat()} is after the claim was "
                    f"submitted ({submitted_on.isoformat()})."
                ),
                "evidence": ["accident_date", "submitted_on"],
                "comparison": {"rows": [
                    {"source": "Accident date", "value": acc_date.isoformat()},
                    {"source": "Claim submitted", "value": submitted_on.isoformat()},
                ], "verdict": "AFTER SUBMISSION"},
            })
        else:
            days = (submitted_on - acc_date).days
            if days > late_intimation_days():
                # MEDIUM and review only: never a rejection by itself.
                flags.append({
                    "rule": "late_intimation",
                    "severity": "MEDIUM",
                    "review": True,
                    "message": (f"Reported {days} days after the accident — ask the customer "
                                "for the reason."),
                    "evidence": ["accident_date", "submitted_on"],
                    "comparison": {"rows": [
                        {"source": "Accident date", "value": acc_date.isoformat()},
                        {"source": "Claim submitted", "value": submitted_on.isoformat()},
                    ], "verdict": f"REPORTED {days} DAYS LATER"},
                })

    # 4. policy_not_active. The registry is the source of truth for the
    #    policy when there is one; the documents only without it.
    p_start = parsed_dates["policy_start_date"]
    p_end = parsed_dates["policy_end_date"]
    if registry:
        flags.extend(registry_flags(fields, registry, seen, acc_date))
    elif acc_date and (p_start or p_end):
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

    return add_comparisons(flags, fields, registry)


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


PARTIAL_NAME_NOTES = {
    "insured_owner_mismatch": ("insured_owner_partial_match", "Insured name on the claim form",
                               "registered owner on the RC"),
    "driver_licence_mismatch": ("driver_licence_partial_match", "Driver named on the claim form",
                                "driving licence holder"),
}


def identity_notes(fields):
    """Information about names, never a flag: the licence holder may
    legitimately differ from the registered owner, and a name written with
    parts left out is noted for the assessor."""
    notes = []
    for rule, field_a, field_b, _ in NAME_CHECKS:
        a_value, b_value = get_field(fields, field_a), get_field(fields, field_b)
        if a_value and b_value and name_match(a_value, b_value) == "partial":
            note_rule, a_label, b_label = PARTIAL_NAME_NOTES[rule]
            notes.append({
                "rule": note_rule,
                "severity": "INFO",
                "message": (
                    f"{a_label} ('{a_value}') matches the {b_label} ('{b_value}') in part: "
                    "one has name parts the other leaves out."
                ),
                "evidence": [field_a, field_b],
            })
    owner = get_field(fields, "owner_name_rc")
    holder = get_field(fields, "holder_name_dl")
    if owner and holder and name_match(owner, holder) == "mismatch":
        notes.append({
            "rule": "driver_not_owner",
            "severity": "INFO",
            "message": (
                f"The driver is not the registered owner: the licence holder is "
                f"'{holder}', the RC owner is '{owner}'. A driver need not be the owner."
            ),
            "evidence": ["holder_name_dl", "owner_name_rc"],
        })
    return notes


# --- comparisons: every flag and note as {source, value} rows plus a verdict --

SOURCE_LABELS = {
    "insured_name_claim_form": "Claim form name",
    "driver_name_claim_form": "Claim form driver",
    "owner_name_rc": "RC owner",
    "holder_name_dl": "Licence holder",
    "registration_number_claim_form": "Claim form registration",
    "registration_number_rc": "RC registration",
    "dl_expiry_date": "Licence valid until",
    "licence_expiry_nt": "Licence valid until (non-transport)",
    "licence_expiry_tr": "Licence valid until (transport)",
    "registration_valid_until": "RC registration valid until",
    "accident_date": "Accident date",
    "policy_start_date": "Policy start (documents)",
    "policy_end_date": "Policy end (documents)",
    "accident_description": "Accident description",
    "registry.plate": "Registered plate (VAHAN)",
    "registry.owner_name": "Registered owner (VAHAN)",
}
# The verdict a flag's rule stands for; anything else is MISMATCH.
RULE_VERDICTS = {
    "dl_expired_at_loss": "EXPIRED",
    "registration_expired_at_loss": "EXPIRED",
    "policy_not_active": "OUTSIDE POLICY",
    "policy_third_party_only": "NOT COVERED",
    "date_undetermined": "UNREADABLE",
    "no_damage_detected": "NO DAMAGE FOUND",
    "not_a_vehicle": "NOT A VEHICLE",
    "vehicle_mismatch": "DIFFERENT VEHICLES",
    "ai_generated_photo": "SUSPICIOUS",
    "duplicate_photo_in_bundle": "ALREADY CLAIMED",
    "other_vehicle_in_photos": "NOTE",
    "driver_not_owner": "DIFFERENT (ALLOWED)",
}


def _value(fields, registry, key):
    if key.startswith("registry."):
        return (registry or {}).get(key.split(".", 1)[1])
    return get_field(fields, key)


def _rows(fields, registry, keys):
    return [{"source": SOURCE_LABELS.get(key, key), "value": _value(fields, registry, key)}
            for key in keys]


def _period(start, end, insurer=None):
    text = f"{start or '?'} to {end or '?'}"
    return f"{text} · {insurer}" if insurer else text


def comparison(item, fields, registry=None):
    """{rows: [{source, value}], verdict} for a flag or note, from its rule,
    its evidence and the fields; None when nothing can be compared."""
    rule = item.get("rule")
    evidence = [str(value) for value in item.get("evidence") or []]
    verdict = RULE_VERDICTS.get(rule)
    if verdict is None:
        verdict = "PARTIAL MATCH" if str(rule or "").endswith("_partial_match") else "MISMATCH"
    registry = registry or {}
    rows = None
    if rule in ("policy_not_active",):
        if "registry.policy_start" in evidence:
            period = _period(registry.get("policy_start"), registry.get("policy_end"),
                             registry.get("insurer"))
            rows = [{"source": "Accident date", "value": get_field(fields, "accident_date")},
                    {"source": "Registered policy period (VAHAN)", "value": period}]
        else:
            rows = [{"source": "Accident date", "value": get_field(fields, "accident_date")},
                    {"source": "Policy period (documents)", "value": _period(
                        get_field(fields, "policy_start_date"),
                        get_field(fields, "policy_end_date"))}]
    elif rule == "policy_third_party_only":
        rows = [{"source": "Registered policy (VAHAN)", "value": "Third party only"},
                {"source": "This claim", "value": "Own damage"}]
    elif rule == "vehicle_identity_mismatch" and len(evidence) >= 3:
        photo = evidence[0].replace("\\", "/").rsplit("/", 1)[-1]
        rows = [{"source": f"Plate in {photo}", "value": evidence[1]},
                {"source": "RC registration", "value": evidence[2]}]
    elif rule == "ai_generated_photo" and len(evidence) >= 2:
        rows = [{"source": "Photo", "value": evidence[0]},
                {"source": "AI-generation score", "value": evidence[1]}]
    elif rule in ("vehicle_mismatch", "other_vehicle_in_photos"):
        rows = [{"source": f"Vehicle {index}", "value": label}
                for index, label in enumerate(evidence, 1)]
    elif rule == "duplicate_photo_in_bundle":
        rows = [{"source": "Excluded photo", "value": name} for name in evidence]
    elif rule == "damage_narrative_mismatch":
        parts = [value for value in evidence if value != "accident_description"]
        rows = [{"source": "Accident description",
                 "value": get_field(fields, "accident_description")},
                {"source": "Damage found", "value": ", ".join(parts)}]
    elif rule == "no_damage_detected":
        rows = [{"source": "Visible damage in photos", "value": "none found"}]
    elif rule == "not_a_vehicle":
        rows = [{"source": "Vehicle in photos", "value": "none"}]
    elif evidence and all(key in SOURCE_LABELS or key.startswith("registry.")
                          for key in evidence):
        rows = _rows(fields, registry, [key for key in evidence if key in SOURCE_LABELS])
    if not rows:
        return None
    for row in rows:
        row["value"] = "not read" if row["value"] in (None, "") else str(row["value"])
    return {"rows": rows, "verdict": verdict}


def add_comparisons(items, fields, registry=None):
    """Each flag or note carries its comparison; one made at creation is kept."""
    for item in items or []:
        if isinstance(item, dict) and not item.get("comparison"):
            built = comparison(item, fields, registry)
            if built:
                item["comparison"] = built
    return items


PASSED_TITLES = {
    "insured_owner_mismatch": "Insured is the registered owner",
    "driver_licence_mismatch": "Driver is the licence holder",
}


def passed_checks(fields, registry=None, flags=(), notes=(), photos=()):
    """The cross-checks that ran and agreed, as MATCH rows, so the console
    shows what was compared and found consistent, not only what failed."""
    raised = {item.get("rule") for item in list(flags or []) + list(notes or [])
              if isinstance(item, dict)}
    registry = registry or {}
    checks = []

    def add(rule, title, keys=None, rows=None, verdict="MATCH"):
        rows = rows or _rows(fields, registry, keys)
        checks.append({"rule": rule, "title": title, "severity": "PASS",
                       "comparison": {"rows": rows, "verdict": verdict}})

    for rule, field_a, field_b, _ in NAME_CHECKS:
        a_value, b_value = get_field(fields, field_a), get_field(fields, field_b)
        if a_value and b_value and name_match(a_value, b_value) == "match":
            add(rule.replace("_mismatch", "_match"), PASSED_TITLES[rule], [field_a, field_b])
    reg_form = get_field(fields, "registration_number_claim_form")
    reg_rc = get_field(fields, "registration_number_rc")
    if reg_form and reg_rc and _norm_reg(reg_form) == _norm_reg(reg_rc):
        add("registration_match", "Registration number agrees",
            ["registration_number_claim_form", "registration_number_rc"])
    if registry.get("plate") and reg_rc and _norm_reg(reg_rc) == _norm_reg(registry["plate"]):
        add("registry_rc_match", "RC matches the registry",
            ["registration_number_rc", "registry.plate"])
    owner_rc = get_field(fields, "owner_name_rc")
    if owner_rc and registry.get("owner_name") and name_match(
            owner_rc, registry["owner_name"]) == "match":
        add("registry_owner_match", "RC owner is the registered owner",
            ["owner_name_rc", "registry.owner_name"])
    if reg_rc:
        plates = sorted({str(photo["number_plate"]) for photo in photos or []
                         if isinstance(photo, dict) and photo.get("number_plate")
                         and _norm_reg(photo["number_plate"]) == _norm_reg(reg_rc)})
        if plates and "vehicle_identity_mismatch" not in raised:
            add("vehicle_identity_match", "Plate in the photos matches the RC", rows=[
                {"source": "Plate in photos", "value": ", ".join(plates)},
                {"source": "RC registration", "value": str(reg_rc)}])
    dl_expiry, dl_field = licence_validity(fields, registry)
    accident = parse_date(get_field(fields, "accident_date"))[0]
    if dl_expiry and accident and dl_expiry >= accident:
        add("dl_valid_at_loss", "Licence valid on the accident date",
            [dl_field, "accident_date"], verdict="VALID")
    reg_until = parse_date(get_field(fields, "registration_valid_until"))[0]
    if reg_until and accident and accident <= reg_until:
        add("registration_valid_at_loss", "Registration valid on the accident date",
            ["accident_date", "registration_valid_until"], verdict="VALID")
    if accident and "policy_not_active" not in raised:
        if registry.get("policy_start") and registry.get("policy_end"):
            start = parse_date(registry["policy_start"])[0]
            end = parse_date(registry["policy_end"])[0]
            if start and end and start <= accident <= end:
                add("policy_active", "Accident inside the policy period", rows=[
                    {"source": "Accident date", "value": str(get_field(fields, "accident_date"))},
                    {"source": "Registered policy period (VAHAN)", "value": _period(
                        registry["policy_start"], registry["policy_end"],
                        registry.get("insurer"))}], verdict="COVERED")
        else:
            start = parse_date(get_field(fields, "policy_start_date"))[0]
            end = parse_date(get_field(fields, "policy_end_date"))[0]
            if start and end and start <= accident <= end:
                add("policy_active", "Accident inside the policy period", rows=[
                    {"source": "Accident date", "value": str(get_field(fields, "accident_date"))},
                    {"source": "Policy period (documents)", "value": _period(
                        get_field(fields, "policy_start_date"),
                        get_field(fields, "policy_end_date"))}], verdict="COVERED")
    for check in checks:
        for row in check["comparison"]["rows"]:
            row["value"] = "not read" if row["value"] in (None, "") else str(row["value"])
    return checks


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
        # HIGH, and any flag marked review (late intimation, an RC number that
        # differs from the registry): an assessor decides. Never a rejection.
        if isinstance(flag, dict) and (str(flag.get("severity")).upper() == "HIGH"
                                       or flag.get("review") is True):
            reasons.append(flag.get("message") or f"Flag: {flag.get('rule')}")

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
        if (isinstance(field, dict) and field.get("status") == "unreadable"
                and field.get("name") not in SILENT_FIELDS):
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
