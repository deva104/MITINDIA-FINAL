import base64
import json
import os
import time
import traceback

from google import genai
from google.genai import types
from openai import OpenAI


TEMPERATURE = 0.0

_groq_client = None
_gemini_client = None
_openrouter_client = None


def _strip_fences(text):
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1] if "\n" in value else value[3:]
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        _groq_client = OpenAI(
            api_key=os.environ["GROQ_API_KEY"],
            base_url="https://api.groq.com/openai/v1",
        )
    return _groq_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def _get_openrouter_client():
    global _openrouter_client
    if _openrouter_client is None:
        api_key = os.environ["OPENROUTER_API_KEY"]
        _openrouter_client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "ClaimPilot",
                "X-Title": "ClaimPilot",
            },
        )
    return _openrouter_client


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
        print(f"[vision] {label}: {elapsed:.2f}s via groq/{model}", flush=True)
        return result
    except Exception:
        elapsed = time.perf_counter() - started
        print(f"[vision] {label}: failed after {elapsed:.2f}s via groq/{model}", flush=True)
        traceback.print_exc()
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
        print(f"[vision] {label}: {elapsed:.2f}s via gemini/{model}", flush=True)
        return result
    except Exception:
        elapsed = time.perf_counter() - started
        print(f"[vision] {label}: failed after {elapsed:.2f}s via gemini/{model}", flush=True)
        traceback.print_exc()
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
            f"[vision] {label}: {elapsed:.2f}s via openrouter/{model}",
            flush=True,
        )
        return result
    except Exception:
        elapsed = time.perf_counter() - started
        print(
            f"[vision] {label}: failed after {elapsed:.2f}s via openrouter/{model}",
            flush=True,
        )
        traceback.print_exc()
        raise
