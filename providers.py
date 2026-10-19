import base64
import hashlib
import json
import os
import re
import threading
import time
from contextvars import ContextVar

from google import genai
from google.genai import types
from openai import OpenAI


TEMPERATURE = 0.0
# Every provider call is bounded. Without this the OpenAI SDK waits up to 600 s
# and retries twice on its own; vision.generate_json already retries.
VISION_TIMEOUT_SECONDS = float(os.getenv("VISION_TIMEOUT_SECONDS", "20") or 20)

KEY_PREFIXES = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
# (provider, label, value) of the key the current call should use. Set by
# vision.generate_json around each attempt; empty means the bare env var.
_CURRENT_KEY = ContextVar("vision_current_key", default=None)
_CLIENTS = {}
_CLIENTS_LOCK = threading.Lock()
# Shapes of provider keys, masked or not, as they can appear in error text.
_KEY_PATTERN = re.compile(r"(?:sk-or-|sk-|gsk_|AIza)[A-Za-z0-9_\-\.\*]{4,}")


def discover_keys(provider):
    """[(label, value)] for every non-empty PREFIX or PREFIX_<suffix> env var.

    The bare name comes first (label "main"), then suffixed names in
    alphabetical order, labelled by the suffix in lower case. Labels are for
    logs and health only; values never leave this module's callers. A value
    seen twice is used once.
    """
    prefix = KEY_PREFIXES[provider]
    keys, seen = [], set()
    bare = os.environ.get(prefix, "").strip()
    if bare:
        keys.append(("main", bare))
        seen.add(bare)
    stem = prefix + "_"
    names = sorted(
        (name for name in os.environ if name.upper().startswith(stem) and len(name) > len(stem)),
        key=str.upper,
    )
    labels = {"main"}
    for name in names:
        value = os.environ.get(name, "").strip()
        if not value or value in seen:
            continue
        label = name[len(stem):].lower()
        while label in labels:
            label += "-2"
        labels.add(label)
        seen.add(value)
        keys.append((label, value))
    return keys


def use_key(provider, label, value):
    """Make the next call for provider use this key. Returns a reset token."""
    return _CURRENT_KEY.set((provider, label, value))


def release_key(token):
    _CURRENT_KEY.reset(token)


def current_key_label(provider):
    current = _CURRENT_KEY.get()
    return current[1] if current and current[0] == provider else None


def _key_for(provider):
    current = _CURRENT_KEY.get()
    if current and current[0] == provider:
        return current[2]
    # No pool in use: the bare variable, raising KeyError when unset exactly
    # as before, which the chain reports as "not set".
    return os.environ[KEY_PREFIXES[provider]]


def redact(text):
    """Remove every configured key value and anything key-shaped."""
    text = str(text)
    for prefix in KEY_PREFIXES.values():
        for name, value in os.environ.items():
            if name.upper().startswith(prefix) and value and len(value.strip()) >= 8:
                text = text.replace(value.strip(), "[key]")
    return _KEY_PATTERN.sub("[key]", text)


def _client(provider, build):
    value = _key_for(provider)
    fingerprint = (provider, hashlib.sha256(value.encode("utf-8")).hexdigest())
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(fingerprint)
        if client is None:
            client = build(value)
            _CLIENTS[fingerprint] = client
    return client


def _strip_fences(text):
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1] if "\n" in value else value[3:]
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()


def _get_groq_client():
    return _client("groq", lambda key: OpenAI(
        api_key=key,
        base_url="https://api.groq.com/openai/v1",
        timeout=VISION_TIMEOUT_SECONDS,
        max_retries=0,
    ))


def _get_gemini_client():
    return _client("gemini", lambda key: genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=int(VISION_TIMEOUT_SECONDS * 1000)),
    ))


