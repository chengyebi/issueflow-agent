import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")

from app.core.config import clear_settings_cache  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def client():
    return TestClient(app)

