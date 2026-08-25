"""Guardrails — hard caps enforced in code between the Strategist and the queue.

Contract (lifted from the long-horizon-harness recipe): a validator returns
None to allow, or a dict with "error" to block. Blocked actions are ledgered
as rejected, never retried blindly, never silently dropped.
"""

import datetime as dt

from agent import config, state

VALID_TYPES = {
    "ship_level",
    "send_code_drop",
    "send_individual_code",
    "activate_promo_banner",
    "send_level_push",
    "none",
}


def _count_recent(kind: str, game: str, action_types: set[str], hours: int) -> int:
    entries = state.recent_ledger(hours=hours, kind=kind)
    return sum(1 for e in entries if e.get("game") == game and e.get("action") in action_types)


def validate(action: dict) -> dict | None:
    """None = allowed; {"error": ...} = blocked."""
    t, game = action.get("type"), action.get("game")

    if t == "none":
        return {"error": "no-op action", "silent": True}
    if t not in VALID_TYPES:
        return {"error": f"unknown action type {t!r}"}
    if game not in config.GAMES:
        return {"error": f"unknown game {game!r}"}
    if game not in config.ACTIVE_GAMES:
        return {"error": f"{game} is not active"}

    if t in ("send_code_drop", "send_individual_code"):
        if not config.GAMES[game].get("fcm_token_collections"):
            return {"error": f"{game} can't guarantee per-user codes (no per-user FCM tokens)"}
        sent = _count_recent("action", game, {"code_drop", "individual_code"}, hours=24)
        n = action.get("n_codes") or 1
        if sent + n > config.CAPS["codes_per_game_per_day"]:
            return {"error": f"codes/day cap: {sent} sent + {n} requested > {config.CAPS['codes_per_game_per_day']}"}
        if n > 10:
            return {"error": f"drop size {n} > 10"}

    if t == "ship_level":
        shipped = _count_recent("action", game, {"level_pipeline"}, hours=24)
        if shipped >= config.CAPS["levels_per_game_per_day"]:
            return {"error": f"levels/day cap reached ({shipped})"}

    if t in ("send_code_drop", "send_individual_code", "send_level_push", "ship_level"):
        pushes = _count_recent("action", game,
                               {"code_drop", "individual_code", "level_pipeline", "level_push"}, hours=4)
        if pushes >= config.CAPS["push_actions_per_game_per_4h"]:
            return {"error": f"push-action/4h cap reached for {game}"}

    if t == "activate_promo_banner" and game != "palindrome":
        return {"error": "promo banner only exists for palindrome"}

    if t in ("send_code_drop", "send_individual_code", "send_level_push") and not config.GAMES[game]["level_push_topic"] and t != "send_code_drop":
        return {"error": f"{game} has no push channel"}

    return None


ACTION_TO_TASK = {
    "ship_level": "level_pipeline",
    "send_code_drop": "code_drop",
    "send_individual_code": "individual_code",
    "activate_promo_banner": "promo_banner",
    "send_level_push": "level_push",
}


def gate_and_enqueue(decision: dict) -> dict:
    """Validate each Strategist action; enqueue allowed ones; ledger rejects."""
    enqueued, rejected = [], []
    for action in decision.get("actions", []):
        verdict = validate(action)
        if verdict is None:
            not_before = state.now() + dt.timedelta(minutes=action.get("delay_minutes") or 0)
            task_id = state.enqueue(
                ACTION_TO_TASK[action["type"]],
                action["game"],
                {**action, "not_before": not_before.isoformat()},
            )
            if task_id:
                enqueued.append({"task": task_id, **action})
        elif not verdict.get("silent"):
            state.ledger("rejected", action.get("game"), action=action.get("type"),
                         reason=verdict["error"], raw=action)
            rejected.append({**action, "rejected": verdict["error"]})
    return {"enqueued": enqueued, "rejected": rejected, "notes": decision.get("notes", "")}
