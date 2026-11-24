"""OAuth authorization server for the MCP connector (phase 4 of docs/mcp-agent-plan.md).

The MCP SDK hosts the protocol endpoints and does the protocol checks
(/.well-known/oauth-authorization-server, /authorize, /token, /revoke; PKCE S256,
exact redirect-URI match, client secret check, code/refresh expiry). This module is
the storage and the human step: /authorize sends the browser to /mcp-auth/consent,
which signs the user in with the website's existing Google login, asks them to
approve, then redirects back to Claude with a one-time code.

Client: one pre-registered client from the environment, entered in Claude's
connector settings under "Use your own OAuth client":

    MCP_CLIENT_ID, MCP_CLIENT_SECRET
    MCP_REDIRECT_URIS   (optional, comma-separated; default Claude's callback)

Tokens: random 256-bit strings; only SHA-256 hashes are stored. Access tokens last
1 hour, refresh tokens 60 days and rotate on every use. Presenting an already-rotated
refresh token revokes the whole grant (it may have been stolen).
"""

import contextvars
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
import uuid

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import db
from config import BASE_URL

logger = logging.getLogger("dietician.mcp_auth")

ISSUER = BASE_URL.rstrip("/")
RESOURCE_URL = f"{ISSUER}/mcp"
SCOPE = "dietician"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"

AUTH_REQUEST_TTL = 10 * 60
AUTH_CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 60 * 24 * 60 * 60

CONSENT_PATH = "/mcp-auth/consent"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


# Which client-auth method the current /token or /revoke request used. The SDK only
# accepts the single method stored on the client, and Claude's docs don't say whether
# it sends Basic auth or form fields, so token_auth_method_shim sets this per request.
_request_auth_method: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mcp_request_auth_method", default="client_secret_post")


def token_auth_method_shim(app):
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            method = "client_secret_basic" if auth[:6].lower() == b"basic " else "client_secret_post"
            token = _request_auth_method.set(method)
            try:
                return await app(scope, receive, send)
            finally:
                _request_auth_method.reset(token)
        return await app(scope, receive, send)
    return wrapped


