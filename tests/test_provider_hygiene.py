"""Provider errors found in the seed dry run: Groq 429s carried the account's
organisation id into assessor and customer text, and were retried uselessly."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import os
import unittest
from unittest.mock import patch

import vision
import whatsapp

try:
    from helpers import fresh_key_state, provider_keys
except ImportError:
    from tests.helpers import fresh_key_state, provider_keys


class RateLimitError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


GROQ_429 = RateLimitError(
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model `qwen` in "
    "organization `org_01m1bsbcr5esgv2b0r406s64m5` service tier `on_demand`'}}",
    429,
)


class ProviderHygieneTests(unittest.TestCase):
    def test_http_errors_never_repeat_the_provider_body(self):
        text = vision.short_error(GROQ_429)
        self.assertEqual(text, "RateLimitError (HTTP 429)")
        self.assertNotIn("org_", text)

    def test_plain_errors_drop_payloads_and_account_ids(self):
        error = RuntimeError("quota exhausted for org_abc123 {'detail': 'secret body'}")
        text = vision.short_error(error)
        self.assertEqual(text, "RuntimeError: quota exhausted for org_…")
        self.assertEqual(vision.short_error(RuntimeError("503 UNAVAILABLE")),
                         "RuntimeError: 503 UNAVAILABLE")

    def test_rate_limit_falls_back_at_once_without_a_retry(self):
        calls = []

        def groq(parts, model, label):
            calls.append("groq")
            raise GROQ_429

        def gemini(parts, model, label):
            calls.append("gemini")
            return {"ok": True}

        # One groq key: the 429 cools it and the chain moves on, no sleep, no retry.
        with provider_keys(groq=1, gemini=1), fresh_key_state(), \
                patch.dict(vision.PROVIDER_CALLS, {"groq": groq, "gemini": gemini}), \
                patch.dict(os.environ, {"VISION_PROVIDERS": "groq,gemini",
                                        "CACHE_ENABLED": "false"}), \
                patch.object(vision.time, "sleep") as sleep:
            vision.reset_cache_tracking()
            result = vision.generate_json([("text", "x")], "probe")
            fallbacks = list(vision.fallbacks_used())

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["groq", "gemini"])
        sleep.assert_not_called()
        self.assertEqual(fallbacks[0]["detail"], "RateLimitError (HTTP 429)")

    def test_customer_reply_never_names_internal_providers(self):
        lines = whatsapp._undetermined_lines([
            {"item": "Primary vision provider",
             "reason": "groq unavailable (RateLimitError (HTTP 429)), fell back to gemini",
             "impact": "result produced by gemini instead", "audience": "internal"},
            {"item": "Claim form", "reason": "required document not provided",
             "impact": "claim documentation incomplete",
             "audience": "customer_actionable", "request": "please send your claim form"},
        ])
        text = "\n".join(lines)
        self.assertNotIn("vision provider", text)
        self.assertNotIn("groq", text)
        self.assertIn("Please send your claim form.", text)
        self.assertEqual(whatsapp._undetermined_lines([
            {"item": "Primary vision provider", "reason": "x", "impact": "y"},
        ]), [])


class DeployedCommitTests(unittest.TestCase):
    def test_health_reports_the_deployed_commit(self):
        from fastapi.testclient import TestClient

        import main

        with patch.dict(os.environ, {"RENDER_GIT_COMMIT": "c10970b5a1f2e3d4"}):
            payload = TestClient(main.app).get("/api/health").json()
        self.assertEqual(payload["commit"], "c10970b")


if __name__ == "__main__":
    unittest.main()
