import subprocess
import sys
import unittest
from pathlib import Path

try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401


class BackendSelectionTests(unittest.TestCase):
    def test_phash_import_does_not_change_backend(self):
        script = """
import os
os.environ["DATABASE_URL"] = (
    "postgresql://user:secret@ep-example-pooler.region.aws.neon.tech/neondb"
)
import db
import unittest
import scipy.fftpack
import imagehash
from PIL import Image
imagehash.phash(Image.new("RGB", (32, 32), "red"))
if db.BACKEND != "postgres":
    raise SystemExit("backend switched to " + db.BACKEND)
if "unittest" not in __import__("sys").modules:
    raise SystemExit("unittest was not imported")
print(db.BACKEND)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertIn("[db] backend=postgres host=ep-...-pooler.region.aws.neon.tech", result.stdout)
        self.assertIn("postgres", result.stdout)
        self.assertNotIn("secret", result.stdout)
        self.assertNotIn("postgresql://", result.stdout)

    def test_db_module_does_not_inspect_imported_modules(self):
        source = Path(__file__).resolve().parents[1].joinpath("db.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.modules", source)
