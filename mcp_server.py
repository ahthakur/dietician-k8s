"""MCP endpoint for the dietician pal (phase 3 of docs/mcp-agent-plan.md).

Exposes the 12 functions in tools.py over Streamable HTTP at /mcp. The caller's
user_id comes only from the verified bearer token (AccessToken.subject), never from
tool arguments.

Auth status: the SDK acts as the OAuth *resource server* here. It serves
/.well-known/oauth-protected-resource/mcp and answers unauthenticated requests
with 401 + WWW-Authenticate. Issuing tokens (the authorization server, signing in
with Google) is phase 4. Until then the only accepted token is an optional dev
token from the environment, for local testing:

    MCP_DEV_TOKEN=<long random string> MCP_DEV_USER=<google sub>

With neither set, every request is rejected.
"""

import hmac
import logging
import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import tools
from config import BASE_URL

logger = logging.getLogger("dietician.mcp")

MCP_PATH = "/mcp"
RESOURCE_URL = f"{BASE_URL.rstrip('/')}{MCP_PATH}"

INSTRUCTIONS = """Dietician tools for one person's food diary, macro targets (calories,
protein, fiber), workouts, weight and shared household fridge. Never guess the user's
numbers: fetch them. Calories, protein and fiber you pass in are your own estimates;
the server stores them as given. Dates are YYYY-MM-DD and default to today in the
user's timezone."""


# ── Auth ─────────────────────────────────────────

class DieticianTokenVerifier:
    """Phase 3: accepts only the env dev token. Phase 4 replaces this with issued tokens."""

    async def verify_token(self, token: str) -> AccessToken | None:
        dev_token = os.environ.get("MCP_DEV_TOKEN", "")
        dev_user = os.environ.get("MCP_DEV_USER", "")
        if len(dev_token) >= 32 and dev_user and hmac.compare_digest(token, dev_token):
            return AccessToken(token=token, client_id="dev", scopes=["dietician"],
                               resource=RESOURCE_URL, subject=dev_user)
        return None


def _user_id() -> str:
    tok = get_access_token()
    if not tok or not tok.subject:
        # The auth middleware should make this unreachable.
        raise MCPToolError("Not authenticated.")
    return tok.subject


def _call(fn, *args, **kwargs):
    try:
        return fn(_user_id(), *args, **kwargs)
    except tools.ToolError as e:
        raise MCPToolError(str(e)) from e


# ── Server ───────────────────────────────────────

_host = urlparse(BASE_URL).netloc

mcp = MCPServer(
    "Dietician",
    instructions=INSTRUCTIONS,
    token_verifier=DieticianTokenVerifier(),
    auth=AuthSettings(
        # Phase 4 hosts the authorization server on this same origin.
        issuer_url=BASE_URL,
        resource_server_url=RESOURCE_URL,
        required_scopes=["dietician"],
        validate_token_resource=True,
    ),
)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

DateArg = str | None


class FoodItem(BaseModel):
    description: str = Field(description="What was eaten, e.g. 'masala chai' or 'dal with rice'.")
    portion: str = Field("", description="Free-text portion, e.g. '1 cup', '2 rotis'. Optional.")
    calories: float = Field(ge=0, le=tools.MAX_ITEM_CALORIES, description="Estimated kcal for this portion.")
    protein: float = Field(0, ge=0, le=tools.MAX_ITEM_PROTEIN, description="Grams of protein.")
    fiber: float = Field(0, ge=0, le=tools.MAX_ITEM_FIBER, description="Grams of fiber.")


class FridgeItem(BaseModel):
    name: str = Field(description="Item name, e.g. 'paneer'.")
    quantity: str = Field("", description="Free text, e.g. '200g', '1 bunch'.")
    category: str = Field("other", description="e.g. dairy, produce, protein, grains, condiments, other.")


# Reading data

@mcp.tool(annotations=READ)
def get_today(date: DateArg = None) -> dict:
    """Get the user's food diary for a day: totals, targets (calories include today's
    workout burn), what's REMAINING, each entry with its id, the workout, day type
    (normal/rest/vacation) and weight. Call before suggesting food, before answering
    "how am I doing today", and to find entry ids for corrections."""
    return _call(tools.get_today, date)


