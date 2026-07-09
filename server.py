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

from config import HOST, PORT, ANTHROPIC_API_KEY, WORKOUTS, SOCIAL_DAYS, SESSION_SECRET, SYNC_TOKEN
from auth import login as auth_login, callback as auth_callback, me as auth_me, logout as auth_logout, require_auth
from db import get_user, update_user_targets, update_user_timezone
from db import (get_workout, get_latest_workout, set_workout, delete_workout,
    get_workouts_range, get_drinks_range, set_vacation, unset_vacation,
    is_vacation, get_vacation_range)
from db import log_weight, get_weight, get_weight_range, get_latest_weight, delete_weight
from db import (
    init_db, get_fridge_items, add_fridge_item, add_fridge_items_bulk,
    remove_fridge_item, remove_fridge_item_by_id, clear_fridge,
    update_fridge_item,
    get_fridge_id, set_fridge_group, get_fridge_group_members, migrate_fridge_items,
    add_food_log_entry, get_food_log, delete_food_log_entry,
    update_food_log_entry, get_food_log_totals, get_food_log_history,
    check_food_log_for_keyword,
    ack_reminder,
    save_quick_log, get_frequent_quick_logs, search_quick_logs,
    log_purchases_bulk, get_purchase_frequency,
    save_chat_message, get_chat_history,
)
from meal_engine import process_chat_message, analyze_food_photo, analyze_fridge_photo, analyze_receipt_photo, generate_meal_ideas

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
    quantity: Optional[int] = None
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
    weight_target: Optional[float] = None
    current_weight: Optional[float] = None

class WorkoutRequest(BaseModel):
    time: str = ""
    duration_min: int = 45
    label: str = ""
    calories_burned: int = 0
    source: str = "manual"

class AppleHealthSyncRequest(BaseModel):
    date: str = ""
    workouts: list = []
    active_calories: float = 0
    total_duration_min: float = 0

class WeightRequest(BaseModel):
    date: Optional[str] = None
    weight: float

class MealIdeasRequest(BaseModel):
    meal_type: str = "dinner"
    servings: int = 2
    cuisine: str = ""


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
    update_user_targets(user_id, req.calories_max, req.protein_target, req.fiber_target, req.weight_target)
    if req.current_weight:
        today = get_client_date(request)
        log_weight(user_id, today, req.current_weight)
    return {"status": "updated"}


# ── Weight Tracking ─────────────────────────────

@app.post("/api/weight")
async def post_weight(req: WeightRequest, request: Request):
    user_id = require_auth(request)
    log_date = req.date or get_client_date(request)
    log_weight(user_id, log_date, req.weight)
    return {"status": "logged", "date": log_date, "weight": req.weight}

@app.get("/api/weight")
async def get_weight_history(request: Request, days: int = 30):
    user_id = require_auth(request)
    ref = get_client_date(request)
    from datetime import timedelta as td
    start = str(date.fromisoformat(ref) - td(days=days))
    entries = get_weight_range(user_id, start, ref)
    latest = get_latest_weight(user_id)
    return {"entries": entries, "latest": latest}

@app.delete("/api/weight/{log_date}")
async def remove_weight(log_date: str, request: Request):
    user_id = require_auth(request)
    delete_weight(user_id, log_date)
    return {"status": "deleted"}


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
    is_social = dow in SOCIAL_DAYS
    totals = get_food_log_totals(today, user_id)
    entries = get_food_log(today, user_id)
    targets = get_user_targets(user_id)

    # Resolve workout: DB entry > yesterday's DB entry > config default
    workout = get_workout(user_id, today)
    if workout:
        workout = {"time": workout["time"], "duration_min": workout["duration_min"],
                   "label": workout["label"], "calories_burned": workout["calories_burned"],
                   "source": workout["source"]}
    else:
        prev = get_latest_workout(user_id, today)
        if prev:
            workout = {"time": prev["time"], "duration_min": prev["duration_min"],
                       "label": prev["label"], "calories_burned": 0,
                       "source": prev["source"], "inherited": True}
        else:
            config_w = WORKOUTS.get(dow)
            if config_w:
                workout = {**config_w, "calories_burned": 0, "source": "default", "inherited": True}

    workout_cal = workout["calories_burned"] if workout else 0
    adjusted_calories = targets["calories_max"] + workout_cal

    # Update stored timezone if sent
    tz_name = request.headers.get("X-Timezone")
    if tz_name:
        user = get_user(user_id)
        if user and user.get("timezone") != tz_name:
            update_user_timezone(user_id, tz_name)

    vacation = is_vacation(user_id, today)
    today_weight = get_weight(user_id, today)
    latest_weight = get_latest_weight(user_id)

    return {
        "date": today,
        "day_name": date.fromisoformat(today).strftime("%A"),
        "workout": workout,
        "is_social": is_social,
        "is_rest_day": workout is None,
        "is_vacation": vacation,
        "totals": totals,
        "entries": entries,
        "targets": {
            "calories": targets["calories_max"],
            "calories_adjusted": adjusted_calories,
            "workout_calories": workout_cal,
            "protein": targets["protein_target"],
            "fiber": targets["fiber_target"],
        },
        "weight": today_weight,
        "latest_weight": latest_weight,
    }


