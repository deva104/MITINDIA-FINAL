import hashlib
import json
import os
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "claimpilot.db")
MAX_MEDIA_BYTES = 2 * 1024 * 1024
PROCESSED_MESSAGE_TTL_SECONDS = 48 * 60 * 60

# Set for the duration of analyse_claim so stored images can name their claim
# without changing forensics function signatures.
current_claim_id = ContextVar("current_claim_id", default=None)

_POOL = None

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    claim_id     TEXT PRIMARY KEY,
    created_at   TEXT,
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id      TEXT,
    filename      TEXT,
    kind          TEXT,
    path          TEXT,
    phash         TEXT,
    source_sha256 TEXT,
    source_bytes  INTEGER
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
CREATE TABLE IF NOT EXISTS media (
    id         TEXT PRIMARY KEY,
    claim_id   TEXT,
    kind       TEXT,
    mime       TEXT,
    bytes      BLOB,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS wa_sessions (
    wa_id      TEXT PRIMARY KEY,
    state_json TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS wa_last_inbound (
    wa_id TEXT PRIMARY KEY,
    at    TEXT
);
CREATE TABLE IF NOT EXISTS wa_session_media (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id        TEXT NOT NULL,
    media_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    page         INTEGER NOT NULL DEFAULT 0,
    source_bytes INTEGER,
    received_at  REAL NOT NULL,
    UNIQUE (wa_id, sha256, page)
);
"""

POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS claims (
        claim_id     TEXT PRIMARY KEY,
        created_at   TEXT,
        payload_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        claim_id      TEXT,
        filename      TEXT,
        kind          TEXT,
        path          TEXT,
        phash         TEXT,
        source_sha256 TEXT,
        source_bytes  BIGINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vision_cache (
        content_hash  TEXT PRIMARY KEY,
        label         TEXT,
        response_json TEXT,
        provider      TEXT,
        model         TEXT,
        created_at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        claim_id    TEXT,
        actor       TEXT,
        action      TEXT,
        target      TEXT,
        before_json TEXT,
        after_json  TEXT,
        reason      TEXT NOT NULL,
        created_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_messages (
        message_id   TEXT PRIMARY KEY,
        processed_at DOUBLE PRECISION
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media (
        id         TEXT PRIMARY KEY,
        claim_id   TEXT,
        kind       TEXT,
        mime       TEXT,
        bytes      BYTEA,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wa_sessions (
        wa_id      TEXT PRIMARY KEY,
        state_json TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wa_last_inbound (
        wa_id TEXT PRIMARY KEY,
        at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wa_session_media (
        id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        wa_id        TEXT NOT NULL,
        media_id     TEXT NOT NULL,
        kind         TEXT NOT NULL,
        sha256       TEXT NOT NULL,
        page         INTEGER NOT NULL DEFAULT 0,
        source_bytes BIGINT,
        received_at  DOUBLE PRECISION NOT NULL,
        UNIQUE (wa_id, sha256, page)
    )
    """,
)

# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS does not
# add them to an existing table, so init_db adds any that are missing.
ADDED_COLUMNS = (
    ("files", "source_sha256", "TEXT"),
    ("files", "source_bytes", "BIGINT"),
)

TABLES = (
    "claims",
    "files",
    "vision_cache",
    "audit_log",
    "processed_messages",
    "media",
    "wa_sessions",
    "wa_last_inbound",
    "wa_session_media",
)


class Row(dict):
    """Dict row that also supports the positional access sqlite3.Row allows."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


class QueryResult:
    def __init__(self, rows, rowcount=0, lastrowid=None):
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class DatabaseConnection:
    def __init__(self, raw, postgres=False, pool=None):
        self._raw = raw
        self._postgres = postgres
        self._pool = pool
        self._closed = False
        self._statements = 0

    def _replace_dead_connection(self):
        try:
            self._pool.putconn(self._raw)  # the pool discards a broken connection
        except Exception:
            try:
                self._raw.close()
            except Exception:
                pass
        self._raw = self._pool.getconn()

    def execute(self, sql, params=None):
        values = tuple(params or ())
        if not self._postgres:
            return self._raw.execute(sql, values)
        statement = sql.replace("?", "%s")
        try:
            cursor = self._raw.execute(statement, values or None)
        except Exception as error:
            # Retry once, and only when the connection died before this
            # checkout ran anything: nothing can have been half-applied.
            if self._statements or not _is_connection_error(error):
                raise
            print(f"[db] connection dropped ({type(error).__name__}); retrying once", flush=True)
            self._replace_dead_connection()
            cursor = self._raw.execute(statement, values or None)
        self._statements += 1
        rows = []
        if cursor.description:
            columns = [column.name for column in cursor.description]
            rows = [Row(zip(columns, record)) for record in cursor.fetchall()]
        lastrowid = None
        if rows and sql.lstrip().upper().startswith("INSERT") and "id" in rows[0]:
            lastrowid = rows[0]["id"]
        return QueryResult(rows, rowcount=cursor.rowcount, lastrowid=lastrowid)

    def executescript(self, script):
        if self._postgres:
            raise RuntimeError("executescript is only used for SQLite schema setup")
        self._raw.executescript(script)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._postgres:
            try:
                self._raw.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(self._raw)
            except Exception:
                try:
                    self._raw.close()
                except Exception:
                    pass
            return
        self._raw.close()


def _select_backend():
    """Read DATABASE_URL once. Later environment changes must not switch backends."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return "postgres", url
    return "sqlite", ""


def _public_host(url):
    """Hostname only, with a Neon endpoint id shortened. Never user or password."""
    host = urlparse(url).hostname or ""
    labels = host.split(".")
    if labels and labels[0].startswith("ep-"):
        pooled = "-pooler" if labels[0].endswith("-pooler") else ""
        labels[0] = f"ep-...{pooled}"
        return ".".join(labels)
    return host


BACKEND, DB_URL = _select_backend()
if BACKEND == "postgres":
    print(f"[db] backend=postgres host={_public_host(DB_URL)}", flush=True)
else:
    print("[db] backend=sqlite", flush=True)


def database_backend():
    return BACKEND


def _pool():
    global _POOL
    if not DB_URL:
        raise RuntimeError("Postgres pool requested without a database URL")
    if _POOL is None:
        from psycopg_pool import ConnectionPool

        _POOL = ConnectionPool(
            DB_URL,
            min_size=1,
            max_size=5,
            kwargs={
                "autocommit": False,
                "prepare_threshold": None,
                # Notice a dead peer in about a minute instead of never.
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
            },
            # Neon closes idle connections. Check before checkout so a dead
            # one is discarded instead of failing the next statement.
            check=ConnectionPool.check_connection,
            max_idle=120,
            max_lifetime=600,
            # Fail a request in 10 s rather than hang a worker for 30. Raise it
            # for runs far from the database (e.g. tests from India to Oregon).
            timeout=float(os.getenv("DB_POOL_TIMEOUT", "10") or 10),
            open=True,
        )
    return _POOL


def _is_connection_error(error):
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, psycopg.OperationalError)


