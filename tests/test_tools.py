"""tools.py: the functions behind the MCP tools, plus the shared /api/today and /api/analysis logic."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import db
import tools
from conftest import login
from tools import ToolError


def test_today_uses_stored_timezone():
    db.update_user_timezone("alice", "Asia/Kolkata")
    assert tools.get_user_today("alice") == str(datetime.now(ZoneInfo("Asia/Kolkata")).date())


def test_log_food_returns_day_with_remaining():
    r = tools.log_food("alice", [
        {"description": "masala chai", "portion": "1 cup", "calories": 120, "protein": 4},
        {"description": "poha", "calories": 300, "protein": 6, "fiber": 3},
    ], source="photo")
    day = r["day"]
    assert r["logged"] == 2
    assert day["totals"] == {"calories": 420, "protein": 10, "fiber": 3}
    assert day["remaining"]["calories"] == day["targets"]["calories"] - 420
    assert {e["description"] for e in day["entries"]} == {"masala chai (1 cup)", "poha"}
    assert tools.search_food_history("alice", "chai")["matches"][0]["calories"] == 120


def test_log_food_validates_whole_plate_before_writing():
    with pytest.raises(ToolError, match="out of range"):
        tools.log_food("alice", [{"description": "ok", "calories": 100}, {"description": "coffee", "calories": 9000}])
    assert tools.get_today("alice")["totals"]["calories"] == 0


@pytest.mark.parametrize("call", [
    lambda: tools.get_today("alice", "yesterday"),
    lambda: tools.get_progress("alice", 5),
    lambda: tools.log_food("alice", []),
    lambda: tools.log_food("alice", [{"description": "x", "calories": 1}], source="fax"),
    lambda: tools.log_weight("alice", -3),
    lambda: tools.set_day_type("alice", None, "holiday"),
    lambda: tools.search_food_history("alice", "  "),
])
def test_bad_input_raises_tool_error(call):
    with pytest.raises(ToolError):
        call()


def test_update_and_delete_are_owner_scoped():
    day = tools.log_food("alice", [{"description": "poha", "calories": 300, "protein": 6, "fiber": 3}])["day"]
    eid = day["entries"][0]["id"]
    with pytest.raises(ToolError, match="No food entry"):
        tools.update_food("bob", eid, calories=1)
    with pytest.raises(ToolError, match="No food entry"):
        tools.delete_food("bob", eid)

    entry = tools.update_food("alice", eid, calories=250)["day"]["entries"][0]
    assert (entry["calories"], entry["protein"], entry["fiber"]) == (250, 6, 3)   # untouched fields kept
    assert tools.delete_food("alice", eid)["day"]["totals"]["calories"] == 0


def test_workout_raises_target_and_day_types():
    day = tools.log_workout("alice", "Bootcamp", 45, 400)["day"]
    assert day["day_type"] == "normal" and day["targets"]["calories"] == 1800 + 400
    assert tools.set_day_type("alice", None, "rest")["day"]["day_type"] == "rest"
    assert tools.set_day_type("alice", None, "vacation")["day"]["day_type"] == "vacation"
    tools.set_day_type("alice", None, "normal")
    assert db.get_workout("alice", tools.get_user_today("alice")) is None   # rest day undone


def test_weight_and_fridge():
    assert tools.log_weight("alice", 158.4)["latest_weight"]["weight"] == 158.4
    tools.add_fridge_items("alice", [{"name": "Paneer", "quantity": "200g", "category": "dairy"},
                                     {"name": "spinach"}, {"name": "  "}])
    r = tools.remove_fridge_items("alice", ["paneer", "tofu"])
    assert r["removed"] == ["Paneer"] and r["not_found"] == ["tofu"]
    assert [i["name"] for i in r["items"]] == ["spinach"]
    assert tools.get_fridge("bob")["items"] == []


def test_progress_deficit_and_vacation():
    today = date.fromisoformat(tools.get_user_today("alice"))
    for i in range(3):
        db.add_food_log_entry(str(today - timedelta(days=i)), "alice", "meal", 1800, 100)
    db.set_vacation("alice", str(today - timedelta(days=1)))
    # Weight is reported for the span covered by food entries (same as the Analysis tab).
    db.log_weight("alice", str(today - timedelta(days=2)), 160.0)
    db.log_weight("alice", str(today), 158.0)

    p = tools.get_progress("alice", 7)
    s = p["summary"]
    assert s["active_days"] == 2 and s["vacation_days"] == 1
    assert s["avg_deficit"] == 1800 - 2300          # TDEE 2300, no workouts: negative = deficit
    assert s["est_weight_change_lbs"] == round(2 * (1800 - 2300) / 3500, 1)
    assert p["weight"]["change"] == -2.0 and p["weight"]["goal"] == 150


def test_website_routes_use_shared_logic(client):
    login(client, "alice")
    tools.log_food("alice", [{"description": "chai", "calories": 120, "protein": 4}])
    today = client.get("/api/today", headers={"X-Timezone": "America/Chicago"}).json()
    assert set(today) >= {"date", "workout", "is_rest_day", "is_vacation", "totals", "entries", "targets",
                          "weight", "latest_weight"}
    assert today["totals"]["calories"] == 120
    assert today["targets"]["calories_adjusted"] == today["targets"]["calories"] + today["targets"]["workout_calories"]

    analysis = client.get("/api/analysis?days=7", headers={"X-Timezone": "America/Chicago"}).json()
    assert analysis["history"][-1]["deficit"] == 120 - 2300
    assert {"is_vacation", "drinks", "adjusted_target", "workout_calories"} <= set(analysis["history"][-1])
