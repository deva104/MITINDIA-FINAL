"""Seed WhatsApp sessions the way intake stores them: state in wa_sessions,
media as stored blobs plus append-only wa_session_media rows."""
import hashlib
import io
import os
import time
from unittest.mock import patch

from PIL import Image

import db
import whatsapp


def jpeg(color=(20, 40, 60), size=(24, 16)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _items(value, offset):
    if isinstance(value, int):
        return [jpeg(color=((offset + index * 37) % 256, (index * 71) % 256, 90))
                for index in range(value)]
    return list(value or [])


def seed_session(wa_id, photos=0, documents=0, **state):
    """Store state and media for wa_id. photos/documents are a count or a list
    of JPEG bytes (a document is one rendered page). Media rows take the state's
    updated time, so a stale session is stale everywhere. Returns the bytes."""
    whatsapp.SESSIONS[wa_id] = state
    received_at = state.get("updated") or time.time()
    photo_bytes = _items(photos, 11)
    page_bytes = _items(documents, 140)
    for data in photo_bytes:
        media_id = db.media_put(data, "image/jpeg", "photo", None)
        db.add_session_media(
            wa_id, media_id, "photo", hashlib.sha256(data).hexdigest(), 0,
            len(data), received_at,
        )
    for data in page_bytes:
        media_id = db.media_put(data, "image/jpeg", "document_page", None)
        db.add_session_media(
            wa_id, media_id, "document_page", hashlib.sha256(data).hexdigest(), 1,
            len(data), received_at,
        )
    return photo_bytes, page_bytes


def extraction_ok(fields, documents=None, source_doc=None):
    """What gemini.extract_fields returns when every document was read."""
    fields = [dict(field, source_doc=source_doc) if source_doc else dict(field)
              for field in fields or []]
    names = [document.get("name") for document in documents or [] if isinstance(document, dict)]
    return {
        "status": "ok", "fields": fields, "documents": len(names) or 1,
        "read": names, "failed": [], "error": None,
    }


KEY_PREFIXES = ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")


def provider_keys(groq=1, gemini=1, openrouter=1, **extra):
    """Patch the environment so tests never see the developer's real keys:
    every real provider key is removed and `n` fake ones are set per provider."""
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(KEY_PREFIXES)}
    for prefix, count in zip(KEY_PREFIXES, (groq, gemini, openrouter)):
        for index in range(count):
            name = prefix if index == 0 else f"{prefix}_K{index}"
            env[name] = f"fake-{prefix.lower()}-{index}-000000000000"
    env.update(extra)
    return patch.dict(os.environ, env, clear=True)


def fresh_key_state():
    """Key cool-downs are process-wide; each test starts with none."""
    import vision

    return patch.dict(vision._KEY_STATE, {}, clear=True)


def read_all(paths):
    out = []
    for path in paths:
        with open(path, "rb") as handle:
            out.append(handle.read())
    return out
