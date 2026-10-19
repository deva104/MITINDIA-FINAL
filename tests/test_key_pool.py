"""Provider key pool: discovery, rotation, state, logging. Fake keys and fake
providers only; no real call is ever made."""
try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import contextlib
import io
import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import providers
import vision

FAKE = {
    "GEMINI_API_KEY": "AIzaFAKEmain0000000000000000000001",
    "GEMINI_API_KEY_Shrav": "AIzaFAKEshrav000000000000000000002",
    "GEMINI_API_KEY_Talha": "AIzaFAKEtalha000000000000000000003",
    "GEMINI_API_KEY_Pranav": "AIzaFAKEpranav00000000000000000004",
    "OPENROUTER_API_KEY": "sk-or-FAKErouter000000000000000005",
}
PROVIDER_PREFIXES = tuple(providers.KEY_PREFIXES.values())


class ProviderError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def per_minute():
    return ProviderError(
        "429 RESOURCE_EXHAUSTED. {'error': {'details': [{'quotaId': "
        "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}, "
        "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '17s'}]}}", 429)


def per_day():
    return ProviderError(
        "429 RESOURCE_EXHAUSTED. {'error': {'details': [{'quotaId': "
        "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}, "
        "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '64425s'}]}}", 429)


def overloaded():
    return ProviderError("503 UNAVAILABLE. The model is overloaded. Please try again later.", 503)


def unauthorised(key):
    # Some providers echo the (masked) key; the value must still never be logged.
    return ProviderError(f"401 Invalid API key provided: {key}", 401)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class KeyPoolCase(unittest.TestCase):
    """Env holds only fake keys; the chain is gemini -> openrouter."""

    def setUp(self):
        env = {name: value for name, value in os.environ.items()
               if not name.upper().startswith(PROVIDER_PREFIXES)}
        env.update(FAKE)
        env.update({"VISION_PROVIDERS": "gemini,openrouter", "CACHE_ENABLED": "false"})
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        vision.reset_key_states()
        vision.wait_for_keys(False)
        self.clock = Clock()
        self.calls = []
        self.behaviour = {}
        self.log = io.StringIO()
        self.patches = [
            patch.object(vision.time, "time", self.clock),
            patch.object(vision.time, "sleep", self.sleep),
            patch.dict(vision.PROVIDER_CALLS, {
                "gemini": self.fake("gemini"), "openrouter": self.fake("openrouter"),
            }),
        ]
        for item in self.patches:
            item.start()
        self.slept = []

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.env.stop()
        vision.reset_key_states()
        vision.wait_for_keys(False)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock.now += seconds

    def fake(self, provider):
        def call(parts, model, label):
            key = providers.current_key_label(provider)
            self.calls.append((provider, key))
            action = self.behaviour.get((provider, key))
            if callable(action):
                raise action()
            if isinstance(action, Exception):
                raise action
            return {"provider": provider, "key": key}
        return call

    def ask(self):
        with contextlib.redirect_stdout(self.log):
            return vision.generate_json([("text", "x")], "probe")

    def states(self, provider="gemini"):
        return {key["label"]: key["state"]
                for item in vision.provider_status()["chain"] if item["provider"] == provider
                for key in item["keys"]}


class DiscoveryTests(KeyPoolCase):
    def test_bare_first_then_suffixes_alphabetically_labels_only(self):
        self.assertEqual([label for label, _ in providers.discover_keys("gemini")],
                         ["main", "pranav", "shrav", "talha"])

    def test_f_suffixed_keys_work_without_the_bare_name(self):
        del os.environ["GEMINI_API_KEY"]
        self.assertEqual([label for label, _ in providers.discover_keys("gemini")],
                         ["pranav", "shrav", "talha"])
        self.assertEqual(self.ask(), {"provider": "gemini", "key": "pranav"})

    def test_provider_with_no_keys_behaves_as_before(self):
        for name in [n for n in os.environ if n.upper().startswith("GEMINI_API_KEY")]:
            del os.environ[name]
        self.assertEqual(providers.discover_keys("gemini"), [])
        # No pool: the call runs on the bare variable (the fake ignores keys).
        self.assertEqual(self.ask()["provider"], "gemini")
        self.assertEqual(self.calls, [("gemini", None)])


