"""Seed WhatsApp sessions the way intake stores them: state in wa_sessions,
media as stored blobs plus append-only wa_session_media rows."""
import hashlib
import io
import time

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


def read_all(paths):
    out = []
    for path in paths:
        with open(path, "rb") as handle:
            out.append(handle.read())
    return out
