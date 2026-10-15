import base64
import json
import mimetypes
import os
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
PRIMARY_MODEL = "qwen/qwen3.6-27b"
MODEL_KEYWORDS = ("vision", "vl", "qwen", "llama", "scout")
VISION_KEYWORDS = ("vision", "vl", "qwen", "scout", "maverick")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def print_failure(check_number, error):
    print(f"CHECK {check_number}: FAIL")
    print("Full error:")
    print("".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip())


def is_not_found(error):
    return getattr(error, "status_code", None) == 404


def find_damage_photo():
    photos = [
        path
        for path in UPLOAD_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not photos:
        raise FileNotFoundError(f"No supported image found under {UPLOAD_DIR}")

    def priority(path):
        name = path.name.lower()
        likely_document = any(
            token in name for token in ("claim", "form", "rc", "dl", "document", "not_vehicle")
        )
        if "damage" in name:
            vehicle_rank = 0
        elif name in {"image.jpg", "image.jpeg", "image.png", "image.webp"}:
            vehicle_rank = 1
        elif any(token in name for token in ("car", "vehicle")):
            vehicle_rank = 2
        else:
            vehicle_rank = 3
        return (likely_document, vehicle_rank, path.stat().st_size, str(path))

    return min(photos, key=priority)


def image_data_url(path):
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def candidate_models(primary, vision_models):
    yield primary
    for model_id in vision_models:
        if model_id != primary:
            yield model_id


def call_with_404_fallback(primary, vision_models, request):
    errors = []
    for index, model_id in enumerate(candidate_models(primary, vision_models)):
        try:
            return model_id, request(model_id)
        except Exception as error:
            errors.append((model_id, error))
            if index == 0 and not is_not_found(error):
                raise
            print(f"Model {model_id!r} was unavailable: {error}")
            print("Trying a vision-capable model from CHECK 1...")

    details = "\n".join(f"{model_id}: {error}" for model_id, error in errors)
    raise RuntimeError(f"No candidate model worked.\n{details}")


def main():
    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing from .env")

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
    vision_models = []

    # CHECK 1: discover the live model IDs instead of relying on a static list.
    try:
        models = sorted(model.id for model in client.models.list().data)
        matching_models = [
            model_id
            for model_id in models
            if any(keyword in model_id.lower() for keyword in MODEL_KEYWORDS)
        ]
        vision_models = [
            model_id
            for model_id in matching_models
            if any(keyword in model_id.lower() for keyword in VISION_KEYWORDS)
        ]
        print("CHECK 1: PASS")
        print("Matching model IDs:")
        for model_id in matching_models:
            print(model_id)
    except Exception as error:
        print_failure(1, error)

    # CHECK 2: verify JSON mode with a text-only request.
    try:
        def text_request(model_id):
            return client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "user", "content": 'Return JSON only: {"ok": true}'},
                ],
                response_format={"type": "json_object"},
            )

        text_model, response = call_with_404_fallback(
            PRIMARY_MODEL, vision_models, text_request
        )
        parsed = json.loads(response.choices[0].message.content)
        print("CHECK 2: PASS")
        print(f"Model that worked: {text_model}")
        print(f"Parsed JSON: {parsed}")
    except Exception as error:
        print_failure(2, error)

    # CHECK 3: verify multimodal input and JSON mode together.
    try:
        photo_path = find_damage_photo()
        data_url = image_data_url(photo_path)

        def vision_request(model_id):
            return client.chat.completions.create(
                model=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Inspect this damage photo. Return JSON only with this shape: "
                                    '{"is_vehicle": bool, "colour": string, '
                                    '"parts_damaged": [string]}.'
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
            )

        started = time.perf_counter()
        vision_model, response = call_with_404_fallback(
            PRIMARY_MODEL, vision_models, vision_request
        )
        parsed = json.loads(response.choices[0].message.content)
        elapsed = time.perf_counter() - started
        print("CHECK 3: PASS")
        print(f"Photo: {photo_path}")
        print(f"Model that worked: {vision_model}")
        print(f"Parsed JSON: {parsed}")
        print(f"Elapsed seconds: {elapsed:.2f}")
    except Exception as error:
        print_failure(3, error)


if __name__ == "__main__":
    main()
