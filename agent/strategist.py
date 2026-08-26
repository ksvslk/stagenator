"""Strategist — the decision LlmAgent.

Receives: detected signals + playbook + recent ledger + pending CEO directives.
Returns: a STRUCTURED list of actions (output_schema-enforced). It cannot invent
action types; guardrails.py validates every action against hard caps before
anything is enqueued.
"""

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from agent import config


class Action(BaseModel):
    type: str = Field(
        description="One of: ship_level | send_code_drop | send_individual_code | "
        "send_level_push | none"
    )
    game: str = Field(description=f"One of: {', '.join(config.ACTIVE_GAMES)}")
    reason: str = Field(description="One sentence: why this action, tied to a signal + playbook rule (internal, for the ledger)")
    message: str | None = Field(
        default=None,
        description="OPTIONAL user-facing push copy — short, warm, engaging (the notification "
        "body players see). Be creative and on-brand. The system always adds the truthful "
        "cue (scarcity for shared code drops, 'reserved for you' for personal ones), so focus "
        "on the hook. Omit to use a sensible default.")
    segment: str | None = Field(default=None, description="Target segment description, if applicable")
    platform: str | None = Field(default=None, description="android | ios | both, if applicable")
    n_codes: int | None = Field(default=None, description="Codes to back a drop with (small: 3-10)")
    culture: str | None = Field(
        default=None,
        description="ship_level OPTIONAL: a country/culture to SOFTLY nod to (e.g. a signal's "
        "`country` where active players concentrate). A hint, never a mandate — omit for a "
        "universal level; only set it on a clear geographic signal.")
    delay_minutes: int = Field(default=0, description="Delay before executing (0 = now); respect playbook send windows")
    directive_responses: list[str] = Field(default_factory=list, description="Answers to any CEO directives addressed")


class Decision(BaseModel):
    actions: list[Action] = Field(description="Actions to take now. Empty list is a valid, often correct, answer.")
    notes: str = Field(description="Brief reasoning summary for the ledger/dashboard")


strategist = LlmAgent(
    name="strategist",
    model=config.MODEL,
    instruction=(
        "You are the Strategist of Stagenator, an autonomous engagement & retention "
        "agent for small mobile games (Subliminal Words, AI Movie Quiz).\n\n"
        "You receive: detected signals (from Google Analytics realtime + code inventory), "
        "the current PLAYBOOK (your learned strategy — follow it), recent ledger entries "
        "(what was already done — never repeat an equivalent action), and any CEO "
        "directives (owner guidance — high priority, address them in directive_responses).\n\n"
        "Decide the minimal set of actions that best serves engagement and retention "
        "RIGHT NOW. Rules:\n"
        "- Few users: each one matters. But do not spam — one meaningful touch beats three pushes.\n"
        "- An active-user signal may carry `events_10min` and `top_events` — the ACTUAL gameplay "
        "events firing right now. Judge engagement from these, not from presence alone; do not "
        "claim knowledge you don't have (you cannot see a player's total playtime or history).\n"
        "- Follow the playbook's phase: in the EARLY GROWTH PHASE be GENEROUS with codes "
        "to hook players (active and returning), not stingy — a code can convert an early "
        "player. Fresh levels are free and always welcome. (Codes get scarcer as the base grows.)\n"
        "- You are given an `audience` breakdown per game (top countries, platform split, "
        "engagement time, lapsing counts) straight from analytics. Factor it into your "
        "decisions however you judge best — it is raw context, not a rule.\n"
        "- You MAY write the push `message` (user-facing copy) creatively; the system appends "
        "the honest cue (limited/first-come for drops, reserved-for-you for personal codes).\n"
        "- You are given `health` (which dependencies are up/down). Do NOT propose an "
        "action that needs a DOWN dependency — e.g. no ship_level if level generation is "
        "down, no code send if the push/claim path is down. Prefer an action whose "
        "dependencies are healthy, or none.\n"
        "- Respect playbook send windows via delay_minutes.\n"
        "- ship_level has an OPTIONAL `culture` lever — softly localize a level toward a "
        "signal's `country` when active players clearly concentrate somewhere; omit for a "
        "universal level (a great universal level beats a forced one). Puzzle difficulty "
        "auto-varies slightly on its own — you do not control it.\n"
        "- If nothing is worth doing, return an empty actions list. That is a good decision.\n"
        "- Never exceed: "
        f"{config.CAPS['codes_per_game_per_day']} codes/game/day, "
        f"{config.CAPS['levels_per_game_per_day']} levels/game/day, "
        f"1 push-action/game/4h. (Hard-enforced downstream regardless.)"
    ),
    output_schema=Decision,
    output_key="decision",
)
