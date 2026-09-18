"""Pytest fixtures for Ibaraholka backend tests.

Uses isolated SQLite DB per test, fake BOT_TOKEN, no real Telegram API,
no real DB/Redis. Re-runs lifespan via TestClient context manager.
"""
import os
import sys
import tempfile
import pytest
from fastapi.testclient import TestClient

# Force test-mode env BEFORE importing app
os.environ["BOT_TOKEN"] = "0:fake"
os.environ["DEMO_MODE"] = "1"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["WIPE_SECRET"] = "test-wipe-secret"

# Each test gets its own temp DB
@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Reload main module with new env
    if "main" in sys.modules:
        del sys.modules["main"]
    if "db_adapter" in sys.modules:
        del sys.modules["db_adapter"]
    sys.path.insert(0, "/workspace")
    import db_adapter
    import main
    main.init_db()
    yield {"path": str(db_path), "main": main}
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def client(tmp_db):
    main = tmp_db["main"]
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def admin_token(tmp_db):
    return "test-admin-token"


@pytest.fixture
def wipe_secret(tmp_db):
    return "test-wipe-secret"


def make_init_data(user_id: int = 100, first_name: str = "Test", username: str = "tester") -> str:
    """Generate valid Telegram initData for testing (DEMO_MODE bypasses hash check)."""
    from urllib.parse import urlencode
    # DEMO_MODE=1 means backend doesn't validate hash, so dummy hash is fine
    return urlencode({
        "user": '{"id":' + str(user_id) + ',"first_name":"' + first_name + '","username":"' + username + '"}',
        "auth_date": "1700000000",
        "hash": "deadbeef" * 8,
    })


@pytest.fixture
def user_token():
    return make_init_data()


@pytest.fixture
def user2_token():
    return make_init_data(user_id=200, first_name="User2", username="user2")


@pytest.fixture
def auth_headers(user_token):
    return {"Authorization": f"tma {user_token}"}


@pytest.fixture
def auth_headers2(user2_token):
    return {"Authorization": f"tma {user2_token}"}
