"""Test isolation: redirect the app to a throwaway database and data directory.

This runs before any app module is imported (pytest imports conftest first), so
`app.config.Settings` picks up these env vars and the test suite never reads or writes the
real `data/app.sqlite` or the real `data/` files. Without this, the TestClient-based smoke
tests would persist synthetic records into the developer's database on every run.
"""

import os
import tempfile
from pathlib import Path

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="omr-test-"))
_TEST_DB = _TEST_DATA_DIR / "test_app.sqlite"

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["DATA_ROOT"] = str(_TEST_DATA_DIR)
