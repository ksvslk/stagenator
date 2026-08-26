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
        "- Fresh levels are cheap and always welcome; codes are scarce — reserve them for "
        "returning/lapsing players per the playbook.\n"
        "- You are given an `audience` value profile (country value tier, engagement, lapsing "
        "counts per game). Spend scarce codes where they are worth most: higher-value "
        "(tier1) and lapsing segments over already-engaged or low-value ones. It is a "
        "prior, not a mandate.\n"
        "- You MAY write the push `message` (user-facing copy) creatively; the system appends "
        "the honest cue (limited/first-come for drops, reserved-for-you for personal codes).\n"
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
