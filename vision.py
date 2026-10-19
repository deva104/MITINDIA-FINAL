import concurrent.futures
import contextvars
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()

from db import cache_get, cache_put
import providers
from providers import call_gemini, call_groq, call_openrouter

DEFAULT_PROVIDERS = "groq,gemini,openrouter"
PROVIDER_CALLS = {
    "groq": call_groq,
    "gemini": call_gemini,
    "openrouter": call_openrouter,
}
MODEL_ENV = {
    "groq": ("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
    "gemini": ("GEMINI_VISION_MODEL", "gemini-3.6-flash"),
    "openrouter": (
        "OPENROUTER_VISION_MODEL",
        "nvidia/nemotron-3.5-super-vl:free",
    ),
}
PROVIDER_MAX_IMAGES = {
    "groq": 3,
    "gemini": 16,
    # The live endpoint accepted a 16-image JSON-mode probe. This exceeds the
    # application's eight-page PDF cap without claiming an undocumented maximum.
    "openrouter": 16,
}
PROVIDER_KEYS = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
_CACHE_EVENTS = ContextVar("vision_cache_events", default=())
_FALLBACK_EVENTS = ContextVar("vision_fallback_events", default=())
# Which provider answered each call, and why earlier ones did not.
_PROVENANCE = ContextVar("vision_provenance", default=())


class VisionUnavailable(RuntimeError):
    """Every configured provider was skipped or failed. attempts lists why, in order."""

    def __init__(self, label, attempts):
        self.attempts = list(attempts)
        # Providers that failed the same way are named together, so the cause
        # stays short enough to show in full.
        groups = {}
        for provider, outcome, detail in self.attempts:
            groups.setdefault((outcome, detail), []).append(provider)
        summary = "; ".join(
            f"{', '.join(names)} {outcome}: {detail}"
            for (outcome, detail), names in groups.items()
        )
        super().__init__(f"no vision provider succeeded ({summary})")


def _status_code(error):
    for attribute in ("status_code", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def is_rate_limited(error):
    return _status_code(error) == 429 or "RateLimit" in type(error).__name__


def short_error(error, limit=160):
    """One line, bounded, and free of provider payloads: safe to show an
    assessor or log. HTTP errors become "Name (HTTP 503)"; a raw provider body
    (which can carry account ids) is never repeated."""
    name = type(error).__name__
    status = _status_code(error)
    if status is not None:
        return f"{name} (HTTP {status})"
    text = providers.redact(" ".join(str(error).split()))
    text = re.split(r"[{\[]", text, maxsplit=1)[0].strip(" -:.,")
    text = re.sub(r"\borg_[A-Za-z0-9]+", "org_…", text)
    return (f"{name}: {text}" if text else name)[:limit]


# Per-key state, in memory. After a restart a limited key is tried once more;
# the provider then answers 429 again and the key is marked again. Nothing
# is persisted on purpose.
_KEY_LOCK = threading.Lock()
_KEY_STATE = {}  # (provider, label) -> {"state": cooling|exhausted|invalid, "until": epoch|None}
DEFAULT_COOLDOWN_SECONDS = 60
# Seed only: wait for a cooling key instead of failing when every key is limited.
_WAIT = {"enabled": False, "max_total": 15 * 60, "max_step": 60}


def reset_key_states():
    with _KEY_LOCK:
        _KEY_STATE.clear()


def wait_for_keys(enabled=True, max_total=15 * 60):
    """Batch jobs (the demo seed) wait for the soonest key; the live pipeline
    never waits and falls through to the next provider at once."""
    _WAIT["enabled"] = bool(enabled)
    _WAIT["max_total"] = max_total


def key_state(provider, label, now=None):
    """("ok"|"cooling"|"exhausted"|"invalid", until-epoch-or-None)."""
    now = time.time() if now is None else now
    with _KEY_LOCK:
        entry = _KEY_STATE.get((provider, label))
        if not entry:
            return "ok", None
        if entry["state"] == "invalid":
            return "invalid", None
        if entry["until"] is not None and entry["until"] <= now:
            del _KEY_STATE[(provider, label)]
            return "ok", None
        return entry["state"], entry["until"]


def _mark_key(provider, label, state, until):
    with _KEY_LOCK:
        _KEY_STATE[(provider, label)] = {"state": state, "until": until}


def _clock(epoch):
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if moment - datetime.now(timezone.utc) > timedelta(hours=20):
        return moment.strftime("%Y-%m-%d %H:%MZ")
    return moment.strftime("%H:%MZ")


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch else None


_DAILY_MARKERS = (
    "perday", "per day", "per_day", "(rpd)", "(tpd)", "daily",
    "free-models-per-day", "requests per day", "tokens per day",
)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.I)
_RETRY_IN = re.compile(
    r"(?:retry|try again)\s+in\s+((?:\d+(?:\.\d+)?\s*[hms]\s*)+)", re.I)


def _retry_seconds(error, text):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
            if value is not None:
                return float(value)
        except (TypeError, ValueError, AttributeError):
            pass
    match = _RETRY_DELAY.search(text)
    if match:
        return float(match.group(1))
    match = _RETRY_IN.search(text)
    if match:
        total = 0.0
        for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", match.group(1)):
            total += float(number) * {"h": 3600, "m": 60, "s": 1}[unit]
        return total or None
    return None


def _next_utc_midnight(now):
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    midnight = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# A stuck provider costs at most this much before the next one is tried: a hard
# cap on every single call, and a budget per provider for one vision call
# (keys, retries and all).
CALL_TIMEOUT_SECONDS = float(os.getenv("VISION_CALL_TIMEOUT", "20") or 20)
PROVIDER_BUDGET_SECONDS = float(os.getenv("VISION_PROVIDER_BUDGET", "25") or 25)
GEMINI_RETRY_DELAY = 2.0
_CALL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="vision-call")
_TIMEOUT_NAMES = {"APITimeoutError", "Timeout", "TimeoutException", "ReadTimeout",
                  "ConnectTimeout", "WriteTimeout", "PoolTimeout", "DeadlineExceeded"}


