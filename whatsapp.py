import asyncio
import hashlib
import json
import os
import re
import time
import traceback
import uuid
from collections import deque

import httpx
from dotenv import load_dotenv

load_dotenv()

import db
import registry
from gemini import classify_image, extract_fields
from rules import (
    DOCUMENT_LABELS,
    establish_documents,
    reported_confidence,
    route_classification,
)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_WABA_ID = os.getenv("WHATSAPP_WABA_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "")

BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

HTTP_TIMEOUT = 30.0
MAX_PROCESSED = 500
PENDING_CHOICE_TIMEOUT = 30 * 60
MAX_IMAGES_PER_CLAIM = 8

PROCESSED = set()
_PROCESSED_ORDER = deque()
# Per-number lock for one in-flight analysis. It stays in process memory on
# purpose: it only has to outlive that analysis, and a restart has no analysis
# left to protect.
ANALYSING_IDS = set()
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
}
ANALYSIS_BUSY_REPLY = "Your claim is still being analysed — you will get the result here."
NEW_CLAIM_WHILE_BUSY = (
    "Your previous claim is still being analysed — these photos will start a new claim."
)
NEW_DOCUMENT_WHILE_BUSY = (
    "Your previous claim is still being analysed — this document will start a new claim."
)
UNHANDLED_WITH_MEDIA_REPLY = (
    "That type of message cannot be read here. Send photos or a PDF of your "
    "documents, then reply DONE."
)
# While an analysis runs, new media gets one notice (not one per photo), and
# media identical to what is being analysed is held here and added back as
# soon as the analysis releases its rows, so it starts the next claim.
BUSY_NOTICE_SENT = set()
DEFERRED_MEDIA = {}
CHOICE_CLEARED_REPLY = (
    "The previous vehicle choice has been cleared. This photo starts a new claim. "
    "Reply DONE when you are done."
)
ANALYSING_REPLY = "Analysing your claim. This usually takes under a minute."
ACCEPTED_MEDIA = (
    "Send a photo of the damaged vehicle, or a photo or PDF of your claim documents."
)
UNSUPPORTED_REPLIES = {
    "sticker": f"Stickers are not accepted. {ACCEPTED_MEDIA}",
    "video": f"Videos are not accepted. {ACCEPTED_MEDIA}",
    "audio": f"Voice notes are not accepted. {ACCEPTED_MEDIA}",
    "voice": f"Voice notes are not accepted. {ACCEPTED_MEDIA}",
    "location": f"Locations are not accepted. {ACCEPTED_MEDIA}",
    "contacts": f"Contact cards are not accepted. {ACCEPTED_MEDIA}",
}
UNREADABLE_IMAGE_REPLY = (
    "I could not read that photo. Please send a clear JPG, PNG, or HEIC photo "
    "of the damaged vehicle."
)
UNREADABLE_PDF_REPLY = (
    "I could not read that PDF. Please send it again, or send a clear photo of each page."
)
DOCUMENT_FORMAT_REPLY = (
    "I can read PDF documents and photos. Please send this document as a PDF "
    "or a clear photo."
)
IMAGE_LIMIT_REPLY = "Maximum 8 images per claim — keeping the first 8."


def _auth_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


# Phone numbers never reach the logs. A wamid message id is base64 that embeds
# the sender's number, so it is hashed too.
_LONG_NUMBER = re.compile(r"\+?\d[\d\s\-]{8,}\d")
_WAMID = re.compile(r"wamid\.[A-Za-z0-9+/=_\-]+")


def log_safe(text):
    """Text for a log line: message ids and long digit runs removed."""
    return _LONG_NUMBER.sub("[number]", _WAMID.sub("wamid.[id]", str(text)))


def who(wa_id):
    """A sender in a log line: the last four digits, as on the console badge."""
    digits = "".join(character for character in str(wa_id or "") if character.isdigit())
    return f"...{digits[-4:]}" if digits else "unknown sender"


def message_ref(message_id):
    """A stable short reference for a message id, for matching log lines."""
    return "msg-" + hashlib.sha256(str(message_id or "").encode("utf-8")).hexdigest()[:10]


def _safe_url(url):
    """Media CDN links carry a signed query string - strip it before logging."""
    return str(url or "").split("?")[0]


def verify(mode, token, challenge):
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return challenge
    return None


def record_inbound(wa_id, timestamp=None):
    if not wa_id:
        return
    try:
        received_at = float(timestamp) if timestamp is not None else time.time()
    except (TypeError, ValueError):
        received_at = time.time()
    db.touch_last_inbound(str(wa_id), received_at)


def last_inbound_at(wa_id):
    if not wa_id:
        return None
    raw = db.get_last_inbound(str(wa_id))
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class _LastInbound:
    """Test hook. Timestamps themselves live in wa_last_inbound."""

    def clear(self):
        db.clear_last_inbound()


LAST_INBOUND = _LastInbound()


def _blank_session(now=None):
    return {
        "pending_choice": None,
        "pending_prompt": None,
        "awaiting_docs": False,
        "missing_docs": [],
        "limit_notified": False,
        "analysing": False,
        "analysis_cutoff": None,
        "updated": time.time() if now is None else now,
    }


# Derived from wa_session_media on every load. Never stored in wa_sessions:
# media arrival is append-only, so no handler can overwrite another's photos.
_DERIVED_KEYS = ("photos", "docs", "doc_count")


def _state_json(session):
    state = {key: value for key, value in dict(session).items() if key not in _DERIVED_KEYS}
    return json.dumps(state, ensure_ascii=False)


def _decode_state(raw):
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in _DERIVED_KEYS:
        data.pop(key, None)
    return data


def _persist(wa_id, session):
    updated = session.get("updated")
    if updated is None:
        updated = time.time()
    db.put_session(str(wa_id), _state_json(session), updated)


def _load_state(wa_id):
    return _decode_state(db.get_session(str(wa_id)))


def _pending_rows(session, rows):
    """Rows the next DONE would analyse: everything after an in-flight snapshot."""
    cutoff = session.get("analysis_cutoff") if session.get("analysing") else None
    if cutoff is None:
        return list(rows)
    return [row for row in rows if int(row["id"]) > int(cutoff)]


def _attach_media(session, rows):
    pending = _pending_rows(session, rows)
    dict.__setitem__(session, "photos", [row["media_id"] for row in pending if row["kind"] == "photo"])
    dict.__setitem__(session, "docs", [row["media_id"] for row in pending if row["kind"] == "document_page"])
    dict.__setitem__(session, "doc_count", len({
        row["sha256"] for row in pending if row["kind"] == "document_page"
    }))
    return session


class _BoundSession(dict):
    """Session state that writes itself back when a field is set.

    photos/docs are a read-only view of wa_session_media; add media with
    add_session_media, never by assigning these keys.
    """

    def __init__(self, wa_id, data):
        super().__init__(data)
        self._wa_id = str(wa_id)

    def __setitem__(self, key, value):
        if key in _DERIVED_KEYS:
            raise TypeError(f"session {key} is append-only; use add_session_media")
        super().__setitem__(key, value)
        _persist(self._wa_id, self)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        for key in changes:
            if key in _DERIVED_KEYS:
                raise TypeError(f"session {key} is append-only; use add_session_media")
        for key, value in changes.items():
            super().__setitem__(key, value)
        _persist(self._wa_id, self)


def _view(wa_id, state=None):
    state = _load_state(wa_id) if state is None else state
    session = _BoundSession(wa_id, state if state is not None else _blank_session())
    return _attach_media(session, db.list_session_media(wa_id))


