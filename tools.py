"""Tool layer for the dietician pal (see docs/mcp-agent-plan.md).

Plain functions over db.py. Every tool takes the caller's ``user_id`` as its first
argument; it must come from the authenticated session or OAuth token, never from
tool input. Dates default to the user's stored timezone. Writes return the updated
day so the caller can confirm without a second call.

The MCP endpoint (phase 3) registers these. ``compute_day`` and ``compute_history``
are also used by the website's /api/today and /api/analysis routes so both stay in sync.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import db
from config import SOCIAL_DAYS, WORKOUTS

DEFAULT_TZ = "America/Chicago"
REST_DAY_LABEL = "Rest Day"
MAX_ITEM_CALORIES = 5000
MAX_ITEM_PROTEIN = 300
MAX_ITEM_FIBER = 150


class ToolError(ValueError):
    """Bad tool input. The message is safe to show to the model/user."""


# ── Shared helpers ───────────────────────────────

def get_user_targets(user_id):
    user = db.get_user(user_id)
    if user:
        tdee = user.get("tdee", 0) or 0
        return {
            "calories_min": user["calories_min"],
            "calories_max": user["calories_max"],
            "protein_target": user["protein_target"],
            "fiber_target": user["fiber_target"],
            "tdee": tdee if tdee > 0 else user["calories_max"],
        }
    return {"calories_min": 1800, "calories_max": 2000, "protein_target": 150, "fiber_target": 30, "tdee": 2000}


def get_user_today(user_id):
    """Today's date in the user's stored timezone (MCP calls carry no X-Timezone header)."""
    user = db.get_user(user_id)
    tz_name = (user or {}).get("timezone") or DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    return str(datetime.now(tz).date())


def _resolve_date(user_id, d):
    if not d:
        return get_user_today(user_id)
    try:
        return date.fromisoformat(d).isoformat()
    except (TypeError, ValueError):
        raise ToolError(f"Invalid date {d!r}; use YYYY-MM-DD.")


def _resolve_workout(user_id, day):
    """DB entry for the day > most recent earlier entry > config default by weekday."""
    workout = db.get_workout(user_id, day)
    if workout:
        return {"time": workout["time"], "duration_min": workout["duration_min"],
                "label": workout["label"], "calories_burned": workout["calories_burned"],
                "source": workout["source"]}
    prev = db.get_latest_workout(user_id, day)
    if prev:
        return {"time": prev["time"], "duration_min": prev["duration_min"],
                "label": prev["label"], "calories_burned": 0,
                "source": prev["source"], "inherited": True}
    config_w = WORKOUTS.get(date.fromisoformat(day).weekday())
    if config_w:
        return {**config_w, "calories_burned": 0, "source": "default", "inherited": True}
    return None


def _is_rest(workout):
    # Matches the frontend: no workout, a "Rest Day" entry, or zero duration.
    return not workout or workout.get("label") == REST_DAY_LABEL or not workout.get("duration_min")


def compute_day(user_id, day):
    """Dashboard data for one day. Same shape as /api/today."""
    totals = db.get_food_log_totals(day, user_id)
    entries = db.get_food_log(day, user_id)
    targets = get_user_targets(user_id)
    workout = _resolve_workout(user_id, day)
    workout_cal = workout["calories_burned"] if workout else 0
    return {
        "date": day,
        "day_name": date.fromisoformat(day).strftime("%A"),
        "workout": workout,
        "is_social": date.fromisoformat(day).weekday() in SOCIAL_DAYS,
        "is_rest_day": workout is None,
        "is_vacation": db.is_vacation(user_id, day),
        "totals": totals,
        "entries": entries,
        "targets": {
            "calories": targets["calories_max"],
            "calories_adjusted": targets["calories_max"] + workout_cal,
            "workout_calories": workout_cal,
            "protein": targets["protein_target"],
            "fiber": targets["fiber_target"],
        },
        "weight": db.get_weight(user_id, day),
        "latest_weight": db.get_latest_weight(user_id),
    }


def compute_history(user_id, days, ref_date):
    """Per-day totals with TDEE-based deficit, drinks and vacation flags. Same data as /api/analysis."""
    history = db.get_food_log_history(user_id, days, reference_date=ref_date)
    targets = get_user_targets(user_id)
    if history:
        start, end = history[0]["log_date"], history[-1]["log_date"]
    else:
        start = end = ref_date
    workout_map = db.get_workouts_range(user_id, start, end)
    drinks_map = db.get_drinks_range(user_id, start, end)
    vacation_set = db.get_vacation_range(user_id, start, end)
    base_cal = targets["tdee"]
    for day in history:
        day["is_vacation"] = day["log_date"] in vacation_set
        wk_cal = workout_map.get(day["log_date"], 0)
        day["workout_calories"] = wk_cal
        day["adjusted_target"] = base_cal + wk_cal
        day["deficit"] = day["total_cal"] - (base_cal + wk_cal)
        dr = drinks_map.get(day["log_date"])
        day["drinks"] = dr["count"] if dr else 0
        day["drink_calories"] = dr["calories"] if dr else 0
    return {
        "history": history,
        "targets": targets,
        "start": start,
        "end": end,
        "weight": db.get_weight_range(user_id, start, end),
    }


def _day_summary(user_id, day):
    """compute_day plus what's remaining and a single day_type, trimmed for the model."""
    d = compute_day(user_id, day)
    t, tg = d["totals"], d["targets"]
    if d["is_vacation"]:
        day_type = "vacation"
    elif _is_rest(d["workout"]):
        day_type = "rest"
    else:
        day_type = "normal"
    return {
        "date": d["date"],
        "day_name": d["day_name"],
        "day_type": day_type,
        "totals": t,
        "targets": {"calories": tg["calories_adjusted"], "base_calories": tg["calories"],
                    "workout_calories": tg["workout_calories"], "protein": tg["protein"], "fiber": tg["fiber"]},
        "remaining": {
            "calories": tg["calories_adjusted"] - t["calories"],
            "protein": tg["protein"] - t["protein"],
            "fiber": tg["fiber"] - t["fiber"],
        },
        "entries": [{"id": e["id"], "description": e["description"], "quantity": e["quantity"],
                     "calories": e["calories"], "protein": e["protein"], "fiber": e["fiber"],
                     "time_logged": e["time_logged"]} for e in d["entries"]],
        "workout": d["workout"],
        "weight": d["weight"],
        "latest_weight": d["latest_weight"],
    }


