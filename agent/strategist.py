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
        "activate_promo_banner | send_level_push | none"
    )
    game: str = Field(description=f"One of: {', '.join(config.ACTIVE_GAMES)}")
    reason: str = Field(description="One sentence: why this action, tied to a signal + playbook rule")
    segment: str | None = Field(default=None, description="Target segment description, if applicable")
    platform: str | None = Field(default=None, description="android | ios | both, if applicable")
    n_codes: int | None = Field(default=None, description="Codes to back a drop with (small: 3-10)")
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
        "- Respect playbook send windows via delay_minutes.\n"
        "- If nothing is worth doing, return an empty actions list. That is a good decision.\n"
        "- Never exceed: "
        f"{config.CAPS['codes_per_game_per_day']} codes/game/day, "
        f"{config.CAPS['levels_per_game_per_day']} levels/game/day, "
        f"1 push-action/game/4h. (Hard-enforced downstream regardless.)"
    ),
    output_schema=Decision,
    output_key="decision",
)
