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
    reason: str = Field(
        description="One sentence: why this action, tied to a signal + playbook rule (internal, for the ledger)"
    )
    message: str | None = Field(
        default=None,
        description="OPTIONAL user-facing push copy — short, warm, engaging (the notification "
        "body players see). Be creative and on-brand. The system always adds the truthful "
        "cue (scarcity for shared code drops, 'reserved for you' for personal ones), so focus "
        "on the hook. Omit to use a sensible default.",
    )
    gift_game: str | None = Field(
        default=None,
        description="CROSS-PROMO: send a code for THIS other game to the audience game's "
        f"players (one of {', '.join(config.ACTIVE_GAMES)}). Omit for a normal same-game code. "
        "Use to pull an engaged player of one game into the other.",
    )
    n_codes: int | None = Field(
        default=None, description="Codes to back a drop with (small: 3-10)"
    )
    message_alt: str | None = Field(
        default=None,
        description="OPTIONAL second push-copy variant for the built-in A/B experiment: a "
        "DIFFERENT hook/tone than `message`. When set, the system alternates the two "
        "variants across recipients/sends and measures which one gets codes claimed. "
        "Provide it whenever you write a `message`.")
    culture: str | None = Field(
        default=None,
        description="ship_level OPTIONAL: a country/culture to SOFTLY nod to (e.g. where the "
        "signal's breakdown shows active players concentrating). A creative hint for level "
        "generation only — never targeting or limiting. Omit for a universal level.",
    )
    delay_minutes: int = Field(
        default=0,
        description="Delay before executing (0 = now); respect playbook send windows",
    )
    directive_responses: dict[str, str] = Field(
        default_factory=dict,
        description="Answers to CEO directives you are addressing, KEYED BY the directive's id "
        '(from the \'directives\' context): {"<directive_id>": "your one-line answer"}. Only '
        "include directives you actually address here — others stay open, never blanket-resolved.",
    )


class Decision(BaseModel):
    actions: list[Action] = Field(
        description="Actions to take now. Empty list is a valid, often correct, answer."
    )
    notes: str = Field(
        description="One or two plain sentences for the owner's dashboard: WHAT you are doing "
        "and WHY, tied to the signal (e.g. 'New iOS player in the US — shipping a welcome "
        "level'). Write for a human skimming a feed, not a log parser."
    )
    ruled_out: list[str] = Field(
        default_factory=list,
        description="The WHY-NOT: options you considered but deliberately did not take, each "
        "as one short line with its reason — a cap ('code drop — 4h push cap used by the "
        "level'), a missing precondition ('AMQ: no players online'), or judgment ('second "
        "push today would feel spammy'). Empty only when nothing else was plausible.",
    )


strategist = LlmAgent(
    name="strategist",
    model=config.MODEL,
    instruction=(
        "You are the Strategist of Stagenator, an autonomous engagement & retention "
        "agent for small mobile games (Subliminal Words, AI Movie Quiz).\n\n"
        "You receive: detected signals (from Google Analytics realtime + code inventory), "
        "the current PLAYBOOK (your learned strategy — follow it), recent ledger entries "
        "(what was already done — never repeat an equivalent action), and any CEO "
        "directives (owner guidance — high priority; when you address one, put "
        "{its id: your answer} in that action's directive_responses — only the ones you answer).\n\n"
        "Decide the minimal set of actions that best serves engagement and retention "
        "RIGHT NOW. Rules:\n"
        "- Few users: each one matters. But do not spam — one meaningful touch beats three pushes.\n"
        "- An active-user signal may carry `events_10min` and `top_events` — the ACTUAL gameplay "
        "events firing right now. Judge engagement from these, not from presence alone; do not "
        "claim knowledge you don't have (you cannot see a player's total playtime or history).\n"
        "- At this small scale do NOT gate actions on deep engagement analysis — any active "
        "or returning player is worth the day's one code (cap: 1) and one level. Act readily. "
        "With a handful of users, just send it.\n"
        "- You are given an `audience` breakdown per game (top countries, platform split, "
        "engagement time, lapsing counts) plus each signal's live platform:country counts. "
        "Raw context for your judgment and copy — never a limit or a targeting rule.\n"
        "- You MAY write the push `message` (user-facing copy) creatively — and a second "
        "variant in `message_alt` with a different hook: the system A/B tests the two and "
        "the nightly review learns which style gets codes claimed. The system appends "
        "the honest cue (limited/first-come for drops, reserved-for-you for personal codes).\n"
        "- You are given `health` (which dependencies are up/down). Do NOT propose an "
        "action that needs a DOWN dependency — e.g. no ship_level if level generation is "
        "down, no code send if the push/claim path is down. Prefer an action whose "
        "dependencies are healthy, or none.\n"
        "- Respect playbook send windows via delay_minutes.\n"
        "- ship_level has an OPTIONAL `culture` lever — softly localize a level when the "
        "breakdown shows players clearly concentrating somewhere; omit for a universal level "
        "(a great universal level beats a forced one). Puzzle difficulty auto-varies "
        "slightly on its own — you do not control it.\n"
        "- If nothing is worth doing, return an empty actions list. That is a good decision.\n"
        "- Never exceed: "
        f"{config.CAPS['codes_per_game_per_day']} codes/game/day, "
        f"{config.CAPS['levels_per_game_per_day']} levels/game/day, "
        f"{config.CAPS['push_actions_per_game_per_4h']} push-actions/game/4h. "
        "(Hard-enforced downstream regardless.)"
    ),
    output_schema=Decision,
    output_key="decision",
)
