import hashlib
import os
import re
import time
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()

from db import cache_get, cache_put
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
    text = " ".join(str(error).split())
    text = re.split(r"[{\[]", text, maxsplit=1)[0].strip(" -:.,")
    text = re.sub(r"\borg_[A-Za-z0-9]+", "org_…", text)
    return (f"{name}: {text}" if text else name)[:limit]


def reset_cache_tracking():
    _CACHE_EVENTS.set(())
    _FALLBACK_EVENTS.set(())


def stage_was_cached():
    events = _CACHE_EVENTS.get()
    return bool(events) and all(events)


def fallbacks_used():
    return _FALLBACK_EVENTS.get()


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
    providers = _configured_providers()
    last_error = None
    first_provider = providers[0]
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
            return cached["response"]
    _record_cache_event(False)

    image_count = sum(
        isinstance(part, tuple) and len(part) == 3 and part[0] == "image"
        for part in parts
    )

    # Separate providers mean separate quota pools, so a 429 on one does not
    # affect the other. A capacity skip is normal routing; only a failure is
    # recorded as one.
    attempts = []
    # Held back until a provider answers: a chain that fails as a whole is
    # reported by the caller, and "fell back to X" would claim X produced it.
    pending_fallbacks = []
    for provider_index, provider in enumerate(providers):
        outcome = None
        try:
            call = PROVIDER_CALLS[provider]
            model = _model_for(provider)
            max_images = PROVIDER_MAX_IMAGES[provider]
        except KeyError:
            last_error = ValueError(f"unsupported vision provider: {provider}")
            outcome = ("failed", f"unsupported provider '{provider}'")
        else:
            if image_count > max_images:
                outcome = (
                    "skipped",
                    f"{image_count} images exceeds {provider} limit {max_images}",
                )
                print(f"[vision] {label}: skipped {provider}: {outcome[1]}", flush=True)
            else:
                for attempt in range(2):
                    try:
                        response = call(parts, model, label)
                    except Exception as error:
                        last_error = error
                        key_env = PROVIDER_KEYS.get(provider)
                        if isinstance(error, KeyError) and error.args[:1] == (key_env,):
                            outcome = ("skipped", f"{key_env} is not set")
                            break
                        outcome = ("failed", short_error(error))
                        if is_rate_limited(error):
                            # The provider has said when to come back; a 2 s
                            # retry only fails again. Use the next provider.
                            break
                        if attempt == 0:
                            time.sleep(2)
                        continue
                    if use_cache:
                        try:
                            cache_put(
                                content_hash,
                                label.split("(", 1)[0],
                                response,
                                provider,
                                model,
                            )
                        except Exception as error:
                            print(
                                f"[vision] {label}: cache write failed: {error}",
                                flush=True,
                            )
                    _FALLBACK_EVENTS.set(_FALLBACK_EVENTS.get() + tuple(pending_fallbacks))
                    return response
        attempts.append((provider, outcome[0], outcome[1]))

        if provider_index + 1 < len(providers):
            next_provider = providers[provider_index + 1]
            pending_fallbacks.append({
                "from": provider,
                "to": next_provider,
                "reason": outcome[0],
                "detail": outcome[1],
            })
            print(
                f"[vision] {label}: {provider} {outcome[0]} ({outcome[1]}), "
                f"falling back to {next_provider}",
                flush=True,
            )

    if attempts:
        raise VisionUnavailable(label, attempts) from last_error
    raise RuntimeError("No vision providers were configured")


def provider_status():
    """The resolved provider chain and which providers have keys. Never values."""
    warnings = []
    try:
        providers = _configured_providers()
    except ValueError as error:
        return {"chain": [], "warnings": [str(error)]}
    chain = []
    for provider in providers:
        if provider not in PROVIDER_CALLS:
            chain.append({"provider": provider, "supported": False})
            warnings.append(f"unknown provider '{provider}' in VISION_PROVIDERS")
            continue
        key_env = PROVIDER_KEYS[provider]
        has_key = bool(os.getenv(key_env, "").strip())
        chain.append({
            "provider": provider,
            "model": _model_for(provider),
            "key_configured": has_key,
            "max_images": PROVIDER_MAX_IMAGES[provider],
        })
        if not has_key:
            warnings.append(f"{provider} is in VISION_PROVIDERS but {key_env} is not set")
    for provider, key_env in PROVIDER_KEYS.items():
        if provider not in providers and os.getenv(key_env, "").strip():
            warnings.append(
                f"{key_env} is set but {provider} is not in VISION_PROVIDERS, "
                "so it is never used"
            )
    return {"chain": chain, "warnings": warnings}