class DieticianOAuthProvider:
    """OAuthAuthorizationServerProvider backed by SQLite."""

    # ── Clients ──────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        cid = os.environ.get("MCP_CLIENT_ID", "")
        secret = os.environ.get("MCP_CLIENT_SECRET", "")
        if not cid or len(secret) < 32 or not hmac.compare_digest(client_id, cid):
            return None
        redirects = [u.strip() for u in os.environ.get("MCP_REDIRECT_URIS", CLAUDE_CALLBACK).split(",") if u.strip()]
        return OAuthClientInformationFull(
            client_id=cid,
            client_secret=secret,
            client_name="Claude",
            redirect_uris=redirects,
            token_endpoint_auth_method=_request_auth_method.get(),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPE,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("Dynamic client registration is disabled.")

    # ── Authorization ────────────────────────────

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != RESOURCE_URL:
            raise AuthorizeError(error="invalid_request", error_description=f"Unknown resource {params.resource}")
        req_id = _new_secret()
        conn = db.get_conn()
        conn.execute("DELETE FROM mcp_auth_requests WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT INTO mcp_auth_requests (id, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
            (req_id, client.client_id, params.model_dump_json(), time.time() + AUTH_REQUEST_TTL),
        )
        conn.commit()
        conn.close()
        return f"{ISSUER}{CONSENT_PATH}?req={req_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM mcp_auth_codes WHERE code_hash = ? AND client_id = ?",
                           (_hash(authorization_code), client.client_id)).fetchone()
        conn.close()
        if not row:
            return None
        return AuthorizationCode(code=authorization_code, **json.loads(row["data"]))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        conn = db.get_conn()
        # Single use: delete first; if nothing was deleted, it was already exchanged.
        cur = conn.execute("DELETE FROM mcp_auth_codes WHERE code_hash = ?", (_hash(authorization_code.code),))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes,
                           authorization_code.resource, grant_id=uuid.uuid4().hex)

    # ── Tokens ───────────────────────────────────

    def _issue(self, client_id, user_id, scopes, resource, grant_id) -> OAuthToken:
        access, refresh = _new_secret(), _new_secret()
        now = int(time.time())
        conn = db.get_conn()
        conn.execute("DELETE FROM mcp_tokens WHERE expires_at < ?", (now,))
        for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
            conn.execute(
                "INSERT INTO mcp_tokens (token_hash, kind, grant_id, client_id, user_id, scopes, resource, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash(token), kind, grant_id, client_id, user_id, " ".join(scopes), resource, now + ttl),
            )
        conn.commit()
        conn.close()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    def _row(self, token, kind):
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM mcp_tokens WHERE token_hash = ? AND kind = ?", (_hash(token), kind)).fetchone()
        conn.close()
        return row

    def _revoke_grant(self, grant_id):
        conn = db.get_conn()
        conn.execute("DELETE FROM mcp_tokens WHERE grant_id = ?", (grant_id,))
        conn.commit()
        conn.close()

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self._row(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        if row["used"]:
            logger.warning("Rotated refresh token reused; revoking grant %s", row["grant_id"])
            self._revoke_grant(row["grant_id"])
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
                            expires_at=row["expires_at"], resource=row["resource"], subject=row["user_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        row = self._row(refresh_token.token, "refresh")
        conn = db.get_conn()
        cur = conn.execute("UPDATE mcp_tokens SET used = 1 WHERE token_hash = ? AND used = 0", (_hash(refresh_token.token),))
        # Old access tokens from this grant end now; the new pair carries on the grant.
        conn.execute("DELETE FROM mcp_tokens WHERE grant_id = ? AND kind = 'access'", (row["grant_id"],))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            raise TokenError(error="invalid_grant", error_description="refresh token already used")
        return self._issue(client.client_id, refresh_token.subject, scopes, refresh_token.resource, row["grant_id"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._row(token, "access")
        if row and row["expires_at"] > time.time():
            return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                               expires_at=row["expires_at"], resource=row["resource"], subject=row["user_id"])
        return _dev_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            row = self._row(token.token, kind)
            if row:
                self._revoke_grant(row["grant_id"])


def _dev_token(token: str) -> AccessToken | None:
    """Local testing only: MCP_DEV_TOKEN (>= 32 chars) acts as MCP_DEV_USER. Unset in production."""
    dev_token, dev_user = os.environ.get("MCP_DEV_TOKEN", ""), os.environ.get("MCP_DEV_USER", "")
    if len(dev_token) >= 32 and dev_user and hmac.compare_digest(token, dev_token):
        return AccessToken(token=token, client_id="dev", scopes=[SCOPE], resource=RESOURCE_URL, subject=dev_user)
    return None


provider = DieticianOAuthProvider()


# ── Consent page (FastAPI routes, registered in server.py) ──────────

def _load_request(req_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM mcp_auth_requests WHERE id = ? AND expires_at > ?", (req_id or "", time.time())).fetchone()
    conn.close()
    return row


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:-apple-system,system-ui,sans-serif;background:#0b1020;color:#e5e7eb;padding:16px;box-sizing:border-box}}
.card{{max-width:380px;width:100%;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);
border-radius:20px;padding:28px;text-align:center}}
h1{{font-size:20px;margin:0 0 12px}} p{{color:rgba(229,231,235,0.75);line-height:1.5;font-size:15px}}
ul{{text-align:left;color:rgba(229,231,235,0.75);font-size:14px;line-height:1.6;padding-left:20px}}
button{{width:100%;padding:14px;border:0;border-radius:12px;font-size:16px;font-weight:600;margin-top:10px;cursor:pointer}}
.approve{{background:#3b82f6;color:#fff}} .deny{{background:rgba(255,255,255,0.08);color:#e5e7eb}}
.who{{color:#fff;font-weight:600}}
</style></head><body><div class="card">{body}</div></body></html>""", status_code=status)


async def consent_get(request: Request):
    req_id = request.query_params.get("req", "")
    if not _load_request(req_id):
        return _page("Link expired", "<h1>This link has expired</h1><p>Go back to Claude and connect again.</p>", 400)
    user_id = request.session.get("user_id")
    user = db.get_user(user_id) if user_id else None
    if not user:
        request.session["next"] = f"{CONSENT_PATH}?req={req_id}"
        return RedirectResponse("/auth/login", status_code=302)
    csrf = _new_secret()
    request.session["mcp_csrf"] = csrf
    who = html.escape(user["email"])
    return _page("Connect Claude", f"""<h1>Connect Claude to Dietician?</h1>
<p>Signed in as <span class="who">{who}</span></p>
<p>Claude will be able to:</p>
<ul><li>Read your food log, targets, progress, weight and fridge</li>
<li>Log, correct and delete food entries</li><li>Log workouts and weight, mark rest or vacation days</li>
<li>Add and remove fridge items</li></ul>
<p>It can't change your targets or fridge sharing.</p>
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="req" value="{html.escape(req_id)}"><input type="hidden" name="csrf" value="{csrf}">
<button class="approve" name="decision" value="approve">Allow</button>
<button class="deny" name="decision" value="deny">Cancel</button></form>""")


async def consent_post(request: Request):
    form = await request.form()
    req_id, csrf, decision = form.get("req", ""), form.get("csrf", ""), form.get("decision", "")
    user_id = request.session.get("user_id")
    expected = request.session.pop("mcp_csrf", None)
    if not user_id or not expected or not hmac.compare_digest(str(csrf), expected):
        return _page("Try again", "<h1>Something went wrong</h1><p>Go back to Claude and connect again.</p>", 400)

    conn = db.get_conn()
    row = conn.execute("SELECT * FROM mcp_auth_requests WHERE id = ? AND expires_at > ?", (str(req_id), time.time())).fetchone()
    conn.execute("DELETE FROM mcp_auth_requests WHERE id = ?", (str(req_id),))  # single use
    conn.commit()
    conn.close()
    if not row:
        return _page("Link expired", "<h1>This link has expired</h1><p>Go back to Claude and connect again.</p>", 400)

    params = AuthorizationParams.model_validate_json(row["params"])
    redirect = str(params.redirect_uri)
    if decision != "approve":
        return RedirectResponse(construct_redirect_uri(redirect, error="access_denied", state=params.state, iss=ISSUER), status_code=302)

    code = _new_secret()
    data = {
        "scopes": params.scopes or [SCOPE],
        "expires_at": time.time() + AUTH_CODE_TTL,
        "client_id": row["client_id"],
        "code_challenge": params.code_challenge,
        "redirect_uri": redirect,
        "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
        # We serve a single resource, so bind to it even if the client omitted `resource`.
        "resource": RESOURCE_URL,
        "subject": user_id,
    }
    conn = db.get_conn()
    conn.execute("DELETE FROM mcp_auth_codes WHERE expires_at < ?", (time.time(),))
    conn.execute("INSERT INTO mcp_auth_codes (code_hash, client_id, user_id, data, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (_hash(code), row["client_id"], user_id, json.dumps(data), data["expires_at"]))
    conn.commit()
    conn.close()
    logger.info("MCP connector approved for user %s", user_id)
    return RedirectResponse(construct_redirect_uri(redirect, code=code, state=params.state, iss=ISSUER), status_code=302)
