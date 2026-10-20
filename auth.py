"""Console sign-in.

Users come from CONSOLE_USERS, a JSON list of
{username, display_name, city, password_hash}. Hashes are PBKDF2-SHA256 with
a salt per user (scripts/hash_password.py prints one). A session is a signed
cookie: HttpOnly, Secure, SameSite=Lax, 12 hours. Plaintext passwords are
never stored or logged, and no log line carries a hash, a cookie or a key.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

COOKIE_NAME = "claimpilot_session"
SESSION_SECONDS = 12 * 60 * 60
ITERATIONS = 310_000
ALGORITHM = "pbkdf2_sha256"
# A mutating request signed in by cookie must also carry this header. A
# cross-site form cannot set a custom header, and the console's fetch does.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "InsureIT"
# Five failed sign-ins from one address within ten minutes lock it for ten.
MAX_FAILURES = 5
FAILURE_WINDOW = 10 * 60
LOCK_SECONDS = 10 * 60

_LOCK = threading.Lock()
_FAILURES = {}
_LOCKED_UNTIL = {}
_REVOKED = {}
_USERS_CACHE = {"raw": None, "users": {}}


def hash_password(password, salt=None, iterations=ITERATIONS):
    """"pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>"."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = str(encoded or "").split("$")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"),
                                     bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


# Checked against an unknown user name so a wrong name costs the same time.
_DUMMY_HASH = hash_password("not-a-user", salt=b"\x00" * 16)


def users():
    """{username (lower case): user} from CONSOLE_USERS. A malformed value
    gives no users and one warning that names no value."""
    raw = os.getenv("CONSOLE_USERS", "")
    if raw == _USERS_CACHE["raw"]:
        return _USERS_CACHE["users"]
    loaded = {}
    if raw.strip():
        try:
            for item in json.loads(raw):
                username = str(item.get("username") or "").strip()
                password_hash = str(item.get("password_hash") or "").strip()
                if not username or not password_hash.startswith(ALGORITHM + "$"):
                    continue
                loaded[username.lower()] = {
                    "username": username,
                    "display_name": str(item.get("display_name") or username).strip(),
                    "city": str(item.get("city") or "").strip(),
                    "password_hash": password_hash,
                }
        except (ValueError, TypeError, AttributeError):
            print("[auth] WARNING: CONSOLE_USERS is not a JSON list of users; nobody can "
                  "sign in", flush=True)
            loaded = {}
    _USERS_CACHE.update(raw=raw, users=loaded)
    return loaded


def user(username):
    return users().get(str(username or "").strip().lower())


def public(record):
    """A user without the hash."""
    if not record:
        return None
    return {key: record[key] for key in ("username", "display_name", "city")}


def display_name(actor):
    """The display name for an audit actor that is a console user, else the actor."""
    record = user(actor)
    return record["display_name"] if record else actor


def authenticate(username, password):
    record = user(username)
    if record is None:
        verify_password(password, _DUMMY_HASH)
        return None
    return record if verify_password(password, record["password_hash"]) else None


# --- sessions --------------------------------------------------------------

def _secret():
    """SESSION_SECRET, else one derived from ADMIN_TOKEN, so sessions survive a
    restart (Render's free tier spins down). None when neither is set."""
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return hashlib.sha256(b"claimpilot-session:" + configured.encode("utf-8")).digest()
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if token:
        return hashlib.sha256(b"claimpilot-session-from-admin:" + token.encode("utf-8")).digest()
    return None


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(username, now=None):
    secret = _secret()
    if secret is None:
        return None
    now = time.time() if now is None else now
    body = _b64(json.dumps({"u": username, "exp": int(now) + SESSION_SECONDS,
                            "n": secrets.token_hex(8)}, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def _claims(value, now):
    secret = _secret()
    if secret is None or not value or "." not in value:
        return None
    body, signature = value.rsplit(".", 1)
    expected = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or int(claims.get("exp") or 0) <= now:
        return None
    with _LOCK:
        if claims.get("n") in _REVOKED:
            return None
    return claims


def session_user(value, now=None):
    """The signed-in user for a cookie value, or None."""
    claims = _claims(value, time.time() if now is None else now)
    return user(claims.get("u")) if claims else None


def revoke(value, now=None):
    """Logout: the cookie is cleared, and this process refuses it until it expires."""
    now = time.time() if now is None else now
    claims = _claims(value, now)
    if not claims:
        return
    with _LOCK:
        _REVOKED[claims.get("n")] = int(claims.get("exp") or 0)
        for nonce, expires in list(_REVOKED.items()):
            if expires <= now:
                del _REVOKED[nonce]


def cookie_settings():
    return {"key": COOKIE_NAME, "max_age": SESSION_SECONDS, "httponly": True,
            "secure": True, "samesite": "lax", "path": "/"}


# --- failed sign-ins --------------------------------------------------------

def locked_for(address, now=None):
    """Seconds this address must still wait, or 0."""
    now = time.time() if now is None else now
    with _LOCK:
        until = _LOCKED_UNTIL.get(address, 0)
        if until <= now:
            _LOCKED_UNTIL.pop(address, None)
            return 0
        return int(until - now) + 1


def record_failure(address, now=None):
    """Returns the seconds of the lock this failure started, or 0."""
    now = time.time() if now is None else now
    with _LOCK:
        recent = [at for at in _FAILURES.get(address, []) if now - at < FAILURE_WINDOW]
        recent.append(now)
        if len(recent) >= MAX_FAILURES:
            _LOCKED_UNTIL[address] = now + LOCK_SECONDS
            _FAILURES.pop(address, None)
            return LOCK_SECONDS
        _FAILURES[address] = recent
        return 0


def record_success(address):
    with _LOCK:
        _FAILURES.pop(address, None)


def reset_state():
    """Tests: no failures, locks or revocations carried between them."""
    with _LOCK:
        _FAILURES.clear()
        _LOCKED_UNTIL.clear()
        _REVOKED.clear()
    _USERS_CACHE.update(raw=None, users={})
