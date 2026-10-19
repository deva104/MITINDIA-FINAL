try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import contextlib
import io
import os
import threading
import time
import unittest
from unittest.mock import patch

import vision

try:
    from helpers import fresh_key_state, provider_keys
except ImportError:
    from tests.helpers import fresh_key_state, provider_keys


class StatusError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status_code = status


def block(seconds):
    # time.sleep is patched out by these tests; a provider that hangs waits here.
    threading.Event().wait(seconds)


class ProviderTimeoutTests(unittest.TestCase):
    def run_chain(self, behaviours, cap=0.2, budget=0.6):
        calls = []

        def provider(name):
            def call(parts, model, label):
                calls.append(name)
                return behaviours[name](len([c for c in calls if c == name]))
            return call

        log = io.StringIO()
        with provider_keys(), fresh_key_state(), \
                patch.dict(vision.PROVIDER_CALLS, {name: provider(name) for name in behaviours}), \
                patch.dict(os.environ, {"VISION_PROVIDERS": ",".join(behaviours),
                                        "CACHE_ENABLED": "false"}), \
                patch.object(vision, "CALL_TIMEOUT_SECONDS", cap), \
                patch.object(vision, "PROVIDER_BUDGET_SECONDS", budget), \
                patch.object(vision.time, "sleep") as sleep, \
                contextlib.redirect_stdout(log):
            vision.reset_cache_tracking()
            started = time.monotonic()
            try:
                result = vision.generate_json([("text", "x")], "probe")
            except vision.VisionUnavailable as error:
                result = error
            elapsed = time.monotonic() - started
            fallbacks = list(vision.fallbacks_used())
        return result, calls, sleep, log.getvalue(), elapsed, fallbacks

    def test_a_timeout_moves_on_at_once_without_a_retry(self):
        def hang(n):
            block(2)
            return {"late": True}

        result, calls, sleep, log, elapsed, fallbacks = self.run_chain({
            "gemini": hang, "openrouter": lambda n: {"ok": "openrouter"}})
        self.assertEqual(result, {"ok": "openrouter"})
        self.assertEqual(calls, ["gemini", "openrouter"])  # no second gemini try
        sleep.assert_not_called()
        self.assertLess(elapsed, 1.0)  # the cap, not the 2 s hang
        self.assertIn("gemini timed out after 0s; moving on, no retry", log)
        self.assertEqual(fallbacks[0]["detail"], "timed out after 0s")

    def test_a_gemini_503_is_retried_once_after_two_seconds(self):
        def flaky(n):
            if n == 1:
                raise StatusError("503 UNAVAILABLE", 503)
            return {"ok": "gemini"}

        result, calls, sleep, log, _, _ = self.run_chain(
            {"gemini": flaky, "openrouter": lambda n: {"ok": "openrouter"}}, budget=25)
        self.assertEqual(result, {"ok": "gemini"})
        self.assertEqual(calls, ["gemini", "gemini"])
        sleep.assert_called_once_with(vision.GEMINI_RETRY_DELAY)
        self.assertIn("retrying once in 2s", log)

    def test_a_second_gemini_503_moves_on(self):
        def down(n):
            raise StatusError("503 UNAVAILABLE", 503)

        result, calls, _, _, _, _ = self.run_chain(
            {"gemini": down, "openrouter": lambda n: {"ok": "openrouter"}}, budget=25)
        self.assertEqual(result, {"ok": "openrouter"})
        self.assertEqual(calls, ["gemini", "gemini", "openrouter"])

    def test_an_openrouter_502_is_not_retried(self):
        def bad_gateway(n):
            raise StatusError("502 Bad Gateway", 502)

        result, calls, sleep, _, _, _ = self.run_chain(
            {"groq": lambda n: (_ for _ in ()).throw(StatusError("429 rate limit", 429)),
             "openrouter": bad_gateway}, budget=25)
        self.assertIsInstance(result, vision.VisionUnavailable)
        self.assertEqual(calls, ["groq", "openrouter"])
        sleep.assert_not_called()

    def test_no_provider_spends_more_than_its_budget(self):
        def slow_errors(n):
            block(0.15)
            raise RuntimeError("model returned prose instead of JSON")

        result, calls, _, log, elapsed, _ = self.run_chain(
            {"gemini": slow_errors, "openrouter": lambda n: {"ok": "openrouter"}},
            cap=0.2, budget=0.25)
        self.assertEqual(result, {"ok": "openrouter"})
        # First try 0.15 s; the second is capped by the 0.1 s left, then the budget ends it.
        self.assertLess(elapsed, 0.25 + 0.2)
        self.assertIn("gemini", calls)

    def test_sdk_timeouts_are_classified_as_timeouts(self):
        import httpx

        for error in (httpx.ReadTimeout("read timed out"), TimeoutError(),
                      vision.ProviderTimeout("no answer within 20s"),
                      RuntimeError("Deadline Exceeded")):
            self.assertEqual(vision.classify_provider_error(error)[0], "timeout", error)
        self.assertEqual(vision.classify_provider_error(StatusError("503", 503))[0], "server")

    def test_defaults_are_twenty_and_twenty_five_seconds(self):
        if not os.getenv("VISION_CALL_TIMEOUT"):
            self.assertEqual(vision.CALL_TIMEOUT_SECONDS, 20)
        if not os.getenv("VISION_PROVIDER_BUDGET"):
            self.assertEqual(vision.PROVIDER_BUDGET_SECONDS, 25)


if __name__ == "__main__":
    unittest.main()
