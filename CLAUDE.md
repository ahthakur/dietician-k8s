# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A personal dietician web app — chat-first interface for food logging, macro tracking (calories/protein/fiber), meal suggestions with guided cooking, and photo analysis. Multi-user via Google OAuth. Uses Claude API for AI features (food estimation, structured recipe generation, fridge/receipt photo analysis). Also exposed to the Claude apps as a custom MCP connector at `/mcp` (OAuth sign-in), so users can log and coach from a "Dietician" Claude Project; plan and Project instructions in `docs/mcp-agent-plan.md`.

## Running the App

```bash
pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-... python server.py
```

Needs Python 3.10+ (the `mcp` package). The system `python3` on the dev Mac is 3.9; use `/opt/homebrew/bin/python3.14` (matches production) in a venv.

Server starts on `http://0.0.0.0:80` (override with `PORT` env var). No build step for the frontend — it's a single `static/index.html` file served directly by FastAPI.

Google OAuth requires `OAuth-client.json` in the project root (gitignored) or `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` env vars. `BASE_URL` defaults to `https://dietician.abhijitt.com`.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`tests/` (~35 tests, <1s) runs fully in-process: each test gets a fresh temp SQLite DB, and HTTP tests drive the real app (website routes, `/mcp`, OAuth) via Starlette's `TestClient`. No network, Google sign-in or API key. `conftest.py` sets env vars before importing `config` (`BASE_URL=http://localhost`, since the MCP SDK only allows a plain-http OAuth issuer on localhost), fakes website sign-in by signing a session cookie with the test `SESSION_SECRET`, and starts the app once per run (the MCP session manager can only start once per process). Not covered: Claude API calls in `meal_engine.py` and the frontend.

## Architecture

**Python backend (FastAPI + SQLite + Claude API), single-page HTML frontend.**