def _get_openrouter_client():
    return _client("openrouter", lambda key: OpenAI(
        api_key=key,
        base_url="https://openrouter.ai/api/v1",
        timeout=VISION_TIMEOUT_SECONDS,
        max_retries=0,
        default_headers={
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "ClaimPilot",
            "X-Title": "ClaimPilot",
        },
    ))


def _key_note(provider):
    key = current_key_label(provider)
    return f" key '{key}'" if key else ""


def _log_failure(provider, model, label, started, error):
    """One sanitised line. A raw traceback can carry a provider's error body,
    and some providers echo a masked key in it."""
    elapsed = time.perf_counter() - started
    key = current_key_label(provider)
    via = f"{provider}/{model}" + (f" key '{key}'" if key else "")
    detail = redact(" ".join(str(error).split()))[:200]
    print(f"[vision] {label}: failed after {elapsed:.2f}s via {via}: "
          f"{type(error).__name__}: {detail}", flush=True)


def _openai_content(parts):
    content = []
    for part in parts:
        if not isinstance(part, tuple) or not part:
            raise ValueError(f"invalid provider-neutral part: {part!r}")
        if part[0] == "text" and len(part) == 2:
            content.append({"type": "text", "text": str(part[1])})
        elif part[0] == "image" and len(part) == 3:
            encoded = base64.b64encode(part[1]).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{part[2]};base64,{encoded}"},
            })
        else:
            raise ValueError(f"invalid provider-neutral part: {part!r}")
    return content


def _gemini_content(parts):
    content = []
    for part in parts:
        if not isinstance(part, tuple) or not part:
            raise ValueError(f"invalid provider-neutral part: {part!r}")
        if part[0] == "text" and len(part) == 2:
            content.append(str(part[1]))
        elif part[0] == "image" and len(part) == 3:
            content.append(types.Part.from_bytes(data=part[1], mime_type=part[2]))
        else:
            raise ValueError(f"invalid provider-neutral part: {part!r}")
    return content


def call_groq(parts, model, label) -> dict:
    started = time.perf_counter()
    try:
        response = _get_groq_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _openai_content(parts)}],
            response_format={"type": "json_object"},
            temperature=TEMPERATURE,
        )
        result = json.loads(_strip_fences(response.choices[0].message.content))
        if not isinstance(result, dict):
            raise ValueError(f"expected a JSON object, got {type(result).__name__}")
        elapsed = time.perf_counter() - started
        print(f"[vision] {label}: {elapsed:.2f}s via groq/{model}{_key_note('groq')}", flush=True)
        return result
    except Exception as error:
        _log_failure("groq", model, label, started, error)
        raise


def call_gemini(parts, model, label) -> dict:
    started = time.perf_counter()
    try:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=TEMPERATURE,
        )
        response = _get_gemini_client().models.generate_content(
            model=model,
            contents=_gemini_content(parts),
            config=config,
        )
        result = json.loads(_strip_fences(response.text))
        if not isinstance(result, dict):
            raise ValueError(f"expected a JSON object, got {type(result).__name__}")
        elapsed = time.perf_counter() - started
        print(f"[vision] {label}: {elapsed:.2f}s via gemini/{model}{_key_note('gemini')}", flush=True)
        return result
    except Exception as error:
        _log_failure("gemini", model, label, started, error)
        raise


def call_openrouter(parts, model, label) -> dict:
    started = time.perf_counter()
    try:
        response = _get_openrouter_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": _openai_content(parts)}],
            response_format={"type": "json_object"},
            temperature=TEMPERATURE,
        )
        result = json.loads(_strip_fences(response.choices[0].message.content))
        if not isinstance(result, dict):
            raise ValueError(f"expected a JSON object, got {type(result).__name__}")
        elapsed = time.perf_counter() - started
        print(
            f"[vision] {label}: {elapsed:.2f}s via openrouter/{model}{_key_note('openrouter')}",
            flush=True,
        )
        return result
    except Exception as error:
        _log_failure("openrouter", model, label, started, error)
        raise