def _num(value, field, maximum):
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a number.")
    if n < 0 or n > maximum:
        raise ToolError(f"{field} {n:g} is out of range (0–{maximum}).")
    return round(n) if field == "calories" else round(n, 1)


# ── Reading data ─────────────────────────────────

def get_today(user_id, date=None):
    return _day_summary(user_id, _resolve_date(user_id, date))


def get_progress(user_id, days=7):
    if days not in (7, 14, 30):
        raise ToolError("days must be 7, 14 or 30.")
    ref = get_user_today(user_id)
    h = compute_history(user_id, days, ref)
    active = [d for d in h["history"] if not d["is_vacation"]]
    n = len(active) or 1
    total_deficit = sum(d["deficit"] for d in active)
    weights = h["weight"]
    return {
        "days": days,
        "through": ref,
        "per_day": [{"date": d["log_date"], "calories": d["total_cal"], "protein": d["total_protein"],
                     "fiber": d["total_fiber"], "target": d["adjusted_target"], "deficit": d["deficit"],
                     "drinks": d["drinks"], "vacation": d["is_vacation"]} for d in h["history"]],
        "summary": {
            "active_days": len(active),
            "vacation_days": len(h["history"]) - len(active),
            "avg_calories": round(sum(d["total_cal"] for d in active) / n),
            "avg_protein": round(sum(d["total_protein"] for d in active) / n),
            "avg_fiber": round(sum(d["total_fiber"] for d in active) / n),
            "avg_deficit": round(total_deficit / n),
            "total_deficit": total_deficit,
            # Negative deficit = under TDEE. 3500 cal ≈ 1 lb.
            "est_weight_change_lbs": round(total_deficit / 3500, 1),
            "drinks": sum(d["drinks"] for d in active),
            "note": "deficit = calories eaten - (TDEE + workout). Negative is a deficit. Today may be incomplete.",
        },
        "weight": {
            "entries": weights,
            "change": round(weights[-1]["weight"] - weights[0]["weight"], 1) if len(weights) >= 2 else None,
            "goal": (db.get_user(user_id) or {}).get("weight_target"),
        },
    }


def get_fridge(user_id):
    items = db.get_fridge_items(db.get_fridge_id(user_id))
    return {"items": [{"name": i["name"], "quantity": i["quantity"], "category": i["category"]} for i in items]}


def search_food_history(user_id, query):
    # quick_log_history is household-wide (no user_id column); see plan doc.
    if not query or not query.strip():
        raise ToolError("query is required.")
    return {"matches": db.search_quick_logs(query.strip())}


# ── Logging food ─────────────────────────────────