class ProviderTimeout(Exception):
    """A provider gave no answer within the per-call cap."""


def _call_with_cap(call, parts, model, label, cap):
    """Run one provider call with a wall-clock cap. The SDK timeout is the
    same value, so a call that overran also ends on its own shortly after."""
    context = contextvars.copy_context()  # carries the key in use
    future = _CALL_POOL.submit(context.run, call, parts, model, label)
    try:
        return future.result(timeout=max(0.01, cap))
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise ProviderTimeout(f"no answer within {cap:.0f}s") from None


def _is_timeout(error, lowered):
    return (
        isinstance(error, (ProviderTimeout, TimeoutError))
        or type(error).__name__ in _TIMEOUT_NAMES
        or "timed out" in lowered or "deadline exceeded" in lowered
    )


def classify_provider_error(error, now=None):
    """What a provider error means for the key that made the call:
    ("minute", until) rate limit, cool the key and try the next key;
    ("daily", until)  quota used, exhaust the key and try the next key;
    ("invalid", None) 401/403, the key is unusable for this process;
    ("timeout", None) no answer in time: the next provider, no retry;
    ("server", None)  503/5xx/overloaded, the model is down for every key;
    ("other", None)   anything else (bad output)."""
    now = time.time() if now is None else now
    status = _status_code(error)
    text = " ".join(str(error).split())
    lowered = text.lower()
    if _is_timeout(error, lowered):
        return "timeout", None
    if status in (401, 403) or (
        status == 400 and ("api key not valid" in lowered or "api_key_invalid" in lowered)
    ):
        return "invalid", None
    if status == 429 or "resource_exhausted" in lowered or "rate limit" in lowered:
        delay = _retry_seconds(error, text)
        if any(marker in lowered for marker in _DAILY_MARKERS):
            return "daily", (now + delay) if delay else _next_utc_midnight(now)
        return "minute", now + (delay if delay else DEFAULT_COOLDOWN_SECONDS)
    if (status is not None and 500 <= status <= 599) or "overloaded" in lowered \
            or "unavailable" in lowered:
        return "server", None
    return "other", None


def reset_cache_tracking():
    _CACHE_EVENTS.set(())
    _FALLBACK_EVENTS.set(())
    _PROVENANCE.set(())


def stage_was_cached():
    events = _CACHE_EVENTS.get()
    return bool(events) and all(events)


def fallbacks_used():
    return _FALLBACK_EVENTS.get()


def provenance():
    return _PROVENANCE.get()


