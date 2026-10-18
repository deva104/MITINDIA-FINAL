import json
import os
import sqlite3
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


def cache_get(content_hash):
    conn = get_conn()
    try:
        _ensure_vision_cache(conn)
        row = conn.execute(
            """
            SELECT response_json, provider, model
            FROM vision_cache
            WHERE content_hash = ?
            """,
            (content_hash,),
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
                "flags": payload.get("flags", []),
                "estimate": payload.get("estimate", {}),
            }
        )
    return out
