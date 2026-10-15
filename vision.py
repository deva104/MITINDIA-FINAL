import os
import time

from dotenv import load_dotenv

from providers import call_gemini, call_groq


load_dotenv()

DEFAULT_PROVIDERS = "groq,gemini"
PROVIDER_CALLS = {
    "groq": call_groq,
    "gemini": call_gemini,
}
MODEL_ENV = {
    "groq": ("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
    "gemini": ("GEMINI_VISION_MODEL", "gemini-3.6-flash"),
}


def _configured_providers():
    configured = os.getenv("VISION_PROVIDERS", DEFAULT_PROVIDERS)
    providers = [name.strip().lower() for name in configured.split(",") if name.strip()]
    if not providers:
        raise ValueError("VISION_PROVIDERS must contain at least one provider")
    return providers


def generate_json(parts, label) -> dict:
    providers = _configured_providers()
    last_error = None

    # Separate providers mean separate quota pools, so a 429 on one does not
    # affect the other.
    for provider_index, provider in enumerate(providers):
        try:
            call = PROVIDER_CALLS[provider]
            model_env, default_model = MODEL_ENV[provider]
            model = os.getenv(model_env, default_model)
        except KeyError:
            last_error = ValueError(f"unsupported vision provider: {provider}")
        else:
            for attempt in range(2):
                try:
                    return call(parts, model, label)
                except Exception as error:
                    last_error = error
                    if attempt == 0:
                        time.sleep(2)

        if provider_index + 1 < len(providers):
            next_provider = providers[provider_index + 1]
            print(
                f"[vision] {label}: falling back from {provider} to {next_provider}",
                flush=True,
            )

    if last_error is not None:
        raise last_error
    raise RuntimeError("No vision providers were configured")
