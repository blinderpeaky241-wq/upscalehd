import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Must be set before config is imported.
_TMP = tempfile.mkdtemp(prefix="upscaler-test-")
os.environ["DATA_DIR"] = _TMP + "/data"
os.environ["CHUNK_MB"] = "1"
os.environ["MIN_FREE_GB"] = "0"
os.environ["MAX_JOBS_PER_OWNER"] = "6"
os.environ["MAX_CONCURRENT_JOBS"] = "2"
os.environ["DELETE_SOURCE_AFTER_SUCCESS"] = "false"

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(scope="session")
def samples():
    d = Path(_TMP) / "samples"
    d.mkdir()
    return d


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