def get_conn():
    if BACKEND == "postgres":
        pool = _pool()
        return DatabaseConnection(pool.getconn(), postgres=True, pool=pool)
    raw = sqlite3.connect(DB_PATH, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    return DatabaseConnection(raw, postgres=False)


def init_db():
    conn = get_conn()
    try:
        if BACKEND == "postgres":
            for statement in POSTGRES_SCHEMA:
                conn.execute(statement)
            for table, column, column_type in ADDED_COLUMNS:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}"
                )
        else:
            conn.executescript(SQLITE_SCHEMA)
            for table, column, column_type in ADDED_COLUMNS:
                existing = {
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        conn.commit()
    finally:
        conn.close()


def reset_tables():
    """Remove every row. Tests use this so one shared Postgres branch stays isolated."""
    conn = get_conn()
    try:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


def prepare_test_db():
    """Create tables. On the shared Postgres test branch, also remove existing rows."""
    init_db()
    if BACKEND == "postgres":
        reset_tables()


def database_ok():
    conn = None
    try:
        conn = get_conn()
        conn.execute("SELECT 1")
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def _json(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def save_claim(claim_id, payload_dict):
    created_at = payload_dict.get("created_at") or datetime.now().isoformat()
    payload = json.dumps(payload_dict, ensure_ascii=False)
    conn = get_conn()
    try:
        if BACKEND == "postgres":
            conn.execute(
                """
                INSERT INTO claims (claim_id, created_at, payload_json)
                VALUES (%s, %s, %s)
                ON CONFLICT (claim_id) DO UPDATE SET
                    created_at = EXCLUDED.created_at,
                    payload_json = EXCLUDED.payload_json
                """,
                (claim_id, created_at, payload),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO claims (claim_id, created_at, payload_json) VALUES (?, ?, ?)",
                (claim_id, created_at, payload),
            )
        conn.commit()
    finally:
        conn.close()


def save_file(claim_id, filename, kind, path, source_sha256=None, source_bytes=None):
    """Register one claim input. source_sha256/source_bytes describe the original
    upload, so an identical resubmission is detectable after the bytes are gone."""
    values = (claim_id, filename, kind, path, source_sha256, source_bytes)
    conn = get_conn()
    try:
        if BACKEND == "postgres":
            cursor = conn.execute(
                """
                INSERT INTO files (claim_id, filename, kind, path, phash, source_sha256, source_bytes)
                VALUES (%s, %s, %s, %s, NULL, %s, %s)
                RETURNING id
                """,
                values,
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO files (claim_id, filename, kind, path, phash, source_sha256, source_bytes)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                values,
            )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def delete_files_for_claim(claim_id):
    """Remove file rows of a claim that was never saved, so a retry of the same
    photos is not matched against them by the duplicate gate."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM files WHERE claim_id = ?", (claim_id,))
        conn.commit()
    finally:
        conn.close()


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
    if BACKEND == "postgres":
        conn.execute(POSTGRES_SCHEMA[3])
        return
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
        values = (
            claim_id,
            actor,
            action,
            target,
            _json(before),
            _json(after),
            reason,
            timestamp,
        )
        if BACKEND == "postgres":
            cursor = conn.execute(
                """
                INSERT INTO audit_log (
                    claim_id, actor, action, target, before_json,
                    after_json, reason, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                values,
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO audit_log (
                    claim_id, actor, action, target, before_json,
                    after_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
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
    statement = POSTGRES_SCHEMA[2] if BACKEND == "postgres" else """
        CREATE TABLE IF NOT EXISTS vision_cache (
            content_hash  TEXT PRIMARY KEY,
            label         TEXT,
            response_json TEXT,
            provider      TEXT,
            model         TEXT,
            created_at    TEXT
        )
        """
    conn.execute(statement)


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
        values = (
            content_hash,
            label,
            json.dumps(response, ensure_ascii=False),
            provider,
            model,
            datetime.now().isoformat(),
        )
        if BACKEND == "postgres":
            conn.execute(
                """
                INSERT INTO vision_cache (
                    content_hash, label, response_json, provider, model, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_hash) DO NOTHING
                """,
                values,
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO vision_cache (
                    content_hash, label, response_json, provider, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        conn.commit()
    finally:
        conn.close()


def _ensure_processed_messages(conn):
    statement = POSTGRES_SCHEMA[4] if BACKEND == "postgres" else """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id   TEXT PRIMARY KEY,
            processed_at REAL
        )
        """
    conn.execute(statement)


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
        if BACKEND == "postgres":
            inserted = conn.execute(
                """
                INSERT INTO processed_messages (message_id, processed_at)
                VALUES (%s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
                """,
                (str(message_id), received_at),
            ).fetchone()
            stored = inserted is not None
        else:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages (message_id, processed_at)
                VALUES (?, ?)
                """,
                (str(message_id), received_at),
            )
            stored = cursor.rowcount == 1
        conn.commit()
        return stored
    finally:
        conn.close()


def media_put(data, mime, kind, claim_id):
    """Store processed bytes once, keyed by sha256. Returns the id, or None if rejected."""
    if data is None:
        return None
    if not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    else:
        data = bytes(data)
    if len(data) > MAX_MEDIA_BYTES:
        print(
            f"[media] rejecting {kind or 'blob'} for {claim_id or 'unknown'}: "
            f"{len(data)} bytes exceeds 2 MB",
            flush=True,
        )
        return None
    media_id = hashlib.sha256(data).hexdigest()
    conn = get_conn()
    try:
        values = (
            media_id,
            claim_id,
            kind,
            mime,
            data,
            datetime.now().isoformat(),
        )
        # Same bytes stored again keep the first row, but a claim id is adopted so
        # session cleanup never deletes a blob a claim now points at.
        conn.execute(
            """
            INSERT INTO media (id, claim_id, kind, mime, bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                claim_id = COALESCE(media.claim_id, EXCLUDED.claim_id)
            """,
            values,
        )
        conn.commit()
        return media_id
    finally:
        conn.close()


def _blob(value):
    if value is None:
        return b""
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value)


def media_get(media_id):
    if not media_id:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT bytes, mime FROM media WHERE id = ?",
            (str(media_id),),
        ).fetchone()
        if not row:
            return None
        return _blob(row["bytes"]), row["mime"] or "application/octet-stream"
    finally:
        conn.close()


def put_session(wa_id, state_json, updated_at):
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO wa_sessions (wa_id, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (wa_id) DO UPDATE SET
                state_json = EXCLUDED.state_json,
                updated_at = EXCLUDED.updated_at
            """,
            (str(wa_id), state_json, str(updated_at)),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(wa_id):
    if not wa_id:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT state_json FROM wa_sessions WHERE wa_id = ?",
            (str(wa_id),),
        ).fetchone()
        if not row:
            return None
        return row["state_json"]
    finally:
        conn.close()


def delete_session(wa_id):
    if not wa_id:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wa_sessions WHERE wa_id = ?", (str(wa_id),))
        conn.commit()
    finally:
        conn.close()


def list_sessions():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT wa_id, state_json FROM wa_sessions"
        ).fetchall()
        return [(row["wa_id"], row["state_json"]) for row in rows]
    finally:
        conn.close()


def clear_sessions():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wa_sessions")
        conn.commit()
    finally:
        conn.close()


def touch_last_inbound(wa_id, at):
    if not wa_id:
        return
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO wa_last_inbound (wa_id, at)
            VALUES (?, ?)
            ON CONFLICT (wa_id) DO UPDATE SET
                at = EXCLUDED.at
            """,
            (str(wa_id), str(at)),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_inbound(wa_id):
    if not wa_id:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT at FROM wa_last_inbound WHERE wa_id = ?",
            (str(wa_id),),
        ).fetchone()
        if not row:
            return None
        return row["at"]
    finally:
        conn.close()


def clear_last_inbound():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wa_last_inbound")
        conn.commit()
    finally:
        conn.close()


SESSION_MEDIA_COLUMNS = "id, wa_id, media_id, kind, sha256, page, source_bytes, received_at"


def add_session_media(wa_id, media_id, kind, sha256, page=0, source_bytes=None, received_at=None):
    """Append one item to a WhatsApp session. Returns the row id, or None when
    the same content (wa_id, sha256, page) is already in the session."""
    values = (
        str(wa_id), media_id, kind, sha256, int(page), source_bytes,
        time.time() if received_at is None else float(received_at),
    )
    conn = get_conn()
    try:
        if BACKEND == "postgres":
            row = conn.execute(
                """
                INSERT INTO wa_session_media
                    (wa_id, media_id, kind, sha256, page, source_bytes, received_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (wa_id, sha256, page) DO NOTHING
                RETURNING id
                """,
                values,
            ).fetchone()
            inserted = row["id"] if row else None
        else:
            cursor = conn.execute(
                """
                INSERT INTO wa_session_media
                    (wa_id, media_id, kind, sha256, page, source_bytes, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (wa_id, sha256, page) DO NOTHING
                """,
                values,
            )
            inserted = cursor.lastrowid if cursor.rowcount == 1 else None
        conn.commit()
        return inserted
    finally:
        conn.close()


def list_session_media(wa_id):
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT {SESSION_MEDIA_COLUMNS} FROM wa_session_media WHERE wa_id = ? ORDER BY id",
            (str(wa_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def session_media_activity():
    """{wa_id: latest received_at} for every number that has session media."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT wa_id, MAX(received_at) AS latest FROM wa_session_media GROUP BY wa_id"
        ).fetchall()
        return {row["wa_id"]: float(row["latest"] or 0) for row in rows}
    finally:
        conn.close()


def delete_session_media(wa_id, row_ids=None):
    """Delete a session's media rows (all, or the given ids) and any blob that
    nothing references any more. Returns the deleted rows."""
    conn = get_conn()
    try:
        rows = [
            dict(row) for row in conn.execute(
                f"SELECT {SESSION_MEDIA_COLUMNS} FROM wa_session_media WHERE wa_id = ?",
                (str(wa_id),),
            ).fetchall()
        ]
        if row_ids is not None:
            wanted = {int(value) for value in row_ids}
            rows = [row for row in rows if int(row["id"]) in wanted]
        for row in rows:
            conn.execute("DELETE FROM wa_session_media WHERE id = ?", (row["id"],))
        for media_id in {row["media_id"] for row in rows}:
            _delete_unreferenced_media(conn, media_id)
        conn.commit()
        return rows
    finally:
        conn.close()


def _delete_unreferenced_media(conn, media_id):
    conn.execute(
        """
        DELETE FROM media
        WHERE id = ? AND claim_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM wa_session_media WHERE media_id = ?)
        """,
        (media_id, media_id),
    )


SESSION_MEDIA_KINDS = ("photo", "document", "document_page")


def purge_orphan_session_media(older_than_seconds):
    """Delete session blobs (claim_id NULL) that no session row references and
    that are older than the given age. Returns the number of blobs removed."""
    cutoff = datetime.fromtimestamp(time.time() - older_than_seconds).isoformat()
    marks = ", ".join("?" for _ in SESSION_MEDIA_KINDS)
    conn = get_conn()
    try:
        cursor = conn.execute(
            f"""
            DELETE FROM media
            WHERE claim_id IS NULL AND kind IN ({marks}) AND created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM wa_session_media WHERE wa_session_media.media_id = media.id
              )
            """,
            (*SESSION_MEDIA_KINDS, cutoff),
        )
        conn.commit()
        return cursor.rowcount or 0
    finally:
        conn.close()


def clear_session_media():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wa_session_media")
        conn.commit()
    finally:
        conn.close()


def storage_stats():
    conn = get_conn()
    try:
        length = "octet_length(bytes)" if BACKEND == "postgres" else "LENGTH(bytes)"
        media_row = conn.execute(
            f"SELECT COALESCE(SUM({length}), 0) AS total FROM media"
        ).fetchone()
        claim_row = conn.execute(
            "SELECT COUNT(*) AS total FROM claims"
        ).fetchone()
        return {
            "media_bytes": int(media_row["total"] or 0),
            "claim_count": int(claim_row["total"] or 0),
        }
    finally:
        conn.close()


def list_claims():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT claim_id, created_at, payload_json FROM claims ORDER BY created_at DESC"
        ).fetchall()
    finally:
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
