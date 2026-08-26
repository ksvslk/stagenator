"""Reflector — the nightly learning LlmAgent.

Reads yesterday's ledger (actions + outcomes) and GA daily aggregates, then
rewrites the playbook and composes the owner's daily brief. This is the
"learns day by day" loop: tomorrow's Strategist reads what tonight's
Reflector concluded.
"""

import json

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from agent import config


class Reflection(BaseModel):
    playbook_json: str = Field(
        description="The COMPLETE updated playbook as a JSON object string — same schema as "
        "the current playbook you were shown (philosophy, knobs, segment_rules, "
        "capability_tiers, ceo_directives, evidence). Change only what the evidence supports."
    )
    changes_summary: str = Field(description="What changed in the playbook and why, 1-3 sentences")
    brief: str = Field(
        description="Daily brief for the owner, markdown, sections: What I did / What happened / "
        "What I changed / Needs you (only if something genuinely needs the owner, e.g. low Runpod balance)"
    )


reflector = LlmAgent(
    name="reflector",
    model=config.MODEL,
    instruction=(
        "You are the Reflector of Stagenator, an autonomous engagement agent for three "
        "small mobile games. Once per night you review the last 24h: every action taken, "
        "every outcome observed (claims, redemptions, session counts, retention movement), "
        "GA daily aggregates, and the current playbook.\n\n"
        "Update the playbook the way a thoughtful growth operator would:\n"
        "- With tiny user counts, evidence is weak — reason qualitatively, tag evidence "
        "weak/strong per rule in the evidence map, and do not overfit to noise.\n"
        "- Adjust knobs (send windows, cadence, inactivity thresholds) only with a stated reason.\n"
        "- Fold in unhandled CEO directives as playbook entries under ceo_directives.\n"
        "- Keep the playbook SHORT and operational — it is read by the Strategist every pulse.\n"
        "- The brief must be honest: if nothing happened, say so plainly."
    ),
    output_schema=Reflection,
    output_key="reflection",
)


PLAYBOOK_MAX_CHARS = 4000  # keep the Strategist's per-decision memory small


def apply_reflection(reflection: dict) -> dict:
    """Parse and persist the Reflector's output. Returns summary for the ledger."""
    from agent import state

    try:
        playbook = json.loads(reflection["playbook_json"])
    except (KeyError, json.JSONDecodeError) as e:
        state.critical(f"Reflector produced unparseable playbook: {e}")
        return {"applied": False, "error": str(e)}
    if not isinstance(playbook, dict):  # valid JSON but not an object (null/list/str)
        state.critical(f"Reflector playbook is not an object: {type(playbook).__name__}")
        return {"applied": False, "error": "playbook is not a JSON object"}

    # Preserve any sections the model omitted — merge over the current playbook so a
    # forgotten `knobs`/`philosophy` isn't silently dropped.
    merged = {**state.get_playbook(), **playbook}

    def _too_big() -> bool:
        return len(json.dumps(merged, default=str)) > PLAYBOOK_MAX_CHARS

    # Actually enforce the bound (the old setdefault trim was dead code): trim the
    # evidence log, then cap segment_rules, then drop least-critical sections until it fits.
    if _too_big():
        merged["evidence"] = {"note": "trimmed to bound memory size"}
    if _too_big() and isinstance(merged.get("segment_rules"), list):
        merged["segment_rules"] = merged["segment_rules"][:8]
    for _k in ("evidence", "segment_rules"):
        if not _too_big():
            break
        merged.pop(_k, None)
    playbook = merged
    state.update_playbook(playbook, reason=reflection.get("changes_summary", "nightly reflection"))
    state.db().collection(config.COL_BRIEFS).document().set(
        {"ts": state.now(), "brief": reflection.get("brief", ""), "changes": reflection.get("changes_summary", "")}
    )
    state.ledger("brief", None, brief=reflection.get("brief", "")[:2000])
    return {"applied": True, "changes": reflection.get("changes_summary", "")}
