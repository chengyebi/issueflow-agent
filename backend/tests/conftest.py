import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

TEST_REVIEW_ADMIN_TOKEN = "test-review-admin-token"
TEST_REVIEW_ADMIN_HEADERS = {
    "X-Review-Admin-Token": TEST_REVIEW_ADMIN_TOKEN,
}

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("REVIEW_ADMIN_TOKEN", TEST_REVIEW_ADMIN_TOKEN)

from app.core.config import clear_settings_cache  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def review_admin_headers():
    return TEST_REVIEW_ADMIN_HEADERS.copy()


@pytest.fixture
def client(review_admin_headers):
    test_client = TestClient(app)
    test_client.headers.update(review_admin_headers)
    return test_client


@pytest.fixture
def unauthenticated_client():
    return TestClient(app)
