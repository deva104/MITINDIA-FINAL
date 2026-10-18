import hashlib
import os
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
_CACHE_EVENTS = ContextVar("vision_cache_events", default=())
_FALLBACK_EVENTS = ContextVar("vision_fallback_events", default=())


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
    # affect the other.
    for provider_index, provider in enumerate(providers):
        try:
            call = PROVIDER_CALLS[provider]
            model = _model_for(provider)
            max_images = PROVIDER_MAX_IMAGES[provider]
        except KeyError:
            last_error = ValueError(f"unsupported vision provider: {provider}")
        else:
            if image_count > max_images:
                last_error = ValueError(
                    f"{provider} supports at most {max_images} images, got {image_count}"
                )
                print(
                    f"[vision] {label}: skipping {provider}, {image_count} images "
                    f"exceeds limit {max_images}",
                    flush=True,
                )
            else:
                for attempt in range(2):
                    try:
                        response = call(parts, model, label)
                    except Exception as error:
                        last_error = error
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
                    return response

        if provider_index + 1 < len(providers):
            next_provider = providers[provider_index + 1]
            _FALLBACK_EVENTS.set(
                _FALLBACK_EVENTS.get() + ({
                    "from": provider,
                    "to": next_provider,
                },)
            )
            print(
                f"[vision] {label}: falling back from {provider} to {next_provider}",
                flush=True,
            )

    if last_error is not None:
        raise last_error
    raise RuntimeError("No vision providers were configured")
