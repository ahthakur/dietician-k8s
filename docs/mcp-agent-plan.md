# Plan: Dietician Pal via Claude (MCP Connector)

**Status:** Planning only. Nothing built yet.
**Drafted:** 2026-10-02

## Why

The web app works, but we rarely open it. The friction is getting to it, not what it does.
Both of us are on iPhone, both have Claude subscriptions, and we already open the Claude app
every day. The plan is to make Claude itself the way we use the app: chat, voice, and
"snap a plate" all happen in the Claude app, and our server keeps the data.

**Claude handles:** the conversation, reading food photos, estimating calories, suggesting meals, coaching.
**Our server handles:** an accurate food log, personal targets, workout calories from Apple
Health, the shared fridge, the deficit math, weight history, and the dashboard.

Claude can't do the server's part on its own. Its memory keeps loose summaries, not a ledger,
and it has no access to the Apple Health data our server already receives.

## Decisions made

| Question | Decision | Reason |
|---|---|---|
| Native iOS app? | **No** (for now) | Most work. Try the Claude app first. |
| Messaging bot (iMessage / Telegram)? | **No** | iMessage has no bot API, and we don't need proactive messages. |
| Should the pal reach out first? | **Not needed** | Too many notifications already. They'd get lost. |
| Where we chat | **Claude iOS app**, inside a "Dietician" Project with the connector on | |
| Website | **Keep it** for the dashboard, charts, cook mode, settings and fridge sharing | Chat is bad for showing trends. |
| Photo analysis | **Claude in the app reads the photo**, then calls `log_food` with the items | The image never reaches our server. That's fine. |
| Who estimates calories | **Claude**, which passes the numbers in. The server just stores them. | Simpler, uses context from the conversation, and avoids paying for a second Claude call. |
| Confirm before logging? | **Photos: confirm first. Typed or spoken: log right away.** | Photo estimates are the least reliable. |

## Architecture

```
Claude iOS app (Project "Dietician", connector enabled)
        │  MCP over HTTPS + OAuth (each person signs in with their own Google account)
        ▼
FastAPI server.py  ──  new /mcp endpoint (official Python MCP SDK)
        │
        ▼
db.py (existing functions)  ──  SQLite
```

- No new service. It's mounted on the existing FastAPI app and deployed to the same VM the usual way.
- The tools call `db.py` directly, not the HTTP routes.
- **Auth:** Claude's remote connectors use OAuth. The MCP endpoint needs an OAuth flow that
  ends with a token tied to the Google `sub` (the same `user_id` used today). Check the current
  MCP auth spec and Claude connector requirements when building. This is likely the biggest
  piece of work.

## Tool list (12 tools)

### Reading data

