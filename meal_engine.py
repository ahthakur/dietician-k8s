"""
Personal Dietician — Meal Engine
Chat-based: parses user intent, logs food, generates recipes on demand.
"""

from __future__ import annotations
from typing import Optional, List

import json
import logging
import httpx
from datetime import date, timedelta
from config import (
    ANTHROPIC_API_KEY, USERS, WORKOUTS, SOCIAL_DAYS,
    SOCIAL_DRINK_CALORIES, CUISINE_PREFERENCE,
)
from db import (
    get_fridge_items, get_food_log_totals, get_recent_dishes,
    log_meal_to_history, was_weekly_protein_used, mark_weekly_protein_used,
)

logger = logging.getLogger("dietician.meal_engine")

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-5-20250929"


async def _call_claude(prompt: str, system: str = "", max_tokens: int = 1500) -> str:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    messages = [{"role": "user", "content": prompt}]
    body = {"model": MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(API_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def _call_claude_vision(image_b64: str, media_type: str, prompt: str) -> str:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": MODEL, "max_tokens": 1000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(API_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def process_chat_message(user_id: str, message: str, plan_date: Optional[date] = None) -> dict:
    """Main chat handler. Determines intent and responds accordingly."""
    if plan_date is None:
        plan_date = date.today()

    user = USERS[user_id]
    totals = get_food_log_totals(str(plan_date), user_id)
    remaining_cal = user["calories_max"] - totals["calories"]
    remaining_prot = user["protein_target"] - totals["protein"]
    remaining_fiber = user["fiber_target"] - totals["fiber"]

    dow = plan_date.weekday()
    workout = WORKOUTS.get(dow)
    is_social = dow in SOCIAL_DAYS

    fridge = get_fridge_items()
    fridge_desc = ", ".join([f"{f['name']}" + (f" ({f['quantity']})" if f.get('quantity') else "") for f in fridge]) if fridge else "empty"

    workout_info = f"Workout today: {workout['label']}" if workout else "Rest day"
    social_info = " (social evening, account for ~2 drinks ~300 cal)" if is_social else ""

    system_prompt = f"""You are a personal dietician assistant for {user['name']}.

DAILY TARGETS: {user['calories_min']}-{user['calories_max']} cal, {user['protein_target']}g protein, {user['fiber_target']}g fiber
CONSUMED SO FAR TODAY: {totals['calories']} cal, {totals['protein']}g protein, {totals['fiber']}g fiber
REMAINING: ~{remaining_cal} cal, ~{remaining_prot}g protein, ~{remaining_fiber}g fiber
{workout_info}{social_info}
FRIDGE: {fridge_desc}
CUISINE PREFERENCE: {CUISINE_PREFERENCE}

You handle TWO types of messages:

TYPE 1 - FOOD LOGGING: The user describes what they ate.
Examples: "had eggs and toast", "protein shake", "2 rotis with dal", "chicken salad for lunch"
For these, respond with ONLY this JSON (no other text):
{{"type": "log", "description": "cleaned up description of food", "calories": 450, "protein": 35, "fiber": 5, "carbs": 40, "fat": 15}}

TYPE 2 - MEAL IDEAS: The user asks for food suggestions.
Examples: "what should I make for dinner?", "lunch ideas", "need a high protein snack", "what can I cook?"
For these, respond with ONLY this JSON (no other text):
{{"type": "suggest", "message": "your conversational response with 2-3 meal suggestions, include estimated cal/protein/fiber for each, keep it concise and practical, under 30 min cook time each"}}

TYPE 3 - GENERAL CHAT: Questions about nutrition, progress, or anything else.
Examples: "how am I doing today?", "am I getting enough protein?", "what foods are high in fiber?"
For these, respond with ONLY this JSON (no other text):
{{"type": "chat", "message": "your helpful response"}}

RULES:
- For logging, be realistic with portion estimates. A typical Indian meal serving, not restaurant sized.
- For suggestions, prioritize fridge ingredients when available. Always include fiber-rich options.
- Protein shake = 100 cal, 25g protein, 0g fiber.
- Keep suggestions under 30 minutes cooking time.
- Always respond with valid JSON only, no markdown fences, no extra text."""

    raw = await _call_claude(message, system=system_prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"type": "chat", "message": cleaned}


async def analyze_food_photo(image_b64: str, media_type: str) -> dict:
    """Analyze a photo of food to estimate calories, protein, and fiber."""
    prompt = """Look at this food photo and estimate the nutritional content.
Identify the dish/items, estimate portion size, and provide calorie, protein, and fiber estimates.
Be realistic about portion sizes.

Return ONLY valid JSON, no markdown fences:
{
  "description": "What the food appears to be",
  "items": ["item 1", "item 2"],
  "estimated_calories": 450,
  "estimated_protein": 35,
  "estimated_fiber": 5,
  "estimated_carbs": 40,
  "estimated_fat": 18,
  "confidence": "medium",
  "notes": "Any relevant notes"
}"""
    raw = await _call_claude_vision(image_b64, media_type, prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


async def analyze_fridge_photo(image_b64: str, media_type: str) -> List[dict]:
    """Use Claude Vision to identify food items in a fridge photo with estimated quantities."""
    prompt = """Identify every food item in this fridge/pantry photo.
For each item, estimate the quantity you can see.
Be specific with item names.

Return ONLY valid JSON, no markdown fences:
[
  {"name": "eggs", "quantity": "about 8"},
  {"name": "milk", "quantity": "half gallon"},
  {"name": "spinach", "quantity": "1 bag"}
]"""
    raw = await _call_claude_vision(image_b64, media_type, prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    items = json.loads(cleaned)
    result = []
    for i in items:
        if isinstance(i, dict):
            result.append({"name": str(i.get("name", "")).strip(), "quantity": str(i.get("quantity", "")).strip()})
        elif isinstance(i, str):
            result.append({"name": i.strip(), "quantity": ""})
    return [r for r in result if r["name"]]


async def analyze_receipt_photo(image_b64: str, media_type: str) -> dict:
    """Analyze a grocery receipt photo to extract purchased items."""
    prompt = """Analyze this grocery store receipt. Extract every food/grocery item.
Translate abbreviated names to clear names.
Return ONLY valid JSON, no markdown fences:
{
  "store": "Store name or unknown",
  "items": [
    {"name": "Boneless Chicken Breast", "quantity": "2 lbs", "price": "8.99"}
  ],
  "total": "45.67",
  "date": "2026-04-22"
}"""
    raw = await _call_claude_vision(image_b64, media_type, prompt)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


async def generate_grocery_list(week_start_date: Optional[date] = None) -> dict:
    if week_start_date is None:
        week_start_date = date.today()
    fridge = get_fridge_items()
    fridge_names = [f["name"] for f in fridge]

    prompt = f"""You are a personal dietician creating a weekly grocery list.
HOUSEHOLD: Two people, Indian cuisine primarily. One salmon and one branzino meal per week.
Daily protein shakes. High fiber foods needed (lentils, vegetables, whole grains).
CURRENTLY IN FRIDGE: {', '.join(fridge_names) if fridge_names else 'Empty'}
Generate a practical grocery list organized by category with quantities.
Return ONLY valid JSON:
{{
  "categories": [
    {{"name": "Proteins", "items": ["Chicken breast (2 lbs)", "Paneer (400g)"]}},
    {{"name": "Vegetables", "items": ["Spinach (2 bunches)"]}}
  ],
  "notes": "Shopping tips"
}}"""

    raw = await _call_claude(prompt, max_tokens=2000)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)
