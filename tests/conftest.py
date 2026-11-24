"""Shared test setup.

Everything runs in-process: each test gets a fresh SQLite file, and HTTP tests drive
the real FastAPI app (website routes, /mcp, OAuth) through Starlette's TestClient.
No network, Google sign-in or Anthropic API key needed.

Run from the repo root with Python 3.10+ (the mcp package needs it):
    pip install -r requirements.txt -r requirements-dev.txt
    pytest
"""

import base64
import json
import os
import sys

# Must be set before config.py is imported (it reads env at import time).
os.environ["BASE_URL"] = "http://localhost"   # the MCP SDK allows a plain-http issuer only on localhost
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["MCP_CLIENT_ID"] = "claude-test"
os.environ["MCP_CLIENT_SECRET"] = "client-secret-0123456789abcdef0123456789"
os.environ.pop("MCP_DEV_TOKEN", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import itsdangerous  # noqa: E402
import pytest  # noqa: E402

import db  # noqa: E402

CLIENT_ID = os.environ["MCP_CLIENT_ID"]
CLIENT_SECRET = os.environ["MCP_CLIENT_SECRET"]
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    for uid in ("alice", "bob"):
        db.create_or_update_user(uid, f"{uid}@example.com", uid.title())
    db.update_user_targets("alice", 1800, 140, 30, weight_target=150, tdee=2300)
    return path


@pytest.fixture(scope="session")
def _app_client():
    # One app start per run: the MCP session manager can only be started once per process.
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app, base_url="http://localhost", follow_redirects=False) as c:
        yield c


@pytest.fixture
def client(_app_client, fresh_db):
    _app_client.cookies.clear()
    yield _app_client
    _app_client.cookies.clear()


def session_cookie(user_id):
    """A signed session cookie, as Starlette's SessionMiddleware writes after Google sign-in."""
    payload = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    return itsdangerous.TimestampSigner(os.environ["SESSION_SECRET"]).sign(payload).decode()


def login(client, user_id):
    # Clear first: the server re-issues the session cookie under its own domain, and a stale
    # copy would otherwise win over the one set here.
    client.cookies.clear()
    client.cookies.set("session", session_cookie(user_id))
