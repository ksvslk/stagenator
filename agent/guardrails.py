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
    "send_level_push",
    "none",
}


def _count_recent(kind: str, game: str, action_types: set[str], hours: int) -> int:
    """Count COMPLETED actions only (status=done). An enqueued-but-failed attempt must
    not consume the cap — the player got nothing, so the budget is still available and
    the agent can retry. Double-enqueue of the same intent is already prevented by the
    per-(type,game) idempotency key, not by this count."""
    entries = state.recent_ledger(hours=hours, kind=kind)
    return sum(
        1
        for e in entries
        if e.get("game") == game
        and e.get("action") in action_types
        and e.get("status") == "done"
    )


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

    gift = action.get("gift_game")
    if gift and gift not in config.ACTIVE_GAMES:
        return {"error": f"gift_game {gift!r} is not an active game"}

    if t in ("send_code_drop", "send_individual_code"):
        if not config.GAMES[game].get("codes_enabled", True):
            return {"error": f"{game} has no promo-code campaign configured"}
        acts = _count_recent("action", game, {"code_drop", "individual_code"}, hours=24)
        if acts >= config.CAPS["code_actions_per_game_per_day"]:
            return {"error": f"code-notification/day cap reached for {game} (1/day)"}
        n = action.get("n_codes") or 1
        if n > 10:
            return {"error": f"drop size {n} > 10"}

    if t == "ship_level":
        shipped = _count_recent("action", game, {"level_pipeline"}, hours=24)
        if shipped >= config.CAPS["levels_per_game_per_day"]:
            return {"error": f"levels/day cap reached ({shipped})"}

    if t in ("send_code_drop", "send_individual_code", "send_level_push", "ship_level"):
        pushes = _count_recent(
            "action",
            game,
            {"code_drop", "individual_code", "level_pipeline", "level_push"},
            hours=4,
        )
        if pushes >= config.CAPS["push_actions_per_game_per_4h"]:
            return {"error": f"push-action/4h cap reached for {game}"}

    # A level push needs the game's level topic; code paths use FCM tokens / drops, not it.
    if t == "send_level_push" and not config.GAMES[game].get("level_push_topic"):
        return {"error": f"{game} has no level push topic"}

    return None


def doors_open(game: str) -> bool:
    """Deterministic mirror of validate(): could ANY action for this game pass the
    gate right now? Used to skip the Strategist entirely on pulses where every
    door is shut — waking the model to conclude "caps reached" wastes a call and
    fills the feed with no-op decisions. Mirrors the checks below; keep in sync."""
    pushes = _count_recent(
        "action",
        game,
        {"code_drop", "individual_code", "level_pipeline", "level_push"},
        hours=4,
    )
    if pushes >= config.CAPS["push_actions_per_game_per_4h"]:
        return False
    if (
        _count_recent("action", game, {"level_pipeline"}, hours=24)
        < config.CAPS["levels_per_game_per_day"]
    ):
        return True
    if (
        config.GAMES[game].get("codes_enabled", True)  # mirrors validate()'s gate
        and _count_recent("action", game, {"code_drop", "individual_code"}, hours=24)
        < config.CAPS["code_actions_per_game_per_day"]
    ):
        return True
    # re-announce door: one level_push per game per day (task idempotency key)
    if (
        config.GAMES[game].get("level_push_topic")
        and _count_recent("action", game, {"level_push"}, hours=24) < 1
    ):
        return True
    return False


ACTION_TO_TASK = {
    "ship_level": "level_pipeline",
    "send_code_drop": "code_drop",
    "send_individual_code": "individual_code",
    "send_level_push": "level_push",
}


def gate_and_enqueue(decision: dict) -> dict:
    """Validate each Strategist action; enqueue allowed ones; ledger rejects."""
    enqueued, rejected = [], []
    for action in decision.get("actions", []):
        verdict = validate(action)
        if verdict is None:
            # clamp: never backdate (negative) or park a task beyond ~12h
            _delay = max(0, min(int(action.get("delay_minutes") or 0), 720))
            not_before = state.now() + dt.timedelta(minutes=_delay)
            task_id = state.enqueue(
                ACTION_TO_TASK[action["type"]],
                action["game"],
                {**action, "not_before": not_before.isoformat()},
            )
            if task_id:
                enqueued.append({"task": task_id, **action})
        elif not verdict.get("silent"):
            state.ledger(
                "rejected",
                action.get("game"),
                action=action.get("type"),
                reason=verdict["error"],
                raw=action,
            )
            rejected.append({**action, "rejected": verdict["error"]})
    return {
        "enqueued": enqueued,
        "rejected": rejected,
        "notes": decision.get("notes", ""),
    }