class _SessionStore:
    """Mapping interface over wa_sessions (state) and wa_session_media (media)."""

    def __getitem__(self, wa_id):
        state = _load_state(wa_id)
        rows = db.list_session_media(wa_id)
        if state is None and not rows:
            raise KeyError(wa_id)
        session = _BoundSession(wa_id, state if state is not None else _blank_session())
        return _attach_media(session, rows)

    def __setitem__(self, wa_id, session):
        data = dict(session or {})
        for key in _DERIVED_KEYS:
            if data.pop(key, None):
                raise ValueError(f"session {key} cannot be assigned; use add_session_media")
        data = {**_blank_session(), **data}
        if data.get("updated") is None:
            data["updated"] = time.time()
        _persist(wa_id, data)

    def __delitem__(self, wa_id):
        db.delete_session(wa_id)

    def __contains__(self, wa_id):
        return _load_state(wa_id) is not None or bool(db.list_session_media(wa_id))

    def get(self, wa_id, default=None):
        try:
            return self[wa_id]
        except KeyError:
            return default

    def pop(self, wa_id, *args):
        """End the conversation state. Media rows are left alone: any still
        present arrived after the state being ended and start the next claim."""
        session = _load_state(wa_id)
        if session is None:
            if args:
                return args[0]
            raise KeyError(wa_id)
        db.delete_session(wa_id)
        return session

    def clear(self):
        db.clear_sessions()
        db.clear_session_media()

    def items(self):
        wa_ids = {str(wa_id) for wa_id, _ in db.list_sessions()}
        wa_ids |= set(db.session_media_activity())
        for wa_id in sorted(wa_ids):
            yield wa_id, _view(wa_id)


SESSIONS = _SessionStore()


def add_session_media(wa_id, media_id, kind, sha256, page=0, source_bytes=None, slot=None):
    """Append one stored item to a session. None means it was already there."""
    return db.add_session_media(wa_id, media_id, kind, sha256, page, source_bytes, slot=slot)


def is_analysing(wa_id):
    """An analysis is running for this number in this process."""
    return wa_id in ANALYSING_IDS


def take_busy_notice(wa_id):
    """True once per analysis: the first new media item gets the notice."""
    if wa_id in BUSY_NOTICE_SENT:
        return False
    BUSY_NOTICE_SENT.add(wa_id)
    return True


def defer_media(wa_id, media_id, kind, sha256, page, source_bytes):
    """Hold media identical to rows being analysed (the unique key refuses it
    while those rows exist) until the analysis releases them."""
    held = DEFERRED_MEDIA.setdefault(wa_id, [])
    if all((item[2], item[3]) != (sha256, page) for item in held):
        held.append((media_id, kind, sha256, page, source_bytes))


def restore_deferred_media(wa_id):
    """Add held media back as the next claim's rows. Returns how many."""
    restored = 0
    for media_id, kind, sha256, page, source_bytes in DEFERRED_MEDIA.pop(wa_id, []):
        if add_session_media(wa_id, media_id, kind, sha256, page, source_bytes) is not None:
            restored += 1
    return restored


def _last_activity(state, rows):
    times = [float(row["received_at"] or 0) for row in rows]
    if state is not None:
        times.append(float(state.get("updated") or 0))
    return max(times) if times else 0.0


def _expire_loaded(wa_id, state, rows, now):
    """Expire a loaded session if it has been idle for 30 minutes.

    Media and orphaned blobs are deleted. When media is dropped, a small state
    row keeps an expiry notice so the customer is told the next time they
    write, however much later that is. Returns (notice, state, rows) as they
    stand afterwards.
    """
    if state is None and not rows:
        return None, state, rows
    if wa_id in ANALYSING_IDS:
        return None, state, rows
    if now - _last_activity(state, rows) <= PENDING_CHOICE_TIMEOUT:
        return None, state, rows
    if state is not None and state.get("expired_notice") and not rows:
        return None, state, rows
    dropped = db.delete_session_media(wa_id, [row["id"] for row in rows]) if rows else []
    notice = {
        "photos": sum(1 for row in dropped if row["kind"] == "photo"),
        "docs": len({row["sha256"] for row in dropped if row["kind"] != "photo"}),
        # A guided conversation part-way through is told it timed out too.
        "stage": (state or {}).get("stage") if (state or {}).get("stage") != "menu" else None,
    }
    if notice["photos"] or notice["docs"] or notice["stage"]:
        tombstone = {"expired_notice": notice, "updated": now}
        db.put_session(str(wa_id), json.dumps(tombstone), now)
        return notice, tombstone, []
    db.delete_session(wa_id)
    return None, None, []


def expire_idle_session(wa_id, now=None):
    now = time.time() if now is None else now
    notice, _, _ = _expire_loaded(
        wa_id, _load_state(wa_id), db.list_session_media(wa_id), now
    )
    return notice


def expiry_text(notice):
    photos, docs = notice.get("photos"), notice.get("docs")
    text = "Your session timed out after 30 minutes of inactivity."
    if photos and docs:
        text += " The photos and documents you sent were removed."
    elif docs:
        text += " The documents you sent were removed."
    elif photos:
        text += " The photos you sent were removed."
    return text


def take_expiry_notice(wa_id):
    """Expire this number's idle session and return the notice to send, once."""
    _, state, _ = _expire_loaded(
        wa_id, _load_state(wa_id), db.list_session_media(wa_id), time.time()
    )
    if not state or not state.get("expired_notice"):
        return None
    db.delete_session(wa_id)
    return expiry_text(state["expired_notice"])


SWEEP_INTERVAL = 5 * 60
_LAST_SWEEP = {"at": 0.0}


def sweep_idle_sessions(now=None):
    """Expire every idle session and purge orphaned session blobs, at most once
    per SWEEP_INTERVAL. Never raises: a failed sweep must not lose a message."""
    now = time.time() if now is None else now
    if now - _LAST_SWEEP["at"] < SWEEP_INTERVAL:
        return
    _LAST_SWEEP["at"] = now
    try:
        wa_ids = {str(wa_id) for wa_id, _ in db.list_sessions()}
        wa_ids |= set(db.session_media_activity())
        for wa_id in wa_ids:
            expire_idle_session(wa_id, now)
        db.purge_orphan_session_media(PENDING_CHOICE_TIMEOUT)
    except Exception:
        print("[whatsapp] session sweep failed:", flush=True)
        traceback.print_exc()


def get_session(wa_id):
    """Return the live session (state plus a view of its media). An idle one is
    expired first; a stale analysing flag from a dead process is cleared."""
    _, state, rows = _expire_loaded(
        wa_id, _load_state(wa_id), db.list_session_media(wa_id), time.time()
    )
    if state is not None and state.get("expired_notice"):
        state = None
    session = _BoundSession(wa_id, {**_blank_session(), **(state or {})})
    if state is not None and state.get("analysing") and wa_id not in ANALYSING_IDS:
        # The analysis lock does not survive a restart, so this flag cannot either.
        session.update(analysing=False, analysis_cutoff=None)
    return _attach_media(session, rows)


def reset_session(wa_id):
    """Fresh conversation state. Media rows are untouched."""
    session = _BoundSession(wa_id, _blank_session())
    _persist(wa_id, session)
    return _attach_media(session, db.list_session_media(wa_id))


def normalised_command(text):
    value = str(text or "").strip().lower().replace("।", " ")
    value = " ".join(value.split())
    return value.strip(".,!?;:")


def is_skip_command(text):
    return normalised_command(text) == "skip"


def remember_message(message_id):
    """Returns True the first time a message id is seen, False on redelivery.

    The id is stored in SQLite for 48 hours so a webhook redelivery after a
    process restart cannot create a second claim.
    """
    if not message_id or message_id in PROCESSED:
        return False
    try:
        from db import remember_processed_message

        if not remember_processed_message(message_id):
            PROCESSED.add(message_id)
            return False
    except Exception:
        print("[whatsapp] processed-message store failed:", flush=True)
        traceback.print_exc()
    PROCESSED.add(message_id)
    _PROCESSED_ORDER.append(message_id)
    while len(_PROCESSED_ORDER) > MAX_PROCESSED:
        PROCESSED.discard(_PROCESSED_ORDER.popleft())
    return True