@mcp.tool(annotations=READ)
def get_progress(days: Literal[7, 14, 30] = 7) -> dict:
    """Get multi-day progress: per-day calories, protein, fiber, deficit vs TDEE+workout,
    and drinks, plus a summary (averages, total deficit, estimated weight change at
    3500 kcal/lb) and weight trend vs goal. Vacation days are excluded from the summary.
    Use when the user asks how they're doing over time or about weight loss."""
    return _call(tools.get_progress, days)


@mcp.tool(annotations=READ)
def get_fridge() -> dict:
    """List what's in the household fridge (shared with household members). Call before
    suggesting meals or recipes."""
    return _call(tools.get_fridge)


@mcp.tool(annotations=READ)
def search_food_history(query: str) -> dict:
    """Find foods logged before and the calories/protein/fiber used then. Call before
    logging a usual or repeated item ("my usual chai", "same breakfast as always") and
    reuse those numbers for consistency."""
    return _call(tools.search_food_history, query)


# Logging food

@mcp.tool(annotations=WRITE)
def log_food(items: list[FoodItem], date: DateArg = None, source: Literal["text", "photo"] = "text") -> dict:
    """Log food or drink the user actually had. Use whenever they mention eating or
    drinking something, even casually ("had a coffee"). Do NOT use for foods they are only
    considering. One call per meal: pass every item on the plate. For photos, show your
    estimate and get confirmation first, then log with source="photo". Returns the
    updated day with remaining macros."""
    return _call(tools.log_food, [i.model_dump() for i in items], date, source)


@mcp.tool(annotations=WRITE)
def update_food(entry_id: int, description: str | None = None, calories: float | None = None,
                protein: float | None = None, fiber: float | None = None) -> dict:
    """Correct a logged entry ("the rice was only half", "that was 2 rotis not 3"). Get
    entry_id from get_today. Pass only the fields that change; for a portion change, pass
    the new scaled calories/protein/fiber. Returns the updated day."""
    return _call(tools.update_food, entry_id, description=description, calories=calories,
                 protein=protein, fiber=fiber)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_food(entry_id: int) -> dict:
    """Remove a logged entry the user didn't eat or logged by mistake. Get entry_id from
    get_today. Returns the updated day."""
    return _call(tools.delete_food, entry_id)


# Body and workouts

@mcp.tool(annotations=WRITE)
def log_workout(label: str, duration_min: int, calories_burned: int, date: DateArg = None) -> dict:
    """Record a workout the user tells you about. Workouts normally sync from Apple
    Health, so use this only for one that's missing. Replaces that day's workout. Burned
    calories raise the day's calorie target."""
    return _call(tools.log_workout, label, duration_min, calories_burned, date)


@mcp.tool(annotations=WRITE)
def log_weight(weight: float, date: DateArg = None) -> dict:
    """Record the user's body weight (in their usual unit, normally lbs) when they mention
    it. Replaces any weight already logged for that day."""
    return _call(tools.log_weight, weight, date)


@mcp.tool(annotations=WRITE)
def set_day_type(type: Literal["normal", "rest", "vacation"], date: DateArg = None) -> dict:
    """Mark a day as a rest day (no workout), vacation (excluded from progress metrics),
    or back to normal. Use when the user says "rest day today", "I'm on vacation", or
    undoes one of those."""
    return _call(tools.set_day_type, date, type)


# Fridge

@mcp.tool(annotations=WRITE)
def add_fridge_items(items: list[FridgeItem]) -> dict:
    """Add groceries to the shared fridge, e.g. after the user shares a fridge photo,
    a receipt, or says what they bought. Existing items get their quantity updated."""
    return _call(tools.add_fridge_items, [i.model_dump() for i in items])


@mcp.tool(annotations=DESTRUCTIVE)
def remove_fridge_items(names: list[str]) -> dict:
    """Remove items the household has used up ("we finished the paneer"). Names match
    case-insensitively; unmatched names are reported back."""
    return _call(tools.remove_fridge_items, names)


# ── ASGI app ─────────────────────────────────────

def build_app():
    """The Starlette app serving /mcp and its protected-resource metadata."""
    return mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[_host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"https://{_host}", "https://claude.ai", "http://127.0.0.1:*", "http://localhost:*"],
        ),
    )
