"""The /mcp endpoint and its OAuth flow, driven the way Claude's connector drives them."""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import CLAUDE_CALLBACK, CLIENT_ID, CLIENT_SECRET, login

RESOURCE = "http://localhost/mcp"
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize(client, challenge, **overrides):
    params = dict(response_type="code", client_id=CLIENT_ID, redirect_uri=CLAUDE_CALLBACK, code_challenge=challenge,
                  code_challenge_method="S256", state="st-123", scope="dietician", resource=RESOURCE)
    params.update(overrides)
    return client.get("/authorize", params=params)


def consent(client, challenge, decision="approve"):
    """/authorize -> consent page -> submit. Returns (response, req_id, csrf)."""
    r = authorize(client, challenge)
    assert r.status_code == 302, r.text
    page = client.get(r.headers["location"]).text
    req = re.search(r'name="req" value="([^"]+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return client.post("/mcp-auth/consent", data={"req": req, "csrf": csrf, "decision": decision}), req, csrf


def token(client, form, basic=False):
    if basic:
        auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        return client.post("/token", data={**form, "client_id": CLIENT_ID}, headers={"Authorization": f"Basic {auth}"})
    return client.post("/token", data={**form, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})


def sign_in(client, user="alice"):
    """Full connector sign-in; returns the token response."""
    login(client, user)
    verifier, challenge = pkce()
    r, _, _ = consent(client, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = token(client, {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK,
                       "code_verifier": verifier})
    assert r.status_code == 200, r.text
    return r.json()


def mcp(client, access_token, method, params=None, id=1):
    return client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {access_token}"},
                       json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def call_tool(client, access_token, name, args=None):
    r = mcp(client, access_token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def tool_json(result):
    """Tool results arrive as JSON text content."""
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


# ── Discovery ────────────────────────────────────

def test_discovery(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]

    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE and prm["authorization_servers"] == ["http://localhost"]

    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["token_endpoint"] == "http://localhost/token"
    assert "registration_endpoint" not in asm          # no dynamic client registration


def test_website_routes_unaffected_by_mount(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/today").status_code == 401
    assert client.get("/no-such-page").status_code == 404


# ── /authorize and consent ───────────────────────

def test_authorize_rejects_bad_client_and_redirect(client):
    _, challenge = pkce()
    r = authorize(client, challenge, redirect_uri="https://evil.example/cb")
    assert r.status_code == 400 and "evil" not in r.headers.get("location", "")
    assert authorize(client, challenge, client_id="someone-else").status_code == 400


def test_signed_out_user_goes_to_google_then_back(client):
    _, challenge = pkce()
    consent_url = authorize(client, challenge).headers["location"]
    assert consent_url.startswith("http://localhost/mcp-auth/consent?req=")
    r = client.get(consent_url)
    assert r.status_code == 302 and r.headers["location"] == "/auth/login"


def test_consent_page_shows_account_and_can_be_denied(client):
    login(client, "alice")
    _, challenge = pkce()
    page = client.get(authorize(client, challenge).headers["location"]).text
    assert "alice@example.com" in page

    r, _, _ = consent(client, challenge, decision="deny")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and "code" not in q


def test_consent_returns_code_once(client):
    login(client, "alice")
    _, challenge = pkce()
    r, req, csrf = consent(client, challenge)
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CLAUDE_CALLBACK
    assert q["state"] == ["st-123"] and q["iss"] == ["http://localhost"] and len(q["code"][0]) > 30
    assert client.post("/mcp-auth/consent", data={"req": req, "csrf": csrf, "decision": "approve"}).status_code == 400


# ── /token ───────────────────────────────────────

def test_code_exchange_checks_pkce_secret_and_reuse(client):
    login(client, "alice")
    verifier, challenge = pkce()
    r, _, _ = consent(client, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier}

    assert token(client, {**form, "code_verifier": "wrong" * 10}).json()["error"] == "invalid_grant"
    assert client.post("/token", data={**form, "client_id": CLIENT_ID, "client_secret": "nope"}).status_code == 401
    r = token(client, form)
    assert r.status_code == 200
    t = r.json()
    assert t["token_type"].lower() == "bearer" and t["expires_in"] == 3600 and t["refresh_token"]
    assert token(client, form).status_code == 400          # single use


def test_refresh_rotates_and_reuse_revokes_grant(client):
    t1 = sign_in(client)
    r = token(client, {"grant_type": "refresh_token", "refresh_token": t1["refresh_token"]}, basic=True)
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    assert mcp(client, t1["access_token"], "ping").status_code == 401     # old access token ended
    assert mcp(client, t2["access_token"], "ping").status_code == 200

    assert token(client, {"grant_type": "refresh_token", "refresh_token": t1["refresh_token"]}).status_code == 400
    assert mcp(client, t2["access_token"], "ping").status_code == 401     # whole grant revoked


def test_revoke(client):
    t = sign_in(client)
    assert client.post("/revoke", data={"token": t["access_token"], "client_id": CLIENT_ID,
                                        "client_secret": CLIENT_SECRET}).status_code == 200
    assert mcp(client, t["access_token"], "ping").status_code == 401
    assert token(client, {"grant_type": "refresh_token", "refresh_token": t["refresh_token"]}).status_code == 400


def test_tokens_stored_hashed(client, fresh_db):
    import sqlite3
    t = sign_in(client)
    hashes = [r[0] for r in sqlite3.connect(fresh_db).execute("SELECT token_hash FROM mcp_tokens")]
    assert len(hashes) == 2
    assert t["access_token"] not in hashes and hashlib.sha256(t["access_token"].encode()).hexdigest() in hashes


# ── /mcp tools ───────────────────────────────────

def test_rejects_unknown_token_and_wrong_host(client):
    assert mcp(client, "garbage", "ping").status_code == 401
    t = sign_in(client)
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {t['access_token']}", "Host": "evil.example"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 421


def test_lists_twelve_tools_with_annotations(client):
    t = sign_in(client)
    tools = {x["name"]: x for x in mcp(client, t["access_token"], "tools/list").json()["result"]["tools"]}
    assert len(tools) == 12
    read_only = {n for n, x in tools.items() if x.get("annotations", {}).get("readOnlyHint")}
    assert read_only == {"get_today", "get_progress", "get_fridge", "search_food_history"}
    destructive = {n for n, x in tools.items() if x.get("annotations", {}).get("destructiveHint")}
    assert destructive == {"delete_food", "remove_fridge_items"}
    assert "user_id" not in str(tools["log_food"]["inputSchema"])


def test_tools_act_as_the_signed_in_user(client):
    t_alice = sign_in(client, "alice")
    t_bob = sign_in(client, "bob")

    res = call_tool(client, t_alice["access_token"], "log_food",
                    {"items": [{"description": "poha", "portion": "1 plate", "calories": 300, "protein": 6}]})
    day = tool_json(res)["day"]
    assert day["totals"]["calories"] == 300
    eid = day["entries"][0]["id"]

    assert tool_json(call_tool(client, t_bob["access_token"], "get_today"))["totals"]["calories"] == 0
    res = call_tool(client, t_bob["access_token"], "delete_food", {"entry_id": eid})
    assert res["isError"] and "No food entry" in res["content"][0]["text"]


@pytest.mark.parametrize("name,args,message", [
    ("log_food", {"items": [{"description": "coffee", "calories": 9000}]}, "less than or equal"),
    ("get_today", {"date": "yesterday"}, "Invalid date"),
    ("get_progress", {"days": 5}, "Input should be 7, 14 or 30"),
])
def test_bad_tool_input_is_a_readable_error(client, name, args, message):
    t = sign_in(client)
    res = call_tool(client, t["access_token"], name, args)
    assert res["isError"] and message in res["content"][0]["text"]
