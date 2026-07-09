"""
Personal Dietician — Google OAuth Authentication
"""

from __future__ import annotations

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from authlib.integrations.starlette_client import OAuth

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL
from db import create_or_update_user, get_user, update_user_targets

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def login(request: Request):
    redirect_uri = f"{BASE_URL}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


async def callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse("/")

    create_or_update_user(
        google_id=user_info["sub"],
        email=user_info["email"],
        name=user_info.get("name", ""),
        picture=user_info.get("picture", ""),
    )

    request.session["user_id"] = user_info["sub"]
    return RedirectResponse("/")


async def me(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"authenticated": False}, status_code=401)
    user = get_user(user_id)
    if not user:
        request.session.clear()
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse({
        "authenticated": True,
        "user": {
            "id": user["google_id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
            "calories_max": user["calories_max"],
            "protein_target": user["protein_target"],
            "fiber_target": user["fiber_target"],
            "weight_target": user.get("weight_target", 0),
        },
    })


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


def require_auth(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id
