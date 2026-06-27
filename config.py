"""
Personal Dietician — Configuration
"""

import os
import json
import secrets

# ─── Anthropic API ────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── Server ───────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 80))

# ─── Ntfy ─────────────────────────────────────────────────
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPICS = {
    "you": os.environ.get("NTFY_TOPIC_YOU", "dietician-abhijit-2026"),
    "wife": os.environ.get("NTFY_TOPIC_WIFE", "dietician-mona-2026"),
}
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "dietician-thakur-2026")

# ─── Users ────────────────────────────────────────────────
USERS = {
    "you": {
        "name": "Abhijit",
        "calories_min": 1700,
        "calories_max": 1800,
        "protein_target": 160,
        "fiber_target": 35,
        "allowed_proteins": ["chicken", "paneer", "eggs", "lentils", "dal", "chickpeas", "tofu"],
        "weekly_proteins": ["salmon", "branzino"],
    },
    "wife": {
        "name": "Mona",
        "calories_min": 1300,
        "calories_max": 1400,
        "protein_target": 130,
        "fiber_target": 28,
        "allowed_proteins": ["chicken", "paneer", "eggs", "lentils", "dal", "chickpeas", "tofu"],
        "weekly_proteins": ["salmon", "branzino"],
    },
}

# ─── Meal Schedule ────────────────────────────────────────
MEALS = [
    {"id": "breakfast", "label": "Breakfast", "time": "08:30", "calorie_pct": 0.25},
    {"id": "lunch", "label": "Lunch", "time": "12:30", "calorie_pct": 0.30},
    {"id": "snack", "label": "Evening Snack", "time": "16:00", "calorie_pct": 0.15},
    {"id": "dinner", "label": "Dinner", "time": "20:00", "calorie_pct": 0.30},
]

# ─── Workout Schedule ────────────────────────────────────
# 0=Monday ... 6=Sunday
WORKOUTS = {
    0: {"time": "17:30", "duration_min": 45, "label": "Burn Bootcamp 5:30 PM"},
    1: {"time": "17:30", "duration_min": 45, "label": "Burn Bootcamp 5:30 PM"},
    2: {"time": "17:30", "duration_min": 45, "label": "Burn Bootcamp 5:30 PM"},
    3: {"time": "17:30", "duration_min": 45, "label": "Burn Bootcamp 5:30 PM"},
    4: {"time": "17:30", "duration_min": 45, "label": "Burn Bootcamp 5:30 PM"},
    5: {"time": "09:00", "duration_min": 45, "label": "Burn Bootcamp 9:00 AM"},
}

# ─── Schedule Bounds ──────────────────────────────────────
WAKE_UP = "07:00"
SLEEP_TIME = "23:30"

# ─── Social Days ──────────────────────────────────────────
SOCIAL_DAYS = [4, 5]
SOCIAL_DRINK_CALORIES = 300

# ─── Cuisine ──────────────────────────────────────────────
CUISINE_PREFERENCE = "Indian cuisine first preference, but not strictly. Other cuisines welcome."

# ─── Database ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "dietician.db")

# ─── Google OAuth ────────────────────────────────────────
_oauth_file = os.path.join(os.path.dirname(__file__), "OAuth-client.json")
_oauth_data = {}
if os.path.exists(_oauth_file):
    with open(_oauth_file) as f:
        _oauth_data = json.load(f).get("web", {})

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", _oauth_data.get("client_id", ""))
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", _oauth_data.get("client_secret", ""))
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
BASE_URL = os.environ.get("BASE_URL", "https://dietician.abhijitt.com")