def _record_provenance(label, provider, fallbacks, cached=False):
    _PROVENANCE.set(_PROVENANCE.get() + ({
        "call": label.split("(", 1)[0],
        "provider": provider,
        "cached": cached,
        "fallbacks": [dict(item) for item in fallbacks],
    },))


def _why(fallback):
    detail = str(fallback.get("detail") or "")
    name = fallback.get("from")
    if "(HTTP 429)" in detail or "RateLimit" in detail or "keys limited" in detail:
        return f"{name} rate-limited"
    if "exceeds" in detail:
        return f"{name} skipped: image limit"
    if detail.endswith("is not set"):
        return f"{name} not configured"
    return f"{name} unavailable"


def summarise_provenance(events):
    """One line for the pipeline trace, e.g. "via gemini (groq rate-limited)".
    None when the stage made no vision call."""
    events = list(events or [])
    if not events:
        return None
    if all(event.get("cached") for event in events):
        return "cached"
    providers, reasons = [], []
    for event in events:
        provider = "cache" if event.get("cached") else event.get("provider")
        if provider and provider not in providers:
            providers.append(provider)
        for fallback in event.get("fallbacks") or []:
            reason = _why(fallback)
            if reason not in reasons:
                reasons.append(reason)
    text = "via " + ", ".join(providers)
    return text + (f" ({'; '.join(reasons)})" if reasons else "")


def _record_cache_event(cached):
    _CACHE_EVENTS.set(_CACHE_EVENTS.get() + (bool(cached),))


