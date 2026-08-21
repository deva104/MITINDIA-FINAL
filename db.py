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
