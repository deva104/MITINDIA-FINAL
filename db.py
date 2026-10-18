import json
import os
import sqlite3
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "claimpilot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS claims (
            claim_id     TEXT PRIMARY KEY,
            created_at   TEXT,
            payload_json TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT,
            filename TEXT,
            kind     TEXT,
            path     TEXT,
            phash    TEXT
        );
        CREATE TABLE IF NOT EXISTS vision_cache (
            content_hash  TEXT PRIMARY KEY,
            label         TEXT,
            response_json TEXT,
            provider      TEXT,
            model         TEXT,
            created_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id    TEXT,
            actor       TEXT,
            action      TEXT,
            target      TEXT,
            before_json TEXT,
            after_json  TEXT,
            reason      TEXT NOT NULL,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id   TEXT PRIMARY KEY,
            processed_at REAL
        );
        """
    )
    conn.commit()
    conn.close()


def save_claim(claim_id, payload_dict):
    created_at = payload_dict.get("created_at") or datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO claims (claim_id, created_at, payload_json) VALUES (?, ?, ?)",
        (claim_id, created_at, json.dumps(payload_dict, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def save_file(claim_id, filename, kind, path):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO files (claim_id, filename, kind, path, phash) VALUES (?, ?, ?, ?, NULL)",
        (claim_id, filename, kind, path),
    )
    conn.commit()
    file_id = cur.lastrowid
    conn.close()
    return file_id


def get_claim(claim_id):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT payload_json FROM claims WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])
    finally:
        conn.close()


def _ensure_audit_log(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id    TEXT,
            actor       TEXT,
            action      TEXT,
            target      TEXT,
            before_json TEXT,
            after_json  TEXT,
            reason      TEXT NOT NULL,
            created_at  TEXT
        )
        """
    )


def audit_log_add(
    claim_id, actor, action, target, before, after, reason, created_at=None
):
    conn = get_conn()
    try:
        _ensure_audit_log(conn)
        timestamp = created_at or datetime.now().isoformat()
        cursor = conn.execute(
            """
            INSERT INTO audit_log (
                claim_id, actor, action, target, before_json,
                after_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                actor,
                action,
                target,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                reason,
                timestamp,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def audit_log_for_claim(claim_id):
    conn = get_conn()
    try:
        _ensure_audit_log(conn)
        rows = conn.execute(
            """
            SELECT id, claim_id, actor, action, target, before_json,
                   after_json, reason, created_at
            FROM audit_log
            WHERE claim_id = ?
            ORDER BY id ASC
            """,
            (claim_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _ensure_vision_cache(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vision_cache (
            content_hash  TEXT PRIMARY KEY,
            label         TEXT,
            response_json TEXT,
            provider      TEXT,
            model         TEXT,
            created_at    TEXT
        )
        """
    )


def cache_get(content_hash, label=None):
    conn = get_conn()
    try:
        _ensure_vision_cache(conn)
        if label is None:
            row = conn.execute(
                """
                SELECT response_json, provider, model, label
                FROM vision_cache
                WHERE content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT response_json, provider, model, label
                FROM vision_cache
                WHERE content_hash = ? AND label = ?
                """,
                (content_hash, label),
            ).fetchone()
        if not row:
            return None
        try:
            response = json.loads(row["response_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(response, dict):
            return None
        return {
            "response": response,
            "provider": row["provider"],
            "model": row["model"],
            "label": row["label"],
        }
    finally:
        conn.close()


def cache_put(content_hash, label, response, provider, model):
    conn = get_conn()
    try:
        _ensure_vision_cache(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO vision_cache (
                content_hash, label, response_json, provider, model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_hash,
                label,
                json.dumps(response, ensure_ascii=False),
                provider,
                model,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


PROCESSED_MESSAGE_TTL_SECONDS = 48 * 60 * 60


def _ensure_processed_messages(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id   TEXT PRIMARY KEY,
            processed_at REAL
        )
        """
    )


def remember_processed_message(message_id, now=None):
    """Return True the first time a message id is stored within 48 hours."""
    if not message_id:
        return False
    received_at = time.time() if now is None else float(now)
    cutoff = received_at - PROCESSED_MESSAGE_TTL_SECONDS
    conn = get_conn()
    try:
        _ensure_processed_messages(conn)
        conn.execute(
            "DELETE FROM processed_messages WHERE processed_at < ?",
            (cutoff,),
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO processed_messages (message_id, processed_at)
            VALUES (?, ?)
            """,
            (str(message_id), received_at),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def list_claims():
    conn = get_conn()
    rows = conn.execute(
        "SELECT claim_id, created_at, payload_json FROM claims ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            payload = {}
        out.append(
            {
                "claim_id": r["claim_id"],
                "created_at": r["created_at"],
                "status": payload.get("status"),
                "stopped_at": payload.get("stopped_at"),
                "decision": payload.get("decision"),
                "decision_reasons": payload.get("decision_reasons", []),
                "confidence": payload.get("confidence"),
                "source": payload.get("source"),
                "from_number": payload.get("from_number"),
                "claimable_count": payload.get("claimable_count"),
                "flags": payload.get("flags", []),
                "estimate": payload.get("estimate", {}),
            }
        )
    return out