async def download_media(media_obj):
    """Returns raw bytes, or None on any failure. Never raises."""
    try:
        if not isinstance(media_obj, dict):
            print("[whatsapp] download_media: media object is not a dict", flush=True)
            return None

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            url = media_obj.get("url")

            if not url:
                media_id = media_obj.get("id")
                if not media_id:
                    print("[whatsapp] download_media: no url and no id", flush=True)
                    return None
                lookup = f"{BASE}/{media_id}"
                print(f"[whatsapp] GET {log_safe(lookup)}", flush=True)
                meta = await client.get(lookup, headers=_auth_headers())
                print(f"[whatsapp] media lookup status {meta.status_code}", flush=True)
                if meta.status_code != 200:
                    print(f"[whatsapp] media lookup failed: {log_safe(meta.text[:300])}",
                          flush=True)
                    return None
                try:
                    url = (meta.json() or {}).get("url")
                except Exception:
                    url = None
                if not url:
                    print("[whatsapp] media lookup returned no url", flush=True)
                    return None

            print(f"[whatsapp] GET {log_safe(_safe_url(url))} (media bytes)", flush=True)
            resp = await client.get(url, headers=_auth_headers())
            print(f"[whatsapp] media download status {resp.status_code}", flush=True)
            if resp.status_code != 200:
                print(f"[whatsapp] media download failed: {log_safe(resp.text[:300])}",
                      flush=True)
                return None
            return resp.content
    except Exception:
        print("[whatsapp] download_media failed:", flush=True)
        traceback.print_exc()
        return None


async def send_text(to, body):
    url = f"{BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                url, headers={**_auth_headers(), "Content-Type": "application/json"}, json=payload
            )
        # Meta's response echoes the recipient's number; only an error body is
        # logged, and then without numbers.
        detail = "" if resp.is_success else f": {log_safe(resp.text[:300])}"
        print(f"[whatsapp] send_text to {who(to)} -> {resp.status_code}{detail}", flush=True)
        return resp.is_success
    except Exception:
        print("[whatsapp] send_text failed:", flush=True)
        traceback.print_exc()
        return False


async def subscribe_app():
    url = f"{BASE}/{WHATSAPP_WABA_ID}/subscribed_apps"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=_auth_headers())
        print(f"[whatsapp] subscribe_app -> {resp.status_code}: {log_safe(resp.text[:300])}",
              flush=True)
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "body": resp.text[:500]}
    except Exception as e:
        print("[whatsapp] subscribe_app failed:", flush=True)
        traceback.print_exc()
        return {"error": str(e)}


def _pretty(name):
    text = str(name or "").replace("_", " ").strip()
    return text.capitalize() if text else "Unknown part"


def _rupees(value):
    try:
        return f"Rs {int(value):,}"
    except Exception:
        return "Rs 0"


def _stage_seconds(payload, stage):
    for entry in payload.get("timings") or []:
        if entry.get("stage") == stage:
            return entry.get("seconds")
    return None


def indicative_range(estimate):
    """Indicative (unconfirmed) range, one line per part as build_estimate set it.

    Estimates stored before per-part dedup lack the totals; summing their
    unconfirmed items is the only figure they carry.
    """
    estimate = estimate or {}
    if "indicative_high" in estimate:
        return estimate.get("indicative_low") or 0, estimate.get("indicative_high") or 0
    unconfirmed = estimate.get("unconfirmed") or []
    return (
        sum(item.get("low") or 0 for item in unconfirmed),
        sum(item.get("high") or 0 for item in unconfirmed),
    )


def _estimate_range(estimate):
    estimate = estimate or {}
    unconfirmed = estimate.get("unconfirmed") or []
    if not estimate.get("total_high") and unconfirmed:
        low, high = indicative_range(estimate)
        return (
            f"No confirmed estimate yet; indicative range "
            f"{_rupees(low)} – {_rupees(high)}, pending assessor review"
        )
    return (
        f"{_rupees(estimate.get('total_low'))} "
        f"– {_rupees(estimate.get('total_high'))}"
    )


def _undetermined_lines(items):
    """What the customer can do to complete the claim. Only entries marked
    customer_actionable are shown, as requests; forensics, provider and model
    details are for the assessor console only."""
    requests = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("audience") != "customer_actionable":
            continue
        request = str(item.get("request") or "").strip()
        if request and request not in requests:
            requests.append(request)
    if not requests:
        return []
    return ["", "*To complete your claim:*"] + [
        f"- {request[0].upper()}{request[1:].rstrip('.')}." for request in requests
    ]


def _single_vehicle_reply(claim_id, vehicle, excluded_count=0, total_photos=None,
                          undetermined=None, decision=None, decision_reasons=None):
    estimate = vehicle.get("estimate") or {}
    line_items = estimate.get("line_items") or []
    lines = []

    if excluded_count:
        total = total_photos if total_photos is not None else len(vehicle.get("photos") or [])
        remaining = max(0, total - excluded_count)
        submitted_verb = "was" if excluded_count == 1 else "were"
        excluded_verb = "has" if excluded_count == 1 else "have"
        remaining_word = "photo" if remaining == 1 else "photos"
        lines += [
            (
                f"{excluded_count} of your {total} photos {submitted_verb} already submitted "
                f"in an earlier claim and {excluded_verb} been excluded. The assessment "
                f"below uses the "
                f"remaining {remaining} {remaining_word}."
            ),
            "",
        ]

    lines += [
        f"*Claim {claim_id} · {vehicle.get('label') or 'Vehicle'}*",
        "",
        "Damage found:",
    ]
    for item in line_items:
        lines.append(
            f"- {_pretty(item.get('part'))} — "
            f"{str(item.get('damage_type') or 'damage').replace('_', ' ')} "
            f"({item.get('severity')}): {_rupees(item.get('low'))} "
            f"– {_rupees(item.get('high'))}"
        )

    unpriced = estimate.get("unpriced") or []
    for item in unpriced:
        lines.append(f"- {_pretty(item.get('part'))} — manual pricing required")

    if not line_items and not unpriced and estimate.get("unconfirmed"):
        lines.append("- Possible damage detected — excluded pending manual review")
    elif not line_items and not unpriced:
        lines.append("- Damage detected — manual pricing required")

    unconfirmed = estimate.get("unconfirmed") or []
    if not estimate.get("total_high") and unconfirmed:
        indicative_low, indicative_high = indicative_range(estimate)
        lines += [
            "",
            "*No confirmed estimate yet*",
            (
                f"Indicative range {_rupees(indicative_low)} – "
                f"{_rupees(indicative_high)}, pending assessor review."
            ),
        ]
    else:
        lines += ["", f"*Estimate: {_estimate_range(estimate)}*"]
    lines += _undetermined_lines(undetermined)
    if decision == "needs_review":
        # The reasons stay in the console: they can name forensic checks a
        # customer must not see. What the customer can fix is listed above.
        lines += ["", "Flagged for manual review. An assessor will check your claim."]
    elif excluded_count:
        lines += ["", "Flagged for manual review."]
    return "\n".join(lines)


def _vehicle_summary(vehicle):
    estimate = vehicle.get("estimate") or {}
    line_items = estimate.get("line_items") or []
    first = line_items[0] if line_items else {}
    part = str(first.get("part") or "damage").replace("_", " ")
    damage_type = str(first.get("damage_type") or "detected").replace("_", " ")
    return f"{part} {damage_type}, {_estimate_range(estimate)}"


def _number_word(number, title=False):
    words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    value = words.get(number, str(number))
    return value.title() if title else value


def _selection_reply(claim_id, vehicles):
    count = len(vehicles)
    lines = [
        f"*{_number_word(count, title=True)} different vehicles in one claim*",
        "",
        (
            f"Your photos show {_number_word(count)} vehicles. A claim covers one vehicle, "
            "so please choose:"
        ),
        "",
    ]
    for index, vehicle in enumerate(vehicles, 1):
        lines.append(
            f"*{index} · {vehicle.get('label') or f'Vehicle {index}'}* "
            f"— {_vehicle_summary(vehicle)}"
        )
    choices = " or ".join(f"*{index}*" for index in range(1, count + 1))
    lines += ["", f"Reply {choices} to continue with that vehicle."]
    return "\n".join(lines)