def log_food(user_id, items, date=None, source="text"):
    if not items:
        raise ToolError("items must contain at least one food.")
    if source not in ("text", "photo"):
        raise ToolError("source must be 'text' or 'photo'.")
    day = _resolve_date(user_id, date)
    clean = []
    for i, item in enumerate(items):
        desc = (item.get("description") or "").strip()
        if not desc:
            raise ToolError(f"items[{i}].description is required.")
        portion = (item.get("portion") or "").strip()
        clean.append((
            f"{desc} ({portion})" if portion else desc,
            _num(item.get("calories"), "calories", MAX_ITEM_CALORIES),
            _num(item.get("protein"), "protein", MAX_ITEM_PROTEIN),
            _num(item.get("fiber"), "fiber", MAX_ITEM_FIBER),
        ))
    # Validate everything before writing so a bad item doesn't leave a half-logged plate.
    for desc, cal, prot, fib in clean:
        db.add_food_log_entry(day, user_id, desc, cal, prot, fib, source=f"mcp_{source}")
        db.save_quick_log(desc, cal, prot, fib)
    return {"logged": len(clean), "day": _day_summary(user_id, day)}


def _entry_date(user_id, entry_id):
    conn = db.get_conn()
    row = conn.execute("SELECT log_date FROM food_log WHERE id = ? AND user_id = ?", (entry_id, user_id)).fetchone()
    conn.close()
    if not row:
        raise ToolError(f"No food entry {entry_id}.")
    return row["log_date"]


def update_food(user_id, entry_id, description=None, calories=None, protein=None, fiber=None):
    # No quantity input: the DB's quantity is a display multiplier that totals ignore,
    # so the caller passes already-scaled calories/protein/fiber instead.
    day = _entry_date(user_id, entry_id)
    current = next(e for e in db.get_food_log(day, user_id) if e["id"] == entry_id)
    db.update_food_log_entry(
        user_id, entry_id,
        _num(current["calories"] if calories is None else calories, "calories", MAX_ITEM_CALORIES),
        _num(current["protein"] if protein is None else protein, "protein", MAX_ITEM_PROTEIN),
        _num(current["fiber"] if fiber is None else fiber, "fiber", MAX_ITEM_FIBER),
        description=description.strip() if description else None,
    )
    return {"updated": entry_id, "day": _day_summary(user_id, day)}


def delete_food(user_id, entry_id):
    day = _entry_date(user_id, entry_id)
    db.delete_food_log_entry(user_id, entry_id)
    return {"deleted": entry_id, "day": _day_summary(user_id, day)}


# ── Body and workouts ────────────────────────────

def log_workout(user_id, label, duration_min, calories_burned, date=None):
    day = _resolve_date(user_id, date)
    if not label or not label.strip():
        raise ToolError("label is required.")
    duration = int(_num(duration_min, "duration_min", 600))
    burned = int(_num(calories_burned, "calories_burned", 3000))
    db.set_workout(user_id, day, duration_min=duration, label=label.strip(), calories_burned=burned, source="mcp")
    return {"logged": True, "day": _day_summary(user_id, day)}


def log_weight(user_id, weight, date=None):
    day = _resolve_date(user_id, date)
    w = _num(weight, "weight", 1000)
    if w <= 0:
        raise ToolError("weight must be positive.")
    db.log_weight(user_id, day, w)
    return {"logged": True, "date": day, "weight": w, "latest_weight": db.get_latest_weight(user_id)}


def set_day_type(user_id, date, type):
    day = _resolve_date(user_id, date)
    if type not in ("normal", "rest", "vacation"):
        raise ToolError("type must be 'normal', 'rest' or 'vacation'.")
    if type == "vacation":
        db.set_vacation(user_id, day)
    else:
        db.unset_vacation(user_id, day)
        current = db.get_workout(user_id, day)
        if type == "rest":
            # Same as the website's "Mark as Rest Day".
            db.set_workout(user_id, day, time="", duration_min=0, label=REST_DAY_LABEL, calories_burned=0, source="manual")
        elif current and current["label"] == REST_DAY_LABEL:
            # Undo a rest day; fall back to the usual workout resolution.
            db.delete_workout(user_id, day)
    return {"day": _day_summary(user_id, day)}


# ── Fridge ───────────────────────────────────────

def add_fridge_items(user_id, items):
    clean = [{"name": (i.get("name") or "").strip(), "quantity": i.get("quantity") or "",
              "category": i.get("category") or "other"} for i in items or []]
    clean = [i for i in clean if i["name"]]
    if not clean:
        raise ToolError("items must contain at least one named item.")
    db.add_fridge_items_bulk(db.get_fridge_id(user_id), clean)
    return {"added": len(clean), **get_fridge(user_id)}


def remove_fridge_items(user_id, names):
    fid = db.get_fridge_id(user_id)
    existing = {i["name"].lower(): i["name"] for i in db.get_fridge_items(fid)}
    removed, not_found = [], []
    for n in names or []:
        match = existing.get((n or "").strip().lower())
        if match:
            db.remove_fridge_item(fid, match)
            removed.append(match)
        else:
            not_found.append(n)
    return {"removed": removed, "not_found": not_found, **get_fridge(user_id)}