| Tool | Inputs | Returns | Built on |
|---|---|---|---|
| `get_today` | `date?` | Totals, targets (`calories_adjusted` includes workout), **remaining**, entries with IDs, workout, rest/vacation status, today's and latest weight | logic in `/api/today` (`server.py` `get_today`) |
| `get_progress` | `days` (7/14/30) | Per day: calories, protein, deficit, drinks. Summary: average deficit, estimated lbs (3500 cal = 1 lb), weight trend. Vacation days excluded | logic in `/api/analysis` |
| `get_fridge` | none | Fridge items (the shared group's fridge if in one) | `get_fridge_items(get_fridge_id(user_id))` |
| `search_food_history` | `query` | Past foods with the macros used before, so the same food gets the same numbers ("my usual chai") | `search_quick_logs` |

### Logging food

| Tool | Inputs | Notes |
|---|---|---|
| `log_food` | `items: [{description, quantity, calories, protein, fiber}]`, `date?`, `source: photo\|text` | Takes a list, so one plate becomes one call. Also calls `save_quick_log`. Returns the updated day |
| `update_food` | `entry_id`, any of `description/quantity/calories/protein/fiber` | Corrections |
| `delete_food` | `entry_id` | |

### Body and workouts

| Tool | Inputs | Notes |
|---|---|---|
| `log_workout` | `label`, `duration_min`, `calories_burned`, `date?` | `set_workout` replaces the day's entry. Apple Health sync may overwrite it later. Fine. |
| `log_weight` | `weight`, `date?` | `log_weight` (replaces the day's entry) |
| `set_day_type` | `date`, `type: normal\|rest\|vacation` | Combines the rest-day toggle (`delete_workout`) and vacation (`set_vacation`/`unset_vacation`) |

### Fridge

| Tool | Inputs | Notes |
|---|---|---|
| `add_fridge_items` | `items: [{name, quantity, category}]` | Used for fridge and receipt photos. `add_fridge_items_bulk` |
| `remove_fridge_items` | `names: []` | "We finished the paneer." `remove_fridge_item` |

### Left out on purpose

- **Meal suggestions, photo analysis, `/api/estimate`.** Claude in the app does these itself by calling
  `get_today` and `get_fridge`. Having the server call Claude for Claude only costs API money.
- **Changing targets and profile.** It's rare, and a stray chat message shouldn't change goals. Do it on the website.
- **Fridge share/join/leave, clear the fridge.** One-time or destructive. Do it on the website.

### Rules that apply to every tool

1. **`user_id` comes from the OAuth token only.** It's never a tool input.
2. **Dates default to the user's stored timezone** (`users.timezone`). Calls from Claude don't carry
   the `X-Timezone` header the website sends, so add a helper like `get_user_today(user_id)`.
3. **Every write returns the updated day** (totals plus what's remaining), so Claude can confirm without a second call.
4. **Write clear tool descriptions that say *when* to use the tool.** Claude picks tools based on them. Example:
   > `log_food`: Log anything the user ate or drank to their food diary. Use whenever the
   > user mentions eating or drinking something, even casually ("had a coffee"). Do not use for
   > foods they are only considering. Returns updated totals for the day.

## Fix before building: missing ownership checks

These `db.py` functions act on an ID with **no `user_id` check**. Today, any signed-in user can
edit or delete another user's rows by calling the API directly:

- `delete_food_log_entry(entry_id)`
- `update_food_log_entry(entry_id, ...)`
- `update_fridge_item(item_id, ...)`
- `remove_fridge_item_by_id(item_id)` (scope this by fridge ID, since fridges can be shared)

Add `AND user_id = ?` (or the fridge ID for fridge items) and pass it in from the routes. The MCP tools
must use the scoped versions.

Also note: `quick_log_history` has **no `user_id` column**, so `search_food_history` would return
both people's foods. That's probably fine for one household. Make it per-user if it ever matters.

## Project instructions (draft)

Set up a Claude Project called **"Dietician"** for each of us, turn the connector on, and paste in:

```
You're our dietician pal: warm, brief, practical, and a little encouraging. Never preachy.
The goal is to stay in a steady calorie deficit while hitting protein and fiber targets.

DATA RULES
- Never guess our numbers (totals, targets, deficit, weight, fridge). Always fetch them with
  the dietician tools first.
- Before suggesting what to eat, call get_today (what's left) and get_fridge (what we have).

LOGGING
- When I mention food or drink I actually had, log it with log_food before replying.
  Don't log foods I'm only thinking about ("should I have…").
- For a usual or repeated item ("my usual chai"), call search_food_history first and reuse
  those numbers.
- Food PHOTOS: list each item with portion, calories and protein, then ask "Log it?" Log only
  after I confirm or correct.
- Typed or spoken meals: log right away, then show what you logged so I can correct it.
- Corrections ("the rice was half", "remove the salad"): use update_food / delete_food.
- After logging, reply in one or two lines: what was logged, today's total, and calories and
  protein remaining.

ESTIMATING
- Assume home-style Indian portions unless told otherwise. A plate of rice is about 1.5 cups;
  a roti is about 100 cal (more if ghee is mentioned).
- When unsure, estimate a little high rather than low.
- Ask about a portion only if it's truly ambiguous and the difference would be big.

WORKOUTS AND BODY
- Workouts usually sync from Apple Health. Use log_workout only if I tell you about one that's
  missing.
- Log weight with log_weight when I mention it.
- "Rest day" or "on vacation" → set_day_type.

COACHING
- When I ask how I'm doing, use get_progress and give: average deficit, estimated weight
  change, one thing going well, and one small thing to adjust.
- For meal ideas, prefer what's in the fridge, high protein, and simple. Give 2–3 options with
  rough macros and offer full steps if I want them.
- Keep it short. No lectures, no disclaimers unless something is actually unsafe.
```

The estimating rules above should match what the current prompts in `meal_engine.py` say.
Copy over any rules there that we rely on.

## Build phases

1. **Ownership fixes** in `db.py` and the routes (small, and worth doing anyway).
2. **Tool layer:** plain Python functions (`tools.py`?) wrapping `db.py`, each taking `user_id`.
   They can be tested without MCP.
3. **MCP endpoint:** mount `/mcp` on FastAPI with the Python MCP SDK and register the 12 tools.
4. **OAuth for MCP:** link to the existing Google sign-in so the token gives us `user_id`.
5. **Connect:** add it as a custom connector in Claude, create the Project, paste the instructions.
6. **Try it for 2 weeks.** Watch for: meals not logged (connector off or outside the Project), bad photo
   estimates, wrong dates around midnight, tool descriptions Claude misreads.
7. **Optional later:** point the website's own `/api/chat` at the same tool layer, so both places behave the same.

## Open questions for later

- Does the Claude iOS app let us start a chat directly in a Project from a shortcut or widget? If so,
  that removes the last bit of friction.
- Should `log_food` keep a `meal` field (breakfast/lunch/dinner/snack)? The current schema doesn't have one.
- Should the server sanity-check the numbers Claude sends (e.g. reject 5000 cal for "a coffee")?
- Before testing, skim `meal_engine.py` prompts and decide which estimation rules to move into the Project instructions.