# ── Vacation ───────────────────────────────────

@app.post("/api/vacation")
async def toggle_vacation(request: Request, log_date: Optional[str] = None):
    user_id = require_auth(request)
    d = log_date or get_client_date(request)
    if is_vacation(user_id, d):
        unset_vacation(user_id, d)
        return {"status": "removed", "is_vacation": False}
    else:
        set_vacation(user_id, d)
        return {"status": "set", "is_vacation": True}


# ── Workout ─────────────────────────────────────

@app.put("/api/workout")
async def update_workout(req: WorkoutRequest, request: Request, log_date: Optional[str] = None):
    user_id = require_auth(request)
    d = log_date or get_client_date(request)
    set_workout(user_id, d, time=req.time, duration_min=req.duration_min,
                label=req.label, calories_burned=req.calories_burned, source=req.source)
    return {"status": "saved"}

@app.delete("/api/workout")
async def remove_workout(request: Request, log_date: Optional[str] = None):
    user_id = require_auth(request)
    d = log_date or get_client_date(request)
    delete_workout(user_id, d)
    return {"status": "deleted"}

@app.post("/api/workout/sync")
async def sync_apple_health(request: Request, token: Optional[str] = None, user_email: Optional[str] = None):
    # Auth: either session cookie or sync token + email
    user_id = None
    try:
        user_id = require_auth(request)
    except Exception:
        pass
    if not user_id:
        if not SYNC_TOKEN or token != SYNC_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid sync token")
        if not user_email:
            raise HTTPException(status_code=400, detail="user_email required with token auth")
        from db import get_conn
        conn = get_conn()
        row = conn.execute("SELECT google_id FROM users WHERE email = ?", (user_email,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        user_id = row["google_id"]

    body = await request.json()
    logger.info(f"Workout sync body: {body}")
    # Try exact key, then fallback to any key containing "calori"
    cal_raw = body.get("active_calories") or body.get("active calories")
    if cal_raw is None:
        for k, v in body.items():
            if "calori" in k.lower() and isinstance(v, (int, float)):
                cal_raw = v
                break
    cal = int(float(cal_raw or 0))
    dur = int(float(body.get("total_duration_min", 0)))
    raw_date = body.get("date", "")
    d = raw_date if isinstance(raw_date, str) and len(raw_date) == 10 else get_client_date(request)
    if is_vacation(user_id, d):
        return {"status": "skipped", "message": "Vacation day — sync skipped"}
    workouts = body.get("workouts", [])
    if cal <= 0:
        return {"status": "skipped", "message": "No active calories to sync"}
    labels = []
    for w in (workouts[:3] if workouts else []):
        if isinstance(w, dict):
            labels.append(w.get("type", "Workout"))
        else:
            labels.append(str(w))
    label = ", ".join(labels) if labels else "Workout"
    first_time = ""
    if workouts and isinstance(workouts[0], dict):
        first_time = workouts[0].get("start_time", "")
    set_workout(user_id, d, time=first_time, duration_min=dur,
                label=label, calories_burned=cal, source="apple_health")
    return {"status": "synced", "date": d, "active_calories": cal}


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
        update_food_log_entry(entry_id, cal, prot, fib, description=req.description, quantity=req.quantity)
        return {"status": "updated"}


@app.post("/api/estimate")
async def estimate_macros(req: ChatRequest, request: Request):
    user_id = require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    try:
        result = await process_chat_message(user_id, f"had {req.message}")
        return {"calories": result.get("calories", 0), "protein": result.get("protein", 0), "fiber": result.get("fiber", 0), "description": result.get("description", req.message)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Analysis ─────────────────────────────────────

@app.get("/api/analysis")
async def get_analysis(request: Request, days: int = 7):
    user_id = require_auth(request)
    ref_date = get_client_date(request)
    history = get_food_log_history(user_id, days, reference_date=ref_date)
    targets = get_user_targets(user_id)

    # Get workout calories for the date range
    if history:
        start = history[0]["log_date"]
        end = history[-1]["log_date"]
    else:
        start = end = ref_date
    workout_map = get_workouts_range(user_id, start, end)
    drinks_map = get_drinks_range(user_id, start, end)
    vacation_set = get_vacation_range(user_id, start, end)

    # Add deficit and drink data to each day, mark vacations
    base_cal = targets["calories_max"]
    for day in history:
        day["is_vacation"] = day["log_date"] in vacation_set
        wk_cal = workout_map.get(day["log_date"], 0)
        day["workout_calories"] = wk_cal
        day["adjusted_target"] = base_cal + wk_cal
        day["deficit"] = day["total_cal"] - (base_cal + wk_cal)
        dr = drinks_map.get(day["log_date"])
        day["drinks"] = dr["count"] if dr else 0
        day["drink_calories"] = dr["calories"] if dr else 0

    # Filter out vacation days for metrics
    active_history = [d for d in history if not d["is_vacation"]]

    # Weight data for the period
    weight_data = get_weight_range(user_id, start, end)

    return {
        "user_id": user_id,
        "days": days,
        "history": history,
        "active_days": len(active_history),
        "targets": {
            "calories": targets["calories_max"],
            "protein": targets["protein_target"],
            "fiber": targets["fiber_target"],
        },
        "weight": weight_data,
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
    user_id = require_auth(request)
    fid = get_fridge_id(user_id)
    return {"items": get_fridge_items(fid)}

@app.post("/api/fridge")
async def add_to_fridge(req: FridgeItemRequest, request: Request):
    user_id = require_auth(request)
    fid = get_fridge_id(user_id)
    add_fridge_item(fid, req.name, req.category, req.quantity)
    return {"status": "added"}

@app.post("/api/fridge/bulk")
async def bulk_add_fridge(req: BulkFridgeRequest, request: Request):
    user_id = require_auth(request)
    fid = get_fridge_id(user_id)
    items_dicts = [{"name": i.name, "quantity": i.quantity, "category": i.category} for i in req.items]
    add_fridge_items_bulk(fid, items_dicts)
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
    user_id = require_auth(request)
    fid = get_fridge_id(user_id)
    remove_fridge_item(fid, item_name)
    return {"status": "removed"}

@app.delete("/api/fridge/id/{item_id}")
async def remove_fridge_by_id(item_id: int, request: Request):
    require_auth(request)
    remove_fridge_item_by_id(item_id)
    return {"status": "removed"}

@app.delete("/api/fridge")
async def clear_all_fridge(request: Request):
    user_id = require_auth(request)
    fid = get_fridge_id(user_id)
    clear_fridge(fid)
    return {"status": "cleared"}


# ── Fridge Sharing ──────────────────────────────

@app.get("/api/fridge/share")
async def get_fridge_share_info(request: Request):
    user_id = require_auth(request)
    user = get_user(user_id)
    group = user.get("fridge_group", "") if user else ""
    if group:
        members = get_fridge_group_members(group)
        return {"shared": True, "code": group, "members": [{"name": m["name"], "picture": m["picture"]} for m in members]}
    return {"shared": False, "code": "", "members": []}

@app.post("/api/fridge/share")
async def create_fridge_share(request: Request):
    import secrets
    user_id = require_auth(request)
    user = get_user(user_id)
    if user.get("fridge_group"):
        return {"code": user["fridge_group"]}
    code = secrets.token_hex(3).upper()
    old_fid = get_fridge_id(user_id)
    set_fridge_group(user_id, code)
    migrate_fridge_items(old_fid, code)
    return {"code": code}

class JoinFridgeRequest(BaseModel):
    code: str

@app.post("/api/fridge/join")
async def join_fridge_share(req: JoinFridgeRequest, request: Request):
    user_id = require_auth(request)
    code = req.code.strip().upper()
    members = get_fridge_group_members(code)
    if not members:
        raise HTTPException(status_code=404, detail="Invalid share code")
    old_fid = get_fridge_id(user_id)
    set_fridge_group(user_id, code)
    if old_fid != code:
        migrate_fridge_items(old_fid, code)
    return {"status": "joined", "members": [{"name": m["name"], "picture": m["picture"]} for m in get_fridge_group_members(code)]}

@app.post("/api/fridge/leave")
async def leave_fridge_share(request: Request):
    user_id = require_auth(request)
    set_fridge_group(user_id, "")
    return {"status": "left"}


# ── Meal Ideas ────────────────────────────────────

@app.post("/api/meals/suggest")
async def suggest_meals(req: MealIdeasRequest, request: Request):
    user_id = require_auth(request)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    try:
        log_date = get_client_date(request)
        recipes = await generate_meal_ideas(user_id, req.meal_type, req.servings, req.cuisine, plan_date=date.fromisoformat(log_date))
        return {"recipes": recipes}
    except Exception as e:
        logger.error(f"Meal ideas failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
