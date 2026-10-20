"""Choose the test database before db.py is imported.

Import this module before any application module. The backend is fixed at
db import, so this must run first.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values


def _abort(message):
    print(message, file=sys.stderr)
    os._exit(1)


def _hostname(url):
    return (urlparse(str(url or "").strip()).hostname or "").lower()


PROVIDER_PREFIXES = ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
                     "SIGHTENGINE_API_USER", "SIGHTENGINE_API_SECRET")


def configure():
    env_path = Path(__file__).resolve().parents[1] / ".env"
    file_env = dotenv_values(env_path) if env_path.exists() else {}
    # No test may reach a real provider. Every provider key, pooled ones
    # included, is set empty before the app loads .env (which never
    # overrides a variable that is already set), so an unstubbed call fails
    # here with "not set". Tests that need keys get fakes (provider_keys).
    for name in set(file_env) | set(os.environ):
        if name.upper().startswith(PROVIDER_PREFIXES):
            os.environ[name] = ""
    # Real people's registry rows (data/private_registry_rows.json, gitignored)
    # never reach a test: the public 100 rows only. A test that needs private
    # rows sets synthetic ones.
    os.environ["REGISTRY_PRIVATE_ROWS"] = "[]"
    production_hosts = set()
    file_database_url = (file_env.get("DATABASE_URL") or "").strip()
    if file_database_url:
        production_hosts.add(_hostname(file_database_url))
    # Read the process value before this function replaces it.
    if "DATABASE_URL" in os.environ:
        process_database_url = os.environ["DATABASE_URL"].strip()
        if process_database_url:
            production_hosts.add(_hostname(process_database_url))
    production_hosts.discard("")

    if "TEST_DATABASE_URL" in os.environ:
        test_url = os.environ["TEST_DATABASE_URL"].strip()
    else:
        test_url = (file_env.get("TEST_DATABASE_URL") or "").strip()

    use_test = os.getenv("CLAIMPILOT_USE_TEST_DB", "").strip() == "1"
    if use_test:
        if not test_url:
            _abort(
                "CLAIMPILOT_USE_TEST_DB=1 but TEST_DATABASE_URL is empty. "
                "Refusing to run tests."
            )
        test_host = _hostname(test_url)
        if not test_host:
            _abort(
                "CLAIMPILOT_USE_TEST_DB=1 but TEST_DATABASE_URL has no host. "
                "Refusing to run tests."
            )
        if test_host in production_hosts:
            _abort(
                "CLAIMPILOT_USE_TEST_DB=1 but TEST_DATABASE_URL host matches "
                "a production DATABASE_URL. Refusing to run tests."
            )
        os.environ["DATABASE_URL"] = test_url
        return
    # An empty value blocks load_dotenv() from restoring the production URL.
    os.environ["DATABASE_URL"] = ""


configure()
