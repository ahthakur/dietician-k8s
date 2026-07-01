# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A personal dietician web app — chat-first interface for food logging, macro tracking (calories/protein/fiber), meal suggestions, and photo analysis. Multi-user via Google OAuth. Uses Claude API for AI features (food estimation, recipe suggestions, fridge/receipt photo analysis).

## Running the App

```bash
pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-... python server.py
```

Server starts on `http://0.0.0.0:80` (override with `PORT` env var). No build step for the frontend — it's a single `static/index.html` file served directly by FastAPI.

Google OAuth requires `OAuth-client.json` in the project root (gitignored) or `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` env vars. `BASE_URL` defaults to `https://dietician.abhijitt.com`.

## Architecture

**Python backend (FastAPI + SQLite + Claude API), single-page HTML frontend.**

- `server.py` — FastAPI app, all API routes, lifespan (DB init). Routes are defined inline, not split into routers. Includes `get_client_date(request)` helper that reads `X-Timezone` header to compute the user's local date.
- `config.py` — Env vars, meal schedule, workout schedule (global defaults), social days, cuisine preference. User-specific targets (calories, protein, fiber) live in the DB `users` table, not here.
- `meal_engine.py` — Claude API integration. Calls `api.anthropic.com/v1/messages` directly via `httpx` (no SDK). Handles chat intent classification (log food / suggest meals / general chat) and vision endpoints (food photo, fridge photo, receipt scan). Model is set to `claude-sonnet-4-5-20250929`.
- `db.py` — SQLite data layer. All tables created in `init_db()`. Each function opens its own connection via `get_conn()` (WAL mode). Tables: `food_log`, `fridge_items`, `users`, `chat_history`, `quick_log_history`, `purchase_log`, `grocery_lists`, `reminder_log`, `meal_history`, `weekly_protein_log`.
- `auth.py` — Google OAuth via Authlib. Session-based auth; `require_auth(request)` extracts `user_id` (Google sub) from session and raises 401 if missing.
- `static/index.html` — Complete SPA in a single HTML file (inline CSS + JS). Glassmorphic dark UI, mobile-first. Calls `/api/*` endpoints. Sends `X-Timezone` header on every request.

## Kubernetes / Helm

The repo doubles as a Helm chart (`Chart.yaml`, `values.yaml`, `templates/`). Deploys to Kubernetes with a PVC for SQLite persistence. Local dev uses `image.pullPolicy: Never` with a local Docker image.

## Key Patterns

- User identification: Google OAuth `sub` claim stored in session as `user_id`. Per-user nutrition targets stored in DB `users` table (calories_min, calories_max, protein_target, fiber_target, timezone).
- Timezone handling: Frontend sends IANA timezone via `X-Timezone` header. Server's `get_client_date(request)` uses it to compute the correct local date. User's timezone is persisted in DB for future use.
- Claude API calls go through `_call_claude()` and `_call_claude_vision()` in `meal_engine.py` — raw HTTP, not the Anthropic SDK.
- Chat responses from Claude are expected as raw JSON (no markdown fences). The engine strips fences as a fallback and parses. Three response types: `log` (food entry), `suggest` (meal ideas), `chat` (general).
- Photo scan flow: Backend analyzes but does NOT auto-save. Frontend shows a review modal for editing before confirming. Applies to food photos, fridge photos, and receipt scans.
- Fridge items are categorized (produce, protein, dairy, pantry, frozen, beverages, other) and editable.
- No test suite exists.