def _cache_enabled():
    return os.getenv("CACHE_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _model_for(provider):
    model_env, default_model = MODEL_ENV[provider]
    return os.getenv(model_env, default_model)


def _content_hash(parts, label, first_provider, first_model):
    digest = hashlib.sha256()

    def add(value):
        data = value if isinstance(value, bytes) else str(value).encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    add(label)
    add(first_provider)
    add(first_model)
    for part in parts:
        if not isinstance(part, tuple) or not part:
            raise ValueError(f"invalid provider-neutral part: {part!r}")
        if part[0] == "text" and len(part) == 2:
            add("text")
            add(part[1])
        elif part[0] == "image" and len(part) == 3:
            add("image")
            add(part[2])
            add(part[1])
        else:
            raise ValueError(f"invalid provider-neutral part: {part!r}")
    return digest.hexdigest()


def _configured_providers():
    configured = os.getenv("VISION_PROVIDERS", DEFAULT_PROVIDERS)
    providers = [name.strip().lower() for name in configured.split(",") if name.strip()]
    if not providers:
        raise ValueError("VISION_PROVIDERS must contain at least one provider")
    return providers


def generate_json(parts, label) -> dict:
    chain = _configured_providers()
    last_error = None
    first_provider = chain[0]
    try:
        first_model = _model_for(first_provider)
    except KeyError:
        first_model = f"unsupported:{first_provider}"
    content_hash = _content_hash(parts, label, first_provider, first_model)
    use_cache = _cache_enabled()
    if use_cache:
        try:
            cached = cache_get(content_hash)
        except Exception as error:
            print(f"[vision] {label}: cache read failed: {error}", flush=True)
            cached = None
        if cached is not None:
            _record_cache_event(True)
            print(
                f"[vision] {label}: cache hit "
                f"({cached['provider']}/{cached['model']})",
                flush=True,
            )
            _record_provenance(label, cached.get("provider"), [], cached=True)
            return cached["response"]
    _record_cache_event(False)

    image_count = sum(
        isinstance(part, tuple) and len(part) == 3 and part[0] == "image"
        for part in parts
    )
    waited = 0.0
    while True:
        result = _run_chain(parts, label, chain, image_count)
        if result[0] == "ok":
            _, response, provider, model, fallbacks = result
            if use_cache:
                try:
                    cache_put(content_hash, label.split("(", 1)[0], response, provider, model)
                except Exception as error:
                    print(f"[vision] {label}: cache write failed: {short_error(error)}", flush=True)
            _FALLBACK_EVENTS.set(_FALLBACK_EVENTS.get() + tuple(fallbacks))
            _record_provenance(label, provider, fallbacks)
            return response
        _, attempts, last_error, all_limited = result
        pause = _seconds_until_a_key_returns(chain) if all_limited else None
        if (
            _WAIT["enabled"] and pause is not None
            and waited + pause <= _WAIT["max_total"]
        ):
            step = max(1.0, min(pause, _WAIT["max_step"]))
            print(f"[vision] {label}: every key is limited; waiting {step:.0f}s", flush=True)
            time.sleep(step)
            waited += step
            continue
        if attempts:
            # From None: the provider's own error text can carry account ids.
            raise VisionUnavailable(label, attempts) from None
        raise RuntimeError("No vision providers were configured")


def _seconds_until_a_key_returns(providers_):
    now = time.time()
    soonest = None
    for provider in providers_:
        if provider not in PROVIDER_CALLS:
            continue
        for label, _ in providers.discover_keys(provider):
            state, until = key_state(provider, label, now)
            if state == "ok":
                return 0.0
            if state in ("cooling", "exhausted") and until is not None:
                soonest = until if soonest is None else min(soonest, until)
    return None if soonest is None else max(0.0, soonest - now)


def _run_chain(parts, label, providers_, image_count):
    """One pass over the provider chain, rotating keys inside each provider.
    ("ok", response, provider, model, fallbacks) or
    ("fail", attempts, last_error, every_provider_was_rate_limited)."""
    last_error = None
    attempts = []
    # Held back until a provider answers: a chain that fails as a whole is
    # reported by the caller, and "fell back to X" would claim X produced it.
    pending_fallbacks = []
    limited_only = True
    for provider_index, provider in enumerate(providers_):
        try:
            call = PROVIDER_CALLS[provider]
            model = _model_for(provider)
            max_images = PROVIDER_MAX_IMAGES[provider]
        except KeyError:
            last_error = ValueError(f"unsupported vision provider: {provider}")
            outcome = ("failed", f"unsupported provider '{provider}'")
            limited_only = False
        else:
            if image_count > max_images:
                outcome = (
                    "skipped",
                    f"{image_count} images exceeds {provider} limit {max_images}",
                )
                print(f"[vision] {label}: skipped {provider}: {outcome[1]}", flush=True)
            else:
                response, outcome, error, rate_limited = _try_provider(
                    provider, call, model, parts, label
                )
                if error is not None:
                    last_error = error
                if response is not None:
                    return "ok", response, provider, model, pending_fallbacks
                if not rate_limited:
                    limited_only = False
        attempts.append((provider, outcome[0], outcome[1]))
        if provider_index + 1 < len(providers_):
            next_provider = providers_[provider_index + 1]
            pending_fallbacks.append({
                "from": provider, "to": next_provider,
                "reason": outcome[0], "detail": outcome[1],
            })
            print(f"[vision] {label}: {provider} {outcome[0]} ({outcome[1]}), "
                  f"falling back to {next_provider}", flush=True)
    return "fail", attempts, last_error, limited_only and bool(attempts)


def _try_provider(provider, call, model, parts, label):
    """Try one provider, key by key. Returns (response|None, outcome, error,
    rate_limited) where rate_limited means every key ended up limited."""
    keys = providers.discover_keys(provider) if provider in providers.KEY_PREFIXES else []
    deadline = time.monotonic() + PROVIDER_BUDGET_SECONDS
    if not keys:
        # No pool: behave as before, on the bare variable.
        return _attempt(provider, call, model, parts, label, None, deadline)
    now = time.time()
    usable = [(key_label, value) for key_label, value in keys
              if key_state(provider, key_label, now)[0] == "ok"]
    if not usable:
        return None, ("skipped", f"all {len(keys)} {provider} keys limited"), None, True
    last = (None, ("failed", "no key answered"), None, True)
    for key_label, value in usable:
        token = providers.use_key(provider, key_label, value)
        try:
            response, outcome, error, rate_limited = _attempt(
                provider, call, model, parts, label, key_label, deadline
            )
        finally:
            providers.release_key(token)
        if response is not None:
            return response, outcome, error, False
        last = (None, outcome, error, rate_limited)
        if outcome[0] == "rotate":
            continue
        return last
    remaining = sum(1 for key_label, _ in keys if key_state(provider, key_label)[0] == "ok")
    detail = last[1][1]
    if not remaining and len(keys) > 1:
        detail = f"all {len(keys)} {provider} keys limited (last: {detail})"
    return None, ("failed", detail), last[2], not remaining


def _attempt(provider, call, model, parts, label, key_label, deadline):
    """One key (or the bare variable): up to two tries for an ordinary
    failure; none for a rate limit, an invalid key or a timeout; one retry
    after 2 s for a gemini 5xx. Every try is capped at CALL_TIMEOUT_SECONDS
    and all of them together by the provider's deadline."""
    outcome, error = ("failed", "no attempt"), None
    tries, server_retried = 0, False
    while tries < 2:
        cap = min(CALL_TIMEOUT_SECONDS, deadline - time.monotonic())
        if cap <= 0:
            print(f"[vision] {label}: {provider} used its {PROVIDER_BUDGET_SECONDS:.0f}s "
                  "budget; moving on", flush=True)
            return None, ("failed", f"no answer within {PROVIDER_BUDGET_SECONDS:.0f}s"), \
                error, False
        try:
            return _call_with_cap(call, parts, model, label, cap), ("ok", ""), None, False
        except Exception as caught:
            error = caught
        key_env = PROVIDER_KEYS.get(provider)
        if isinstance(error, KeyError) and error.args[:1] == (key_env,):
            return None, ("skipped", f"{key_env} is not set"), error, False
        kind, until = classify_provider_error(error)
        detail = short_error(error)
        if kind in ("minute", "daily") and key_label is not None:
            state = "cooling" if kind == "minute" else "exhausted"
            _mark_key(provider, key_label, state, until)
            print(f"[vision] {provider} key '{key_label}' {state} until {_clock(until)}, "
                  "rotating", flush=True)
            return None, ("rotate", detail), error, True
        if kind == "invalid" and key_label is not None:
            _mark_key(provider, key_label, "invalid", None)
            print(f"[vision] WARNING: {provider} key '{key_label}' was rejected "
                  f"({detail}); marked invalid until restart, rotating", flush=True)
            return None, ("rotate", detail), error, False
        if kind in ("minute", "daily"):
            return None, ("failed", detail), error, True
        if kind == "timeout":
            print(f"[vision] {label}: {provider} timed out after {cap:.0f}s; "
                  "moving on, no retry", flush=True)
            return None, ("failed", f"timed out after {cap:.0f}s"), error, False
        if kind == "server" and provider == "gemini" and not server_retried \
                and deadline - time.monotonic() > GEMINI_RETRY_DELAY:
            server_retried = True
            print(f"[vision] {label}: gemini {detail}; retrying once in "
                  f"{GEMINI_RETRY_DELAY:.0f}s", flush=True)
            time.sleep(GEMINI_RETRY_DELAY)
            continue
        if kind in ("server", "invalid"):
            return None, ("failed", detail), error, False
        outcome = ("failed", detail)
        tries += 1
        if tries < 2:
            time.sleep(2)
    return None, outcome, error, False


def provider_status():
    """The resolved provider chain, each provider's key labels and their
    state. Labels and states only, never a key value."""
    warnings = []
    try:
        configured = _configured_providers()
    except ValueError as error:
        return {"chain": [], "warnings": [str(error)]}
    chain = []
    for provider in configured:
        if provider not in PROVIDER_CALLS:
            chain.append({"provider": provider, "supported": False})
            warnings.append(f"unknown provider '{provider}' in VISION_PROVIDERS")
            continue
        key_env = PROVIDER_KEYS[provider]
        keys = providers.discover_keys(provider)
        now = time.time()
        key_states = []
        for key_label, _ in keys:
            state, until = key_state(provider, key_label, now)
            entry = {"label": key_label, "state": state}
            if until is not None:
                entry["until"] = _iso(until)
            key_states.append(entry)
            if state == "invalid":
                warnings.append(f"{provider} key '{key_label}' was rejected (invalid)")
        chain.append({
            "provider": provider,
            "model": _model_for(provider),
            "key_configured": bool(keys),
            "key_count": len(keys),
            "keys": key_states,
            "max_images": PROVIDER_MAX_IMAGES[provider],
        })
        if not keys:
            warnings.append(f"{provider} is in VISION_PROVIDERS but {key_env} is not set")
    for provider, key_env in PROVIDER_KEYS.items():
        if provider not in configured and providers.discover_keys(provider):
            warnings.append(
                f"{key_env} is set but {provider} is not in VISION_PROVIDERS, "
                "so it is never used"
            )
    return {"chain": chain, "warnings": warnings}