class RotationTests(KeyPoolCase):
    def test_a_per_minute_limit_rotates_and_cools_the_key(self):
        self.behaviour[("gemini", "main")] = per_minute
        self.assertEqual(self.ask(), {"provider": "gemini", "key": "pranav"})
        self.assertEqual(self.calls, [("gemini", "main"), ("gemini", "pranav")])
        self.assertEqual(self.states()["main"], "cooling")

        self.calls.clear()
        self.ask()
        self.assertEqual(self.calls, [("gemini", "pranav")])  # main skipped, no call spent

        self.clock.now += 18  # retryDelay was 17 s
        del self.behaviour[("gemini", "main")]
        self.calls.clear()
        self.assertEqual(self.ask(), {"provider": "gemini", "key": "main"})
        self.assertEqual(self.states()["main"], "ok")

    def test_b_daily_quota_exhausts_the_key_for_the_window(self):
        self.behaviour[("gemini", "main")] = per_day
        self.ask()
        self.assertEqual(self.states()["main"], "exhausted")
        until = next(key["until"] for item in vision.provider_status()["chain"]
                     for key in item.get("keys", []) if key["label"] == "main"
                     and item["provider"] == "gemini")
        self.assertEqual(until, vision._iso(1_800_000_000.0 + 64425))
        for hours in (1, 10, 17):
            self.clock.now = 1_800_000_000.0 + hours * 3600
            self.calls.clear()
            self.ask()
            self.assertNotIn(("gemini", "main"), self.calls)

    def test_daily_without_retry_info_lasts_until_midnight_utc(self):
        kind, until = vision.classify_provider_error(
            ProviderError("429 Rate limit exceeded: free-models-per-day", 429), now=1_800_000_000.0)
        self.assertEqual(kind, "daily")
        self.assertEqual(vision._iso(until)[11:19], "00:00:00")

    def test_c_server_outage_moves_to_the_next_provider_without_rotating(self):
        self.behaviour[("gemini", "main")] = overloaded
        self.assertEqual(self.ask(), {"provider": "openrouter", "key": "main"})
        self.assertEqual(self.calls, [("gemini", "main"), ("openrouter", "main")])
        self.assertEqual(self.states()["main"], "ok")

    def test_d_rejected_key_is_invalid_and_the_others_are_used(self):
        self.behaviour[("gemini", "main")] = unauthorised(FAKE["GEMINI_API_KEY"])
        self.assertEqual(self.ask(), {"provider": "gemini", "key": "pranav"})
        self.assertEqual(self.states()["main"], "invalid")
        self.clock.now += 10 * 3600
        self.calls.clear()
        self.ask()
        self.assertNotIn(("gemini", "main"), self.calls)
        self.assertIn("WARNING: gemini key 'main' was rejected", self.log.getvalue())

    def test_e_all_gemini_keys_limited_falls_through_to_openrouter(self):
        for label in ("main", "pranav", "shrav", "talha"):
            self.behaviour[("gemini", label)] = per_minute
        self.assertEqual(self.ask(), {"provider": "openrouter", "key": "main"})
        self.assertEqual([call for call in self.calls if call[0] == "gemini"],
                         [("gemini", "main"), ("gemini", "pranav"),
                          ("gemini", "shrav"), ("gemini", "talha")])
        self.calls.clear()
        self.assertEqual(self.ask(), {"provider": "openrouter", "key": "main"})
        self.assertEqual(self.calls, [("openrouter", "main")])  # gemini skipped, nothing spent
        self.assertEqual(vision.summarise_provenance(vision.provenance()[-1:]),
                         "via openrouter (gemini rate-limited)")


