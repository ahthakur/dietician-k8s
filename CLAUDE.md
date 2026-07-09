# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A personal dietician web app — chat-first interface for food logging, macro tracking (calories/protein/fiber), meal suggestions with guided cooking, and photo analysis. Multi-user via Google OAuth. Uses Claude API for AI features (food estimation, structured recipe generation, fridge/receipt photo analysis).

## Running the App

```bash
pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-... python server.py
```

Server starts on `http://0.0.0.0:80` (override with `PORT` env var). No build step for the frontend — it's a single `static/index.html` file served directly by FastAPI.

Google OAuth requires `OAuth-client.json` in the project root (gitignored) or `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` env vars. `BASE_URL` defaults to `https://dietician.abhijitt.com`.

No test suite exists.

## Architecture

**Python backend (FastAPI + SQLite + Claude API), single-page HTML frontend.**

- `server.py` (~700 lines) — FastAPI app, all API routes inline (no routers). Pydantic models for request validation. `get_client_date(request)` helper reads `X-Timezone` header to compute the user's local date.
- `config.py` — Env vars, meal schedule, workout schedule (global defaults), social days, cuisine preference, `SYNC_TOKEN` for external API auth. User-specific targets (calories, protein, fiber, weight) live in the DB `users` table, not here.
- `meal_engine.py` (~300 lines) — Claude API integration. Calls `api.anthropic.com/v1/messages` directly via `httpx` (no SDK). Model is `claude-sonnet-4-5-20250929`. Core functions: `_call_claude()` for text, `_call_claude_vision()` for images, `generate_meal_ideas()` for structured recipe generation.
- `db.py` (~680 lines) — SQLite data layer. All tables created in `init_db()`. Each function opens its own connection via `get_conn()` (WAL mode). No connection pooling or shared connections.
- `auth.py` — Google OAuth via Authlib. Session-based auth; `require_auth(request)` extracts `user_id` (Google sub) from session and raises 401 if missing.
- `static/index.html` (~1100 lines) — Complete SPA in a single HTML file (inline CSS + JS). Glassmorphic dark UI, mobile-first. Sends `X-Timezone` header on every fetch request.

## API Route Groups

All routes are in `server.py`. Auth routes are mounted from `auth.py` functions:

- `/auth/*` — login, callback, me, logout (Google OAuth)
- `/api/chat` — Main chat interface (POST). Intent classification returns `log`, `suggest`, or `chat` JSON
- `/api/estimate` — Estimate-only endpoint (POST). Returns macros without logging. Used by re-estimate flows.
- `/api/today` — Dashboard data (GET). Returns food log, targets, workout, weight, chat history for a date
- `/api/food-log` — CRUD for food entries. PUT supports `re_estimate: true` to re-run Claude estimation
- `/api/photo/{food,fridge,receipt}` — Vision analysis endpoints. All return analysis without auto-saving
- `/api/fridge` — CRUD + bulk add. Fridge items are per-user, or shared via fridge groups
- `/api/fridge/share` — GET share info, POST to generate share code
- `/api/fridge/join` — Join a shared fridge by code
- `/api/fridge/leave` — Leave a shared fridge
- `/api/workout` — CRUD + Apple Health sync at `/api/workout/sync`
- `/api/vacation` — Toggle vacation mode (excludes day from analysis metrics)
- `/api/analysis` — Multi-day macro history with deficit calculations and weight data
- `/api/weight` — Weight tracking CRUD. POST logs/upserts, GET returns history, DELETE removes entry
- `/api/meals/suggest` — Structured recipe generation (POST). Returns 3 recipes with ingredients, steps, timers
- `/api/suggestions` — Quick log autocomplete from `quick_log_history`

## Deployment

Production deploys via SCP to a GCP VM named `dietician` in `us-central1-f`, then `sudo systemctl restart dietician`. Python 3.14, venv at `~/dietician/venv`. Static file changes (index.html) don't need a restart but must be SCP'd to `~/dietician/static/index.html` (not just `~/dietician/`). The repo also contains a Helm chart (`Chart.yaml`, `values.yaml`, `templates/`) but Kubernetes is not used for production.

## Key Patterns

- **User identification**: Google OAuth `sub` claim stored in session as `user_id`. Per-user targets stored in DB `users` table (calories_min, calories_max, protein_target, fiber_target, weight_target, timezone).
- **Timezone handling**: Frontend sends IANA timezone via `X-Timezone` header. Server's `get_client_date(request)` uses it to compute the correct local date. User's timezone is persisted in DB for future use.
- **Claude API responses**: Expected as raw JSON (no markdown fences). The engine strips fences as a fallback. Three chat response types: `log` (food entry), `suggest` (meal ideas), `chat` (general). The system prompt includes current macro totals and fridge inventory for context.
- **Photo scan flow**: Backend analyzes but does NOT auto-save. Frontend shows a review modal with editable item quantities (assumed portions), delete buttons, and a Re-estimate button. Re-estimate calls `/api/estimate` (not `/api/chat`) to avoid auto-logging. Applies to food photos, fridge photos, and receipt scans.
- **DB migrations**: `init_db()` uses try/except around ALTER TABLE statements to add columns idempotently — if the column already exists, the OperationalError is silently caught. New columns must be added this way.
- **Fridge sharing**: `fridge_items` table is keyed on `(user_id, name)`. Users can create a shared fridge group (6-char hex code) — members of the same `fridge_group` share one fridge. `get_fridge_id(user_id)` resolves to the group ID if set, else the user's own ID. All fridge DB functions take this resolved ID as first argument.
- **Quick log dedup**: `quick_log_history` stores descriptions lowercased, uses UPSERT to increment `times_used` on repeats. Powers the autocomplete suggestions endpoint.
- **Drink detection**: `db.py` has a `DRINK_KEYWORDS` tuple (~40 terms). `get_drinks_range()` scans food log descriptions for keyword matches to compute drink counts/calories per day for analysis.
- **Workout resolution chain**: DB entry for today → most recent previous DB entry → config defaults by weekday. Supports manual entry and Apple Health sync via iOS Shortcuts.
- **Apple Health sync**: `POST /api/workout/sync` accepts token+email auth (no browser session). Uses `SYNC_TOKEN` env var. Fuzzy JSON key matching (`"calori" in key.lower()`) for robust iOS Shortcut compatibility.
- **Calorie deficit**: adjusted target = base calories + workout active calories. Analysis tab shows deficit per day and cumulative summary with weight loss estimate (3500 cal = 1 lb). Vacation days are excluded from metrics.
- **Weight tracking**: `weight_log` table with UNIQUE(user_id, log_date). Weight is logged from the profile modal (current weight + goal weight target). Analysis shows weight chart with goal line when 2+ entries exist. `/api/today` returns today's weight and latest weight entry.
- **Food log entries**: Support share (native Web Share API), quantity multiplier (1-4x in edit modal), and re-estimation (calls `/api/estimate` for fresh calorie estimate without logging). Ordered descending (latest at top).
- **Meals tab (guided cooking)**: Replaces the old grocery tab. `generate_meal_ideas()` in `meal_engine.py` returns 3 structured recipes with ingredients, fridge matches, and step-by-step instructions with optional `timer_seconds` per step. Frontend renders recipe cards; tapping one enters cook mode (ingredients checklist → one-step-at-a-time with visual countdown timers → log meal on completion). "More Ideas" replaces cards with fresh suggestions. Cuisine selector: Surprise me, Indian, Mexican, Thai, Italian, Chinese, American, Mediterranean.