- `server.py` (~660 lines) — FastAPI app, all API routes inline (no routers). Pydantic models for request validation. `get_client_date(request)` helper reads `X-Timezone` header to compute the user's local date. `/api/today` and `/api/analysis` delegate to `tools.compute_day` / `tools.compute_history`. The MCP app is mounted at `/` *after* every other route, so website routes always win.
- `config.py` — Env vars, meal schedule, workout schedule (global defaults), social days, cuisine preference, `SYNC_TOKEN` for external API auth. User-specific targets (calories, protein, fiber, weight) live in the DB `users` table, not here.
- `meal_engine.py` (~300 lines) — Claude API integration. Calls `api.anthropic.com/v1/messages` directly via `httpx` (no SDK). Model is `claude-sonnet-4-5-20250929`. Core functions: `_call_claude()` for text, `_call_claude_vision()` for images, `generate_meal_ideas()` for structured recipe generation.
- `db.py` (~750 lines) — SQLite data layer. All tables created in `init_db()`. Each function opens its own connection via `get_conn()` (WAL mode). No connection pooling or shared connections.
- `auth.py` — Google OAuth via Authlib. Session-based auth; `require_auth(request)` extracts `user_id` (Google sub) from session and raises 401 if missing. The callback returns to `session["next"]` only if it's a `/mcp-auth/` path (used by the connector consent flow).
- `tools.py` (~360 lines) — Plain functions behind the 12 MCP tools (`get_today`, `get_progress`, `get_fridge`, `search_food_history`, `log_food`, `update_food`, `delete_food`, `log_workout`, `log_weight`, `set_day_type`, `add_fridge_items`, `remove_fridge_items`). Each takes `user_id` first; raises `ToolError` (a `ValueError`) for bad input. Also holds `get_user_targets`, `compute_day`, `compute_history` shared with the website routes.
- `mcp_server.py` (~220 lines) — MCP endpoint using the official `mcp` SDK (v2, `MCPServer`, stateless Streamable HTTP, JSON responses). Thin wrappers over `tools.py`; tool docstrings are what Claude reads to decide when to call each tool. `user_id` comes only from the verified token (`get_access_token().subject`). Transport security allows only the `BASE_URL` host (plus localhost).
- `mcp_auth.py` (~320 lines) — OAuth authorization server for the connector, plugged into the SDK via `auth_server_provider`. The SDK serves `/authorize`, `/token`, `/revoke` and both `.well-known` documents and does PKCE/redirect/secret/expiry checks; this module does storage plus the consent page at `/mcp-auth/consent` (Google sign-in → Allow/Cancel).
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
- `/mcp` — MCP endpoint (bearer token required). Served by the mounted MCP app, along with `/authorize`, `/token`, `/revoke`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`
- `/mcp-auth/consent` — GET consent page / POST approve or deny (registered in `server.py` from `mcp_auth.py`)

## Deployment

Production deploys via SCP to a GCP VM named `dietician` in `us-central1-f`, then `sudo systemctl restart dietician`. Python 3.14, venv at `~/dietician/venv`. Static file changes (index.html) don't need a restart but must be SCP'd to `~/dietician/static/index.html` (not just `~/dietician/`). The repo also contains a Helm chart (`Chart.yaml`, `values.yaml`, `templates/`) but Kubernetes is not used for production.

- **Traffic path:** Cloudflare (TLS on 443) → Caddy on `:80` (`/etc/caddy/Caddyfile`, routes by host) → app on `:8080` (`PORT=8080`). The Host header passes through, which `/mcp` transport security relies on.
- **Env vars** live in the systemd unit (`ANTHROPIC_API_KEY`, `SESSION_SECRET`, `BASE_URL`, `PORT`, `SYNC_TOKEN`). MCP client credentials (`MCP_CLIENT_ID`, `MCP_CLIENT_SECRET`) are in a root-only drop-in, `/etc/systemd/system/dietician.service.d/mcp.conf`; `sudo systemctl daemon-reload` after editing either.
- **New dependencies:** when `requirements.txt` changes, run `~/dietician/venv/bin/pip install -r requirements.txt` on the VM before restarting.
- **Debugging the connector:** `sudo journalctl -u dietician` — a working sign-in shows `/authorize` → `/mcp-auth/consent` → "MCP connector approved for user …" → `POST /token` 200 → `POST /mcp` 200s.

## Key Patterns

- **Ownership checks**: every by-ID write is scoped in SQL (`WHERE id = ? AND user_id = ?`, or the resolved fridge ID for fridge items) and returns whether a row changed; routes turn `False` into 404. Keep new by-ID functions scoped the same way.
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
- **MCP connector auth**: one pre-registered OAuth client (no dynamic registration), locked to Claude's callback `https://claude.ai/api/mcp/auth_callback` (override with `MCP_REDIRECT_URIS`). Codes and tokens are stored only as SHA-256 hashes (`mcp_auth_requests`, `mcp_auth_codes`, `mcp_tokens`). Access tokens last 1h; refresh tokens last 60 days and rotate on use, and reusing a rotated refresh token revokes the whole grant. `token_auth_method_shim` accepts both `client_secret_basic` and `client_secret_post`. `MCP_DEV_TOKEN` + `MCP_DEV_USER` give a static token for local testing only; never set them in production.
- **MCP tool dates**: MCP calls carry no `X-Timezone` header, so tools use `tools.get_user_today(user_id)` from the stored `users.timezone` (updated whenever the user opens the website). Entries logged through the connector have `source` `mcp_text` or `mcp_photo`.
- **MCP tool results**: tools return dicts that reach Claude as JSON text content. Writes return the updated day (totals plus `remaining`) so Claude can confirm without a second call. Raise `tools.ToolError` for anticipated bad input; `mcp_server._call` turns it into a readable `is_error` result, while any other exception shows Claude only a generic error. `log_food` takes a free-text `portion` (appended to the description) because `food_log.quantity` is a display multiplier that totals ignore; for the same reason `update_food` has no quantity input.