def _not_vehicle_reply(payload):
    photos = {
        os.path.basename(str(photo.get("source_photo") or "")).lower(): photo
        for photo in (payload.get("photos") or [])
        if isinstance(photo, dict)
    }
    descriptions = []
    for vehicle in payload.get("vehicles") or []:
        if vehicle.get("status") != "not_vehicle":
            continue
        text = next(
            (
                photos.get(os.path.basename(str(name)).lower(), {}).get("description")
                for name in vehicle.get("photos") or []
                if photos.get(os.path.basename(str(name)).lower(), {}).get("description")
            ),
            None,
        )
        if text:
            descriptions.append((vehicle.get("label"), text))

    fallback = payload.get("vehicle_description") or ""
    lines = ["*Cannot process this image*", ""]
    if len(descriptions) == 1:
        lines.append(f"What I saw: {descriptions[0][1]}")
    elif descriptions:
        for label, description in descriptions:
            lines.append(f"What I saw in {label or 'this photo'}: {description}")
    elif fallback:
        lines.append(f"What I saw: {fallback}")
    else:
        lines.append("This photo does not appear to show a motor vehicle.")
    lines += ["", "Please send a clear photo of the *damaged vehicle* to start your claim."]
    return "\n".join(lines)


def _duplicate_reply(payload):
    claim_id = payload.get("claim_id", "unknown")
    match = ""
    for entry in payload.get("forensics") or []:
        if entry.get("check") == "duplicate" and entry.get("verdict") == "suspicious":
            match = entry.get("detail") or ""
            break
    seconds = _stage_seconds(payload, "stage0_duplicate")
    lines = [
        "*Claim rejected - duplicate photo*",
        "",
        "This photo has already been submitted with an earlier claim.",
    ]
    if match:
        lines.append(f"Match: {match}")
    lines += [
        f"Reference: *{claim_id}*",
        "",
        "Your claim has been sent for *manual review*.",
    ]
    if seconds is not None:
        lines.append(f"Detected at stage 0 in {seconds}s - no AI analysis was run.")
    return "\n".join(lines)


OPEN_CLAIM_LINE = (
    "Send any missing documents here and they will be added to this claim. "
    "Reply *END* to close this session or *MENU* for more."
)


def case_footer(payload):
    """Tracking ID, assessor and next step, for a claim that has them."""
    if not payload.get("tracking_id"):
        return ""
    lines = [f"Tracking ID: *{payload['tracking_id']}*"]
    assessor = payload.get("assessor") or {}
    if assessor.get("name"):
        phone = f", {assessor['phone']}" if assessor.get("phone") else ""
        lines.append(f"Assessor: {assessor['name']}{phone}")
    if payload.get("next_step"):
        lines.append(f"Next: {payload['next_step']}")
    return "\n".join(lines) + "\n\n" + OPEN_CLAIM_LINE


def format_reply(payload):
    result = _result_text(payload)
    footer = case_footer(payload)
    return f"{result}\n\n{footer}" if footer else result


def _result_text(payload):
    status = payload.get("status")
    stopped_at = payload.get("stopped_at")
    claim_id = payload.get("claim_id", "unknown")

    if status == "rejected" and stopped_at == "duplicate":
        return _duplicate_reply(payload)

    if status == "rejected" and stopped_at == "not_a_vehicle":
        return _not_vehicle_reply(payload)

    if status == "error" and stopped_at == "ai_unavailable":
        return "\n".join([
            "*Analysis unavailable*",
            "",
            "We could not complete the automated assessment right now.",
            f"Your claim *{claim_id}* has been escalated to a human assessor.",
            "",
            "You do not need to resend anything.",
        ])

    if status == "analysed":
        vehicles = [
            vehicle for vehicle in (payload.get("vehicles") or [])
            if isinstance(vehicle, dict)
        ]
        claimable = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "claimable"
        ]
        claimable_count = payload.get("claimable_count")
        if not isinstance(claimable_count, int):
            claimable_count = len(claimable)
        selected = payload.get("selected_vehicle_group")
        if selected is not None:
            # The registered vehicle was matched: report on it, ask nothing.
            claimable = [vehicle for vehicle in claimable if vehicle.get("group") == selected]
            claimable_count = len(claimable)
        duplicate = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "all_duplicate"
        ]
        no_damage = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "no_damage"
        ]
        not_vehicle = [
            vehicle for vehicle in vehicles if vehicle.get("status") == "not_vehicle"
        ]

        if claimable_count > 1:
            return _selection_reply(claim_id, claimable)

        if claimable_count == 1 and duplicate:
            lines = ["*One vehicle cannot be claimed*", ""]
            for vehicle in duplicate:
                lines.append(
                    f"*{vehicle.get('label') or 'Vehicle'}* — the photos of this vehicle "
                    "were already submitted in an earlier claim. It cannot be claimed again."
                )
            vehicle = claimable[0]
            lines += [
                "",
                (
                    f"*{vehicle.get('label') or 'Vehicle'}* — assessed and claimable, "
                    f"{_estimate_range(vehicle.get('estimate'))}."
                ),
                "",
                (
                    f"Reply *YES* to continue with the "
                    f"{vehicle.get('label') or 'vehicle'}, or send fresh photos."
                ),
            ]
            return "\n".join(lines)

        if claimable_count == 1:
            excluded_count = len(payload.get("excluded_photos") or [])
            total_photos = len(payload.get("photos") or []) + excluded_count
            reply = _single_vehicle_reply(
                claim_id,
                claimable[0],
                excluded_count=excluded_count,
                total_photos=total_photos,
                undetermined=payload.get("undetermined"),
                decision=payload.get("decision"),
                decision_reasons=payload.get("decision_reasons"),
            )
            notes = []
            for vehicle in no_damage:
                count = len(vehicle.get("photos") or [])
                if count == 1:
                    notes.append(
                        f"Note: your photo of the {vehicle.get('label')} shows no visible "
                        "damage, so it has not been included."
                    )
                else:
                    notes.append(
                        f"Note: your photos of the {vehicle.get('label')} show no visible "
                        "damage, so they have not been included."
                    )
            return reply + (("\n\n" + "\n".join(notes)) if notes else "")

        statuses = [vehicle.get("status") for vehicle in vehicles]
        if vehicles and all(value == "no_damage" for value in statuses):
            photo_count = sum(len(vehicle.get("photos") or []) for vehicle in vehicles)
            target = "either vehicle" if len(vehicles) == 2 else "any vehicle"
            return "\n".join([
                "*No visible damage found*",
                "",
                (
                    f"We examined {photo_count} photos and could not identify damage "
                    f"on {target}."
                ),
                (
                    "Send clearer photos showing the damaged area to start a new "
                    "claim. An assessor will also review this one."
                ),
            ])

        if vehicles and all(value == "all_duplicate" for value in statuses):
            return _duplicate_reply(payload)

        if vehicles and all(value == "not_vehicle" for value in statuses):
            return _not_vehicle_reply(payload)

        if not_vehicle and not claimable:
            return _not_vehicle_reply(payload)

    return f"*Claim {claim_id}*\nStatus: {status}"


def required_docs():
    configured = os.getenv(
        "REQUIRED_DOCS", "rc,driving_licence,claim_form"
    )
    return [
        value.strip().lower()
        for value in configured.split(",")
        if value.strip().lower() in DOCUMENT_LABELS
    ]