class WaitingTests(KeyPoolCase):
    def limit_everything_once(self, seconds):
        """Every key answers 429 (retry in `seconds`) once, then works."""
        failed = set()

        def once(provider, key):
            def raise_or_pass():
                if (provider, key) in failed:
                    return None
                failed.add((provider, key))
                return ProviderError(f"429 rate limit: please try again in {seconds}s", 429)
            return raise_or_pass

        for label in ("main", "pranav", "shrav", "talha"):
            self.behaviour[("gemini", label)] = once("gemini", label)
        self.behaviour[("openrouter", "main")] = once("openrouter", "main")

    def fake(self, provider):
        def call(parts, model, label):
            key = providers.current_key_label(provider)
            self.calls.append((provider, key))
            action = self.behaviour.get((provider, key))
            error = action() if callable(action) else action
            if isinstance(error, Exception):
                raise error
            return {"provider": provider, "key": key}
        return call

    def setUp(self):
        super().setUp()
        self.patches[2].stop()
        self.patches[2] = patch.dict(vision.PROVIDER_CALLS, {
            "gemini": self.fake("gemini"), "openrouter": self.fake("openrouter"),
        })
        self.patches[2].start()

    def test_live_pipeline_never_waits(self):
        self.limit_everything_once(5)
        with self.assertRaises(vision.VisionUnavailable):
            self.ask()
        self.assertEqual(self.slept, [])

    def test_seed_waits_for_the_soonest_key_then_succeeds(self):
        vision.wait_for_keys(True)
        self.limit_everything_once(5)
        self.assertEqual(self.ask(), {"provider": "gemini", "key": "main"})
        self.assertEqual(self.slept, [5])

    def test_seed_waits_at_most_60s_at_a_time(self):
        vision.wait_for_keys(True)
        self.limit_everything_once(150)
        self.assertEqual(self.ask()["provider"], "gemini")
        self.assertEqual(self.slept, [60, 60, 30])


class SecrecyTests(KeyPoolCase):
    def test_g_no_key_value_in_logs_or_health(self):
        self.behaviour[("gemini", "main")] = unauthorised(FAKE["GEMINI_API_KEY"])
        self.behaviour[("gemini", "pranav")] = per_minute
        self.behaviour[("gemini", "shrav")] = per_day
        self.behaviour[("gemini", "talha")] = overloaded
        with contextlib.redirect_stdout(self.log):
            self.ask()
            main.log_provider_chain()
            health = TestClient(main.app).get("/api/health").json()
        text = self.log.getvalue() + json.dumps(health)
        for value in FAKE.values():
            self.assertNotIn(value, text)
            self.assertNotIn(value[:12], text)
        labels = {key["label"]: key["state"] for item in health["vision"]["chain"]
                  if item["provider"] == "gemini" for key in item["keys"]}
        self.assertEqual(labels, {"main": "invalid", "pranav": "cooling",
                                  "shrav": "exhausted", "talha": "ok"})
        self.assertEqual(next(item["key_count"] for item in health["vision"]["chain"]
                              if item["provider"] == "gemini"), 4)

    def test_redact_removes_values_and_key_shapes(self):
        text = providers.redact(f"bad key {FAKE['GEMINI_API_KEY']} and gsk_abc123****wxyz")
        self.assertNotIn("AIzaFAKE", text)
        self.assertNotIn("gsk_abc", text)


class ParsingTests(unittest.TestCase):
    def test_groq_and_gemini_retry_hints(self):
        now = 1_800_000_000.0
        groq = ProviderError("Rate limit reached ... on input tokens per minute (ITPM): "
                             "Limit 7000. Please try again in 14.74s.", 429)
        self.assertEqual(vision.classify_provider_error(groq, now), ("minute", now + 14.74))
        groq_day = ProviderError("Rate limit reached on requests per day (RPD). "
                                 "Please try again in 1m26.4s.", 429)
        self.assertEqual(vision.classify_provider_error(groq_day, now), ("daily", now + 86.4))
        self.assertEqual(vision.classify_provider_error(per_day(), now), ("daily", now + 64425))
        self.assertEqual(vision.classify_provider_error(overloaded(), now), ("server", None))
        self.assertEqual(vision.classify_provider_error(
            ProviderError("API key not valid. Please pass a valid API key.", 400), now),
            ("invalid", None))


if __name__ == "__main__":
    unittest.main()
