"""
Personal Dietician — Server
Chat-first interface with food logging and on-demand recipes.
"""

from __future__ import annotations
from typing import Optional, List

import asyncio
import logging
import base64
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from config import HOST, PORT, ANTHROPIC_API_KEY, WORKOUTS, SOCIAL_DAYS, SESSION_SECRET
from auth import login as auth_login, callback as auth_callback, me as auth_me, logout as auth_logout, require_auth
from db import get_user, update_user_targets, update_user_timezone
from db import (
    init_db, get_fridge_items, add_fridge_item, add_fridge_items_bulk,
    remove_fridge_item, remove_fridge_item_by_id, clear_fridge,
    update_fridge_item,
    add_food_log_entry, get_food_log, delete_food_log_entry,
    update_food_log_entry, get_food_log_totals, get_food_log_history,
    check_food_log_for_keyword,
    ack_reminder, save_grocery_list, get_grocery_list,
    save_quick_log, get_frequent_quick_logs, search_quick_logs,
    log_purchases_bulk, get_purchase_frequency,
    save_chat_message, get_chat_history,
)
from meal_engine import process_chat_message, analyze_food_photo, analyze_fridge_photo, analyze_receipt_photo, generate_grocery_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("dietician.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized")
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set")
    yield

app = FastAPI(title="Personal Dietician", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Timezone Helper ──────────────────────────────

def get_client_date(request: Request, fallback: Optional[str] = None) -> str:
    if fallback:
        return fallback
    tz_name = request.headers.get("X-Timezone")
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
            return str(datetime.now(tz).date())
        except Exception:
            pass
    return str(date.today())


# ── Auth Routes ──────────────────────────────────

app.get("/auth/login")(auth_login)
app.get("/auth/callback")(auth_callback)
app.get("/auth/me")(auth_me)
app.get("/auth/logout")(auth_logout)


# ── Models ───────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    date: Optional[str] = None

class FridgeItemRequest(BaseModel):
    name: str
    category: str = "other"
    quantity: str = ""

class UpdateFoodLogRequest(BaseModel):
    calories: Optional[int] = None
    protein: Optional[int] = None
    fiber: Optional[int] = None
    description: Optional[str] = None
    re_estimate: bool = False

class BulkFridgeItem(BaseModel):
    name: str
    quantity: str = ""
    category: str = "other"

class BulkFridgeRequest(BaseModel):
    items: List[BulkFridgeItem]
    source: str = "manual"
    store: str = ""

class DirectFoodLogRequest(BaseModel):
    date: str
    description: str
    calories: int
    protein: int
    fiber: int = 0
    source: str = "photo"

class UpdateFridgeItemRequest(BaseModel):
    name: Optional[str] = None
    quantity: Optional[str] = None
    category: Optional[str] = None

class UpdateProfileRequest(BaseModel):
    calories_max: int
    protein_target: int
    fiber_target: int


# ── Helper: get user targets ────────────────────

def get_user_targets(user_id: str):
    user = get_user(user_id)
    if user:
        return {
            "calories_min": user["calories_min"],
            "calories_max": user["calories_max"],
            "protein_target": user["protein_target"],
            "fiber_target": user["fiber_target"],
        }
    return {
        "calories_min": 1800,
        "calories_max": 2000,
        "protein_target": 150,
        "fiber_target": 30,
    }


# ── Profile ──────────────────────────────────────

@app.put("/api/profile")
async def update_profile(req: UpdateProfileRequest, request: Request):
    user_id = require_auth(request)
    update_user_targets(user_id, req.calories_max, req.protein_target, req.fiber_target)
    return {"status": "updated"}


# ── Chat Endpoint (main interface) ───────────────

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    user_id = require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    log_date = req.date or get_client_date(request)

    try:
        result = await process_chat_message(user_id, req.message, plan_date=date.fromisoformat(log_date))

        save_chat_message(user_id, "user", req.message, log_date)

        if result.get("type") == "log":
            cal = result.get("calories", 0)
            prot = result.get("protein", 0)
            fib = result.get("fiber", 0)
            desc = result.get("description", req.message)
            add_food_log_entry(log_date, user_id, desc, cal, prot, fib, source="chat")
            save_quick_log(desc, cal, prot, fib)
            save_chat_message(user_id, "assistant", f"Logged: {desc} ({cal} cal, {prot}g protein, {fib}g fiber)", log_date)
            return {
                "type": "log",
                "description": desc,
                "calories": cal,
                "protein": prot,
                "fiber": fib,
                "message": f"Logged: {desc} ({cal} cal, {prot}g protein, {fib}g fiber)",
            }
        elif result.get("type") == "suggest":
            msg = result.get("message", "")
            save_chat_message(user_id, "assistant", msg, log_date)
            return {"type": "suggest", "message": msg}
        else:
            msg = result.get("message", "")
            save_chat_message(user_id, "assistant", msg, log_date)
            return {"type": "chat", "message": msg}

    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Health ───────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


# ── Today Summary ────────────────────────────────

@app.get("/api/today")
async def get_today(request: Request, client_date: Optional[str] = None):
    user_id = require_auth(request)
    today = client_date or get_client_date(request)
    dow = date.fromisoformat(today).weekday()
    workout = WORKOUTS.get(dow)
    is_social = dow in SOCIAL_DAYS
    totals = get_food_log_totals(today, user_id)
    entries = get_food_log(today, user_id)
    targets = get_user_targets(user_id)

    # Update stored timezone if sent
    tz_name = request.headers.get("X-Timezone")
    if tz_name:
        user = get_user(user_id)
        if user and user.get("timezone") != tz_name:
            update_user_timezone(user_id, tz_name)

    return {
        "date": today,
        "day_name": date.fromisoformat(today).strftime("%A"),
        "workout": workout,
        "is_social": is_social,
        "is_rest_day": workout is None,
        "totals": totals,
        "entries": entries,
        "targets": {
            "calories": targets["calories_max"],
            "protein": targets["protein_target"],
            "fiber": targets["fiber_target"],
        },
    }


# ── Food Log ─────────────────────────────────────

@app.get("/api/food-log")
async def get_user_food_log(request: Request, log_date: Optional[str] = None):
    user_id = require_auth(request)
    d = log_date or get_client_date(request)
    entries = get_food_log(d, user_id)
    totals = get_food_log_totals(d, user_id)
    targets = get_user_targets(user_id)
    return {
        "date": d,
        "user_id": user_id,
        "entries": entries,
        "totals": totals,
        "targets": {
            "calories": targets["calories_max"],
            "protein": targets["protein_target"],
            "fiber": targets["fiber_target"],
        },
    }

@app.post("/api/food-log")
async def direct_food_log(req: DirectFoodLogRequest, request: Request):
    user_id = require_auth(request)
    add_food_log_entry(req.date, user_id, req.description, req.calories, req.protein, req.fiber, source=req.source)
    save_quick_log(req.description, req.calories, req.protein, req.fiber)
    return {"status": "logged"}

@app.delete("/api/food-log/{entry_id}")
async def remove_food_log(entry_id: int, request: Request):
    require_auth(request)
    delete_food_log_entry(entry_id)
    return {"status": "deleted"}

@app.put("/api/food-log/{entry_id}")
async def edit_food_log(entry_id: int, req: UpdateFoodLogRequest, request: Request):
    user_id = require_auth(request)
    if req.re_estimate and req.description:
        if not ANTHROPIC_API_KEY:
            raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
        try:
            result = await process_chat_message(user_id, f"had {req.description}")
            cal = result.get("calories", 0)
            prot = result.get("protein", 0)
            fib = result.get("fiber", 0)
            update_food_log_entry(entry_id, cal, prot, fib, description=req.description)
            return {"status": "re_estimated", "calories": cal, "protein": prot, "fiber": fib}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        cal = req.calories or 0
        prot = req.protein or 0
        fib = req.fiber or 0
        update_food_log_entry(entry_id, cal, prot, fib, description=req.description)
        return {"status": "updated"}


# ── Analysis ─────────────────────────────────────

@app.get("/api/analysis")
async def get_analysis(request: Request, days: int = 7):
    user_id = require_auth(request)
    ref_date = get_client_date(request)
    history = get_food_log_history(user_id, days, reference_date=ref_date)
    targets = get_user_targets(user_id)
    return {
        "user_id": user_id,
        "days": days,
        "history": history,
        "targets": {
            "calories": targets["calories_max"],
            "protein": targets["protein_target"],
            "fiber": targets["fiber_target"],
        },
    }


# ── Photo Endpoints ──────────────────────────────

@app.post("/api/photo/food")
async def photo_analyze_meal(request: Request, file: UploadFile = File(...)):
    user_id = require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    contents = await file.read()
    b64 = base64.b64encode(contents).decode("utf-8")
    media_type = file.content_type or "image/jpeg"
    try:
        result = await analyze_food_photo(b64, media_type)
        return {"status": "analyzed", "analysis": result}
    except Exception as e:
        logger.error(f"Food photo analysis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/photo/fridge")
async def analyze_fridge(request: Request, file: UploadFile = File(...)):
    require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    contents = await file.read()
    b64 = base64.b64encode(contents).decode("utf-8")
    media_type = file.content_type or "image/jpeg"
    try:
        items = await analyze_fridge_photo(b64, media_type)
        return {"status": "analyzed", "items": items}
    except Exception as e:
        logger.error(f"Fridge photo analysis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/photo/receipt")
async def scan_receipt(request: Request, file: UploadFile = File(...)):
    require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    contents = await file.read()
    b64 = base64.b64encode(contents).decode("utf-8")
    media_type = file.content_type or "image/jpeg"
    try:
        result = await analyze_receipt_photo(b64, media_type)
        return {"status": "analyzed", "store": result.get("store", ""), "items": result.get("items", []), "total": result.get("total", "")}
    except Exception as e:
        logger.error(f"Receipt scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Fridge ───────────────────────────────────────

@app.get("/api/fridge")
async def get_fridge(request: Request):
    require_auth(request)
    return {"items": get_fridge_items()}

@app.post("/api/fridge")
async def add_to_fridge(req: FridgeItemRequest, request: Request):
    require_auth(request)
    add_fridge_item(req.name, req.category, req.quantity)
    return {"status": "added"}

@app.post("/api/fridge/bulk")
async def bulk_add_fridge(req: BulkFridgeRequest, request: Request):
    require_auth(request)
    items_dicts = [{"name": i.name, "quantity": i.quantity, "category": i.category} for i in req.items]
    add_fridge_items_bulk(items_dicts)
    if req.source == "receipt" and req.store:
        purchase_items = [{"name": i.name, "quantity": i.quantity} for i in req.items]
        log_purchases_bulk(purchase_items, req.store)
    return {"status": "added", "count": len(req.items)}

@app.put("/api/fridge/{item_id}")
async def update_fridge(item_id: int, req: UpdateFridgeItemRequest, request: Request):
    require_auth(request)
    update_fridge_item(item_id, req.name, req.quantity, req.category)
    return {"status": "updated"}

@app.delete("/api/fridge/{item_name}")
async def remove_from_fridge(item_name: str, request: Request):
    require_auth(request)
    remove_fridge_item(item_name)
    return {"status": "removed"}

@app.delete("/api/fridge/id/{item_id}")
async def remove_fridge_by_id(item_id: int, request: Request):
    require_auth(request)
    remove_fridge_item_by_id(item_id)
    return {"status": "removed"}

@app.delete("/api/fridge")
async def clear_all_fridge(request: Request):
    require_auth(request)
    clear_fridge()
    return {"status": "cleared"}


# ── Grocery ──────────────────────────────────────

@app.post("/api/grocery/generate")
async def generate_grocery_endpoint(request: Request):
    user_id = require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    try:
        grocery = await generate_grocery_list(user_id)
        save_grocery_list(get_client_date(request), grocery)
        return {"status": "generated", "grocery": grocery}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/grocery")
async def get_grocery(request: Request, week_start: Optional[str] = None):
    require_auth(request)
    ws = week_start or get_client_date(request)
    grocery = get_grocery_list(ws)
    return {"status": "ok" if grocery else "not_found", "grocery": grocery}


# ── Quick Log Suggestions ───────────────────────

@app.get("/api/suggestions")
async def quick_log_suggestions(request: Request, q: Optional[str] = None):
    require_auth(request)
    if q and len(q) >= 2:
        return {"suggestions": search_quick_logs(q)}
    return {"suggestions": get_frequent_quick_logs(10)}


# ── Serve Frontend ───────────────────────────────

import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    @app.get("/")
    async def serve_frontend():
        return FileResponse(os.path.join(static_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting Personal Dietician on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