def strict_docs_enabled():
    return os.getenv("STRICT_DOCS", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def classify_session_intake(session):
    """Decide whether to ask for documents before analysis.

    Uses rules.establish_documents, the same function the pipeline uses, so
    the checklist, the Documents card, the undetermined list and the decision
    reason all describe one result. session["docs"] is a list of page
    documents ({"name", "paths"}). A requirement that only an unread document
    could satisfy is not asked for again: the customer did send something,
    and the claim records it as not established.
    """
    classifications = []
    vehicle_count = 0
    for path in list(session.get("photos") or [])[:MAX_IMAGES_PER_CLAIM]:
        result = classify_image(path)
        entry = {
            "path": path,
            "filename": os.path.basename(str(path)),
            **result,
            "confidence": reported_confidence(result),
        }
        classifications.append(entry)
        if route_classification(entry) == "vehicle_photo":
            vehicle_count += 1

    documents = [
        dict(document, doc_type=None)
        for document in session.get("docs") or []
        if isinstance(document, dict)
    ]
    extraction = extract_fields(documents) if documents else {
        "status": "not_attempted", "fields": [], "failed": [],
    }
    check = establish_documents(
        classifications, extraction, [document["name"] for document in documents],
        required_docs(),
    )
    missing = [] if check["unverified"] else check["missing"]
    result = {
        "classifications": classifications,
        "documents_received": check["received"],
        "vehicle_count": vehicle_count,
        "missing": missing,
        "unread_documents": check["unread_documents"],
        "document_check": check,
    }
    session["intake"] = result
    session["missing_docs"] = missing
    return result


def checklist_reply(intake):
    vehicle_count = intake.get("vehicle_count", 0)
    if not vehicle_count:
        return "\n".join([
            "*Vehicle photo needed*",
            "",
            "I received your documents, but no vehicle photo was found.",
            "Send at least one clear photo of the damaged vehicle.",
            "SKIP is not available until a vehicle photo is provided.",
        ])

    lines = ["*Almost there — documents needed*", "", "Received:"]
    lines.append(f"✓ Damage photos ({vehicle_count})")
    received = intake.get("documents_received") or {}
    for doc_type in required_docs():
        if received.get(doc_type):
            lines.append(f"✓ {DOCUMENT_LABELS[doc_type]}")
    lines += ["", "Still needed:"]
    for doc_type in intake.get("missing") or []:
        lines.append(f"- {DOCUMENT_LABELS[doc_type]}")
    lines += ["", "Send them here."]
    if not strict_docs_enabled():
        lines[-1] += (
            " Or reply *SKIP* to submit now — your claim will go to an "
            "assessor, who will request them."
        )
    return "\n".join(lines)


async def handle_pending_choice(wa_id, text):
    """Handle a numeric/YES response, returning True when a choice was pending."""
    session = get_session(wa_id)
    pending = session.get("pending_choice")
    if not pending:
        return False

    vehicles = [
        vehicle for vehicle in (pending.get("vehicles") or [])
        if isinstance(vehicle, dict)
    ]
    answer = normalised_command(text)
    selected = None
    number = int(answer) if answer.isdigit() else None
    if number is not None and 1 <= number <= len(vehicles):
        selected = vehicles[number - 1]
    elif answer in ("yes", "y") and len(vehicles) == 1:
        selected = vehicles[0]

    if selected is not None:
        reply = _single_vehicle_reply(
            pending.get("claim_id", "unknown"),
            selected,
            excluded_count=len(selected.get("excluded_photos") or []),
            total_photos=len(selected.get("photos") or []),
            undetermined=pending.get("undetermined"),
            decision=pending.get("decision"),
            decision_reasons=pending.get("decision_reasons"),
        )
        # State is written before the await so a message arriving meanwhile
        # never sees, or is overwritten by, a stale copy.
        if session.get("photos") or session.get("docs") or session.get("stage") == "open_claim":
            # A guided claim stays open for documents after the choice.
            session.update(pending_choice=None, pending_prompt=None, updated=time.time())
        else:
            SESSIONS.pop(wa_id, None)
        await send_text(wa_id, reply)
        return True

    session["updated"] = time.time()
    prompt = session.get("pending_prompt")
    if not prompt:
        prompt = (
            _selection_reply(pending.get("claim_id", "unknown"), vehicles)
            if len(vehicles) > 1
            else (
                f"Reply *YES* to continue with the "
                f"{vehicles[0].get('label') if vehicles else 'vehicle'}, "
                "or send fresh photos."
            )
        )
    await send_text(wa_id, prompt)
    return True


def _materialise_media(wa_id, rows):
    """Write session media to temp files for the pipeline. Ids stay in the DB.

    Returns (photos, page_documents, created, documents, missing). photos is
    a list of (path, sha256, source_bytes); page_documents is one
    {"name", "paths"} per source PDF; documents lists one (filename, sha256,
    source_bytes) per source PDF, in arrival order.
    """
    from main import UPLOAD_DIR

    folder = os.path.join(UPLOAD_DIR, f"wa_{wa_id}")
    photos, created, documents = [], [], []
    pages_by_name = {}
    document_numbers = {}
    missing = 0
    for row in rows:
        loaded = db.media_get(row["media_id"])
        if not loaded:
            missing += 1
            print(f"[whatsapp] session media missing for {who(wa_id)}", flush=True)
            continue
        data, mime = loaded
        if row["kind"] == "document_page":
            sha = row["sha256"]
            if sha not in document_numbers:
                document_numbers[sha] = len(document_numbers) + 1
                documents.append((
                    f"document{document_numbers[sha]}.pdf", sha, row.get("source_bytes"),
                ))
            name = f"document{document_numbers[sha]}-page{int(row['page'])}.jpg"
        else:
            extension = _MIME_EXTENSIONS.get(str(mime or "").lower(), ".jpg")
            name = f"photo{len(photos) + 1}-{str(row['media_id'])[:12]}{extension}"
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(data)
        if row["kind"] == "document_page":
            pages_by_name.setdefault(f"document{document_numbers[row['sha256']]}.pdf", []).append(path)
        else:
            photos.append((path, row["sha256"], row.get("source_bytes")))
        created.append(path)
    page_documents = [
        {"name": filename, "paths": pages_by_name.get(filename, [])}
        for filename, _, _ in documents
    ]
    return photos, page_documents, created, documents, missing


def _release_materialised(paths):
    if not paths:
        return
    from main import _release_temp_uploads

    _release_temp_uploads(paths)


def _missing_media_text(count):
    if count == 1:
        return "1 of your files could not be loaded and was left out. Please send it again."
    return (
        f"{count} of your files could not be loaded and were left out. "
        "Please send them again."
    )


def _register_files(claim_id, documents, photos):
    from db import save_file

    for filename, sha, size in documents:
        save_file(claim_id, filename, "document", None, sha, size)
    for path, sha, size in photos:
        save_file(claim_id, os.path.basename(path), "photo", path, sha, size)


async def process_session(wa_id, skip_missing=False):
    if wa_id in ANALYSING_IDS:
        await send_text(wa_id, ANALYSIS_BUSY_REPLY)
        return
    # Resolve expiry before taking the lock so a restarted process does not
    # keep an analysing flag whose lock died with the previous process.
    session = get_session(wa_id)
    ANALYSING_IDS.add(wa_id)
    keep_session = False
    temp_paths = []
    consumed = None
    claim_id = None
    try:
        rows = db.list_session_media(wa_id)
        # Snapshot: exactly these rows belong to this claim, and the number is
        # busy from here on. Anything that arrives while we classify or analyse
        # has a higher id and starts the next claim.
        session.update(
            analysing=True,
            analysis_cutoff=max((row["id"] for row in rows), default=0),
            updated=time.time(),
        )
        photos, page_documents, temp_paths, documents, missing_media = await asyncio.to_thread(
            _materialise_media, wa_id, rows
        )
        photo_paths = [path for path, _, _ in photos]
        view = {"photos": photo_paths, "docs": page_documents}
        intake = await asyncio.to_thread(classify_session_intake, view)
        missing = list(intake.get("missing") or [])
        state = {"intake": intake, "missing_docs": missing}
        notices = [_missing_media_text(missing_media)] if missing_media else []
        # Reload: a session copy held across an await is never written back.
        session = get_session(wa_id)
        if not intake.get("vehicle_count") or (
            missing and (not skip_missing or strict_docs_enabled())
        ):
            session.update(
                **state, awaiting_docs=True, analysing=False, analysis_cutoff=None,
                updated=time.time(),
            )
            keep_session = True
            for notice in notices:
                await send_text(wa_id, notice)
            await send_text(wa_id, checklist_reply(intake))
            return

        # Missing documents are decided by the pipeline (rules.establish_documents)
        # from what it established, on every path, so SKIP adds no reason here.
        consumed = [row["id"] for row in rows]
        session.update(**state, awaiting_docs=False, updated=time.time())
        for notice in notices:
            await send_text(wa_id, notice)
        await send_text(wa_id, ANALYSING_REPLY)

        claim_id = "CLM-" + uuid.uuid4().hex[:8].upper()

        # Registered here so the duplicate gate can write phash back against these rows,
        # exactly as the /api/analyze upload path does.
        await asyncio.to_thread(_register_files, claim_id, documents, photos)

        from main import analyse_claim  # deferred: main imports this module at startup

        # The guided flow confirmed a registry record before collecting.
        guided = get_session(wa_id).get("stage") == "collect"
        record = get_session(wa_id).get("registry") if guided else None
        forced = []
        if record and not registry.mobile_matches(record, wa_id):
            forced.append("WhatsApp number is not the registered mobile for this vehicle")
        payload = await analyse_claim(
            claim_id,
            [],
            photo_paths,
            source="whatsapp",
            from_number=wa_id,
            page_documents=page_documents,
            registry_record=record,
            forced_review_reasons=forced,
        )
        reply = format_reply(payload)
        vehicles = [
            vehicle for vehicle in (payload.get("vehicles") or [])
            if isinstance(vehicle, dict) and vehicle.get("status") == "claimable"
        ]
        has_duplicate_group = any(
            isinstance(vehicle, dict) and vehicle.get("status") == "all_duplicate"
            for vehicle in (payload.get("vehicles") or [])
        )
        needs_choice = (
            payload.get("status") == "analysed"
            # The registered vehicle was matched in the photos: no question.
            and payload.get("selected_vehicle_group") is None
            and (
                payload.get("claimable_count", len(vehicles)) > 1
                or (len(vehicles) == 1 and has_duplicate_group)
            )
        )
        await asyncio.to_thread(db.delete_session_media, wa_id, consumed)
        consumed = None
        # Media identical to this claim's, sent during the analysis, is now
        # the next claim (the duplicate gate will judge it then).
        restore_deferred_media(wa_id)
        live = get_session(wa_id)
        if guided:
            # The claim stays OPEN after its result: documents sent next in
            # this session are added to it (CLM-72F34AC2 started a new case).
            choice = {
                "claim_id": payload.get("claim_id", claim_id),
                "vehicles": vehicles,
                "undetermined": payload.get("undetermined") or [],
                "decision": payload.get("decision"),
                "decision_reasons": payload.get("decision_reasons") or [],
            } if needs_choice else None
            live.update(
                analysing=False, analysis_cutoff=None, stage="open_claim",
                claim_id=payload.get("claim_id", claim_id), step=None,
                pending_choice=choice, pending_prompt=reply if needs_choice else None,
                updated=time.time(),
            )
            keep_session = True
        elif live["photos"] or live["docs"]:
            # Media that arrived during the analysis is the next claim.
            reset_session(wa_id)
            keep_session = True
        elif needs_choice:
            live.update(
                analysing=False,
                analysis_cutoff=None,
                pending_choice={
                    "claim_id": payload.get("claim_id", claim_id),
                    "vehicles": vehicles,
                    "undetermined": payload.get("undetermined") or [],
                    "decision": payload.get("decision"),
                    "decision_reasons": payload.get("decision_reasons") or [],
                },
                pending_prompt=reply,
                updated=time.time(),
            )
            keep_session = True
        await send_text(wa_id, reply)
    except Exception:
        print("[whatsapp] process_session failed:", flush=True)
        traceback.print_exc()
        try:
            saved = bool(claim_id) and await asyncio.to_thread(db.get_claim, claim_id) is not None
        except Exception as error:
            # The database cannot say (a PoolTimeout or an outage): keep the
            # media and ask for DONE, never a raw error.
            print(f"[whatsapp] could not check the claim after a failure: "
                  f"{type(error).__name__}", flush=True)
            saved, consumed = False, None
        if consumed is not None and claim_id and not saved:
            # Nothing was saved: keep the media so DONE retries it, and drop the
            # half-written file rows so the retry is not matched as a duplicate.
            try:
                await asyncio.to_thread(db.delete_files_for_claim, claim_id)
            except Exception as error:
                print(f"[whatsapp] could not drop half-written file rows: "
                      f"{type(error).__name__}", flush=True)
            consumed = None
        if saved:
            # The claim exists; a retry would only meet the duplicate gate.
            await send_text(
                wa_id,
                f"Your claim {claim_id} was saved, but sending the result failed. "
                "An assessor will review it; you do not need to resend anything.",
            )
        else:
            await send_text(
                wa_id, "Something went wrong processing your claim. Reply DONE to try again."
            )
    finally:
        _release_materialised(temp_paths)
        try:
            if consumed:
                db.delete_session_media(wa_id, consumed)
            # Failure paths: kept rows absorb identical held media; the rest returns.
            restore_deferred_media(wa_id)
            if not keep_session:
                if db.list_session_media(wa_id):
                    reset_session(wa_id)
                else:
                    SESSIONS.pop(wa_id, None)
        except Exception as error:
            # The reply has been sent; a cleanup failure must not send another.
            print(f"[whatsapp] session cleanup failed: {type(error).__name__}", flush=True)
        DEFERRED_MEDIA.pop(wa_id, None)
        BUSY_NOTICE_SENT.discard(wa_id)
        ANALYSING_IDS.discard(wa_id)


# --- Guided conversation -----------------------------------------------------
#
# stage (in wa_sessions state):
#   None / "menu"        START: choose 1 (file a claim) or 2 (track a claim)
#   "ask_plate"           waiting for the registration number
#   "confirm_vehicle"     registry record shown, waiting for YES / NO
#   "collect"             steps 1-4: photos, RC, licence, claim form
#   "open_claim"          result sent; documents now attach to claim_id
#   "track"               waiting for a tracking ID or ALL
#   "confirm_attach"      a document arrived for a claim in more_info status
# MENU, END and HELP work at any point.

WELCOME = "*Welcome to InsureIT*\nReply *1* to file a new claim\nReply *2* to track a claim"
HELP_TEXT = "\n".join([
    "*Commands*",
    "*MENU* — start again",
    "*END* — close this session",
    "*HELP* — this list",
    "*DONE* — finish sending damage photos",
    "*SKIP* — skip steps 2 to 4 (RC, licence, claim form)",
])
PLATE_PROMPT = "Send your vehicle registration number, for example *MH12AB1234*."
TRACK_PROMPT = ("Send your tracking ID (e.g. CLM-72F34AC2), or reply *ALL* to see "
                "claims on this number.")
CONFIRM_PROMPT = "Reply *YES* to continue or *NO* to enter a different number."
TIMEOUT_TEXT = "Your session timed out after 30 minutes of inactivity."
STEP_SLOTS = {1: "photo", 2: "rc", 3: "driving_licence", 4: "claim_form"}
SLOT_STEPS = {slot: step for step, slot in STEP_SLOTS.items()}
SLOT_LABELS = {
    "photo": "damage photo", "rc": "registration certificate",
    "driving_licence": "driving licence", "claim_form": "claim form",
    "policy": "policy document",
}
STEP_PROMPTS = {
    1: ("*Step 1 of 4 — damage photos*\nSend photos of the damage from a few angles. "
        "Reply *DONE* when you have sent them all."),
    2: "*Step 2 of 4 — registration certificate*\nSend a photo of the RC, or reply *SKIP*.",
    3: "*Step 3 of 4 — driving licence*\nSend a photo of the driver's licence, or reply *SKIP*.",
    4: "*Step 4 of 4 — claim form*\nSend the claim form as a photo or PDF, or reply *SKIP*.",
}
COMMANDS = {
    "menu": {"menu", "main menu", "start", "restart", "home"},
    "end": {"end", "exit", "quit", "close", "bye"},
    "help": {"help", "?", "commands"},
    "yes": {"yes", "y", "yeah", "yep", "ok", "okay", "haan", "ha", "confirm"},
    "no": {"no", "n", "nope", "nahi"},
    "done": {"done", "finished", "finish", "next", "complete", "ho gaya", "hogaya", "हो गया"},
    "skip": {"skip"},
    "1": {"1", "one", "new claim", "file a claim", "file claim", "claim"},
    "2": {"2", "two", "track", "track a claim", "track claim", "status"},
    "all": {"all"},
}
EVENT_LABELS = {
    "submitted": "Claim submitted", "assessor_assigned": "Assessor assigned",
    "survey_scheduled": "Survey scheduled", "documents_added": "Documents added",
    "more_info_requested": "More information requested", "approved": "Approved",
    "rejected": "Not approved", "assessment_updated": "Assessment updated",
}


def command_of(text):
    """The command a reply means, for "MENU", "menu.", "*1*", "Yes!"; else None."""
    value = normalised_command(str(text or "").replace("*", ""))
    for command, words in COMMANDS.items():
        if value in words:
            return command
    return None


def set_conversation(wa_id, **fields):
    session = get_session(wa_id)
    session.update(updated=time.time(), **fields)
    return session


def clear_conversation(wa_id):
    """END or MENU: the session's state and its pending media go."""
    rows = db.list_session_media(wa_id)
    if rows:
        db.delete_session_media(wa_id, [row["id"] for row in rows])
    db.delete_session(str(wa_id))
    DEFERRED_MEDIA.pop(wa_id, None)


def slot_counts(wa_id):
    """Pending items per slot. Photos sent before a claim was started count
    as damage photos; a PDF counts once, however many pages."""
    session = get_session(wa_id)
    counts = {slot: 0 for slot in SLOT_LABELS}
    seen = set()
    for row in _pending_rows(session, db.list_session_media(wa_id)):
        slot = row.get("slot") or ("photo" if row["kind"] == "photo" else "claim_form")
        key = (slot, row["sha256"])
        if key in seen:
            continue
        seen.add(key)
        counts[slot] = counts.get(slot, 0) + 1
    return counts


def next_step(session, counts, after):
    skipped = set(session.get("skipped") or [])
    for step in range(after + 1, 5):
        slot = STEP_SLOTS[step]
        if counts.get(slot) or slot in skipped:
            continue
        return step
    return None


def classify_slot(data, is_pdf, step):
    """Which slot an arriving image (or a PDF's first page) belongs in."""
    import tempfile

    handle, path = tempfile.mkstemp(suffix=".jpg")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        result = classify_image(path) or {}
    except Exception as error:
        print(f"[whatsapp] slot classification failed: {type(error).__name__}", flush=True)
        result = {}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    route = route_classification({
        "doc_type": result.get("doc_type"), "confidence": reported_confidence(result),
    })
    if route in SLOT_LABELS and route != "photo":
        return route
    if route == "vehicle_photo" and not is_pdf:
        return "photo"
    # Not clearly a vehicle and not a known document: the step being filled.
    if step and step > 1:
        return STEP_SLOTS[step]
    return "claim_form" if is_pdf else "photo"


async def show_welcome(wa_id, prefix=None):
    set_conversation(wa_id, stage="menu")
    await send_text(wa_id, f"{prefix}\n\n{WELCOME}" if prefix else WELCOME)


async def begin_claim(wa_id):
    set_conversation(wa_id, stage="ask_plate", step=None, skipped=[], plate=None,
                     registry=None, claim_id=None)
    await send_text(wa_id, PLATE_PROMPT)


def vehicle_summary(plate, record, wa_id):
    vehicle = registry.vehicle_label(record)
    body = f" ({record['body_type']})" if record.get("body_type") else ""
    mobile = (
        "✓ This WhatsApp number matches the registered mobile"
        if registry.mobile_matches(record, wa_id) else
        "⚠ This WhatsApp number is not the registered mobile. You can continue; "
        "your assessor will verify ownership."
    )
    return "\n".join([
        f"*Vehicle found: {plate}*",
        f"Vehicle: {vehicle}{body}",
        f"Registered owner: {registry.mask_name(record.get('owner_name'))}",
        f"Insurer: {record.get('insurer')}",
        f"Policy: {registry.mask_policy(record.get('policy_number'))}, valid until "
        f"{registry.display_date(record.get('policy_end'))}",
        mobile,
        "",
        CONFIRM_PROMPT,
    ])


async def on_plate(wa_id, text):
    plate = registry.normalise_plate(text)
    if not registry.valid_plate(plate):
        await send_text(wa_id, "That is not a valid registration number. " + PLATE_PROMPT)
        return
    record = registry.lookup(plate)
    if not record:
        await send_text(wa_id, f"We could not find *{plate}* in the registry. "
                               "Check the number and send it again.")
        return
    state = registry.policy_state(record, registry.today())
    insurer = record.get("insurer") or "your insurer"
    end = registry.display_date(record.get("policy_end"))
    ending = None
    if state == "expired":
        ending = (f"The policy on *{plate}* with {insurer} expired on {end}, so a claim "
                  f"cannot be filed against it. Please renew it with {insurer}.")
    elif state == "third_party_only":
        ending = (f"*{plate}* has a third-party-only policy with {insurer}. It does not cover "
                  "damage to your own vehicle, so an own-damage claim cannot be filed.")
    elif state == "not_started":
        ending = (f"The policy on *{plate}* with {insurer} starts on "
                  f"{registry.display_date(record.get('policy_start'))}; it does not cover "
                  "earlier damage.")
    elif state != "active":
        ending = f"We could not confirm the policy dates for *{plate}*. Please contact {insurer}."
    if ending:
        clear_conversation(wa_id)
        await send_text(wa_id, f"{ending}\n\nReply *MENU* to start again.")
        return
    set_conversation(wa_id, stage="confirm_vehicle", plate=plate, registry=record)
    await send_text(wa_id, vehicle_summary(plate, record, wa_id))


async def start_collecting(wa_id):
    set_conversation(wa_id, stage="collect", step=1, skipped=[])
    held = slot_counts(wa_id).get("photo", 0)
    if held:
        await send_text(wa_id, STEP_PROMPTS[1] + f"\nYou have already sent {held} photo"
                        f"{'' if held == 1 else 's'}; send more or reply *DONE*.")
        return
    await send_text(wa_id, STEP_PROMPTS[1])


async def advance(wa_id, step, start_analysis, prefix=None):
    """Prompt the next step, or analyse when every step is filled or skipped."""
    if step is None:
        if prefix:
            await send_text(wa_id, prefix)
        await start_analysis(wa_id, skip_missing=True)
        return
    set_conversation(wa_id, step=step)
    await send_text(wa_id, f"{prefix}\n\n{STEP_PROMPTS[step]}" if prefix else STEP_PROMPTS[step])


async def on_collect_text(wa_id, command, start_analysis):
    session = get_session(wa_id)
    step = session.get("step") or 1
    counts = slot_counts(wa_id)
    if step == 1:
        if command == "skip":
            await send_text(wa_id, "Damage photos cannot be skipped. Send at least one photo "
                                   "of the damage, then reply *DONE*.")
            return
        if command != "done":
            await send_text(wa_id, STEP_PROMPTS[1])
            return
        if not counts.get("photo"):
            await send_text(wa_id, "Send at least one photo of the damage first, then reply *DONE*.")
            return
        await advance(wa_id, next_step(session, counts, 1), start_analysis)
        return
    slot = STEP_SLOTS[step]
    if command == "skip":
        if strict_docs_enabled():
            await send_text(wa_id, f"The {SLOT_LABELS[slot]} is required for this claim. "
                                   "Please send it.")
            return
        skipped = list(session.get("skipped") or []) + [slot]
        session = set_conversation(wa_id, skipped=skipped)
        await advance(wa_id, next_step(session, counts, step), start_analysis)
        return
    if command == "done" and counts.get(slot):
        await advance(wa_id, next_step(session, counts, step), start_analysis)
        return
    await send_text(wa_id, STEP_PROMPTS[step])


async def guided_media_received(wa_id, slot, start_analysis):
    """Say which slot an item filled, and move on when its step is done."""
    if is_analysing(wa_id):
        return
    session = get_session(wa_id)
    step = session.get("step") or 1
    counts = slot_counts(wa_id)
    if slot == "photo":
        count = counts.get("photo", 0)
        if step == 1:
            await send_text(wa_id, f"Photo received ({count} total). Reply DONE when you are done.")
        else:
            await send_text(wa_id, f"That looks like a damage photo — added to step 1 "
                                   f"({count} photo{'' if count == 1 else 's'}). Still on step "
                                   f"{step}: send the {SLOT_LABELS[STEP_SLOTS[step]]} or reply *SKIP*.")
        return
    label = SLOT_LABELS.get(slot, "document")
    target = SLOT_STEPS.get(slot)
    if target is None:
        await send_text(wa_id, f"{label.capitalize()} received; it will be added to the claim.")
        return
    if target == step:
        received = f"{label[:1].upper() + label[1:]} received (step {step} of 4)."
        await advance(wa_id, next_step(session, counts, step), start_analysis, prefix=received)
        return
    text = f"That looks like your {label} — added to step {target}."
    if step == 1:
        text += " Send more damage photos, or reply *DONE*."
    else:
        text += f" Still on step {step}: send the {SLOT_LABELS[STEP_SLOTS[step]]} or reply *SKIP*."
    await send_text(wa_id, text)


def _ist(value):
    from datetime import datetime, timedelta, timezone

    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b, %H:%M IST")


def status_label(response):
    final = (response.get("final_decision") or {}).get("value")
    if final == "approved":
        return "Approved"
    if final == "rejected":
        return "Not approved"
    if final == "more_info":
        return "More information needed"
    if response.get("status") == "error":
        return "With your assessor (automated assessment unavailable)"
    return {
        "auto_assess": "Assessed — approval expected",
        "needs_review": "Under review",
        "reject": "Rejected",
    }.get(response.get("decision"), "Received")


def tracking_text(response):
    record = response.get("registry") or {}
    plate = response.get("plate") or record.get("plate")
    vehicle = registry.vehicle_label(record) if record else response.get("vehicle_description")
    events = response.get("events") or []
    lines = [f"*{response['claim_id']}*"]
    if vehicle or plate:
        lines.append(" · ".join(str(part) for part in (vehicle, plate) if part))
    lines.append(f"Status: {status_label(response)}")
    assessor = response.get("assessor") or {}
    if assessor.get("name"):
        phone = f", {assessor['phone']}" if assessor.get("phone") else ""
        lines.append(f"Assessor: {assessor['name']}{phone}")
    if events:
        last = events[-1]
        lines.append(f"Last update: {EVENT_LABELS.get(last['event'], last['event'])} · "
                     f"{_ist(last['created_at'])}")
    if response.get("next_step"):
        lines.append(f"Next: {response['next_step']}")
    return "\n".join(lines)


def own_claim(wa_id, claim_id):
    """The claim's console response, only when it belongs to this number."""
    from main import build_claim_response

    payload = db.get_claim(claim_id)
    if not payload or str(payload.get("from_number") or "") != str(wa_id):
        return None
    return build_claim_response(payload)


async def on_track(wa_id, text, command):
    if command == "all":
        claims = db.claims_for_number(wa_id)[:5]
        if not claims:
            await send_text(wa_id, "There are no claims for this WhatsApp number yet. "
                                   "Reply *1* to file one, or *MENU*.")
            return
        lines = ["*Claims on this number*"]
        for summary in claims:
            response = await asyncio.to_thread(own_claim, wa_id, summary["claim_id"])
            if response:
                plate = response.get("plate") or ""
                lines.append(f"• *{response['claim_id']}* {plate} — {status_label(response)}")
        lines.append("\nSend a tracking ID for details.")
        await send_text(wa_id, "\n".join(lines))
        return
    tracking = re.sub(r"[\s*]", "", str(text or "")).upper()
    if re.fullmatch(r"[0-9A-F]{8}", tracking):
        tracking = "CLM-" + tracking
    response = await asyncio.to_thread(own_claim, wa_id, tracking) if tracking else None
    if not response:
        await send_text(wa_id, f"We could not find *{tracking or text}* for this number. "
                               "Check the tracking ID, or reply *ALL*.")
        return
    await send_text(wa_id, tracking_text(response))


async def end_session(wa_id):
    session = get_session(wa_id)
    claim_id = session.get("claim_id")
    clear_conversation(wa_id)
    if claim_id:
        await send_text(wa_id, f"Session closed. Your tracking ID is *{claim_id}* — send *2* "
                               "any time to check it. We will message you here when the "
                               "decision is made.")
    else:
        await send_text(wa_id, "Session closed. Send any message to start again.")


def more_info_claim(wa_id):
    """The newest claim from this number waiting for more information, or None."""
    for summary in db.claims_for_number(wa_id):
        rows = [row for row in db.audit_log_for_claim(summary["claim_id"])
                if row.get("action") == "set_decision"]
        if rows:
            after = rows[-1].get("after") or rows[-1].get("after_json") or {}
            if isinstance(after, str):
                try:
                    after = json.loads(after)
                except ValueError:
                    after = {}
            if after.get("value") == "more_info":
                return summary["claim_id"]
    return None


async def handle_conversation_text(wa_id, text, start_analysis, attach):
    """Every text in the guided flow. start_analysis is process_session;
    attach(wa_id, claim_id) adds pending documents to a claim."""
    command = command_of(text)
    session = get_session(wa_id)
    stage = session.get("stage")
    if command == "menu":
        clear_conversation(wa_id)
        await show_welcome(wa_id)
        return
    if command == "end":
        await end_session(wa_id)
        return
    if command == "help":
        await send_text(wa_id, HELP_TEXT)
        return
    if stage in (None, "menu"):
        if command == "1":
            await begin_claim(wa_id)
        elif command == "2":
            set_conversation(wa_id, stage="track")
            await send_text(wa_id, TRACK_PROMPT)
        else:
            await show_welcome(wa_id)
        return
    if stage == "ask_plate":
        await on_plate(wa_id, text)
        return
    if stage == "confirm_vehicle":
        if command == "yes":
            await start_collecting(wa_id)
        elif command == "no":
            set_conversation(wa_id, stage="ask_plate", plate=None, registry=None)
            await send_text(wa_id, PLATE_PROMPT)
        else:
            await send_text(wa_id, CONFIRM_PROMPT)
        return
    if stage == "collect":
        await on_collect_text(wa_id, command, start_analysis)
        return
    if stage == "track":
        await on_track(wa_id, text, command)
        return
    if stage == "confirm_attach":
        claim_id = session.get("attach_claim_id")
        if command == "yes" and claim_id:
            set_conversation(wa_id, stage="open_claim", claim_id=claim_id)
            await attach(wa_id, claim_id)
        elif command == "no":
            clear_conversation(wa_id)
            await show_welcome(wa_id, "Not added.")
        else:
            await send_text(wa_id, f"Add this to *{claim_id}*? Reply *YES* or *NO*.")
        return
    if stage == "open_claim":
        claim_id = session.get("claim_id")
        await send_text(wa_id, f"Your claim *{claim_id}* is open. {OPEN_CLAIM_LINE}")
        return
    await show_welcome(wa_id)


DECISION_WORDS = {
    "auto_assess": "assessed, approval expected", "needs_review": "under review",
    "reject": "rejected",
}


def attach_reply(claim_id, before, after, labels):
    """What adding documents changed, for the customer."""
    check = after.get("document_check") or {}
    received = check.get("received") or {}
    required = after.get("required_documents") or []
    marks = " · ".join(
        f"{'✓' if received.get(doc_type) else '✗'} {DOCUMENT_LABELS.get(doc_type, doc_type)}"
        for doc_type in required
    )
    lines = [f"Added your {', '.join(labels) or 'document'} to *{claim_id}*."]
    if marks:
        lines.append(f"Documents: {marks}")
    missing = check.get("missing") or []
    if missing:
        lines.append("Still needed: " + ", ".join(
            DOCUMENT_LABELS.get(doc_type, doc_type).lower() for doc_type in missing) + ".")
    old_high = {flag.get("message") for flag in before.get("flags") or []
                if flag.get("severity") == "HIGH"}
    new_high = [flag.get("message") for flag in after.get("flags") or []
                if flag.get("severity") == "HIGH" and flag.get("message") not in old_high]
    if new_high:
        lines.append(f"New finding for your assessor: {new_high[0]}")
    word = DECISION_WORDS.get(after.get("decision"), after.get("decision"))
    changed = before.get("decision") != after.get("decision")
    lines.append(f"Status: {word}" + ("" if changed else " (unchanged)"))
    if after.get("next_step"):
        lines.append(f"Next: {after['next_step']}")
    lines += ["", "Send more documents here, reply *END* to close this session or *MENU* for more."]
    return "\n".join(lines)
