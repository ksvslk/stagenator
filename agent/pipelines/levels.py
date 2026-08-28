"""Level pipelines — generate, validate (schema+dedup+QA), submit via Admin SDK.

Per-game submission replicates the admin dashboards' exact write paths:
- subliminal-words: Storage packs/{pack}/levels/{id}/... + level doc + counters
- ai-movie-quiz:    Storage video + levels/{n} + counters/levelsCounter
"""

from agent import config


def run(task: dict) -> dict:
    game = task["game"]
    if game == "subliminal-words":
        return _subliminal_generate_and_submit(task)
    if game == "ai-movie-quiz":
        return _moviequiz_generate_and_submit(task)
    if game == "palindrome":
        return _palindrome_generate_and_submit(task)
    raise ValueError(f"no level pipeline for {game}")


def push_only(task: dict) -> dict:
    """Re-announce an existing recent level (no new content)."""
    from agent.tools import fcm

    game = task["game"]
    topic = config.GAMES[game]["level_push_topic"]
    if not topic:
        raise RuntimeError(f"{game} has no push channel")
    if config.DRY_RUN:
        return {"dry_run": True, "topic": topic}
    return {
        "push": fcm.send_topic_push(
            game,
            title="New levels are waiting",
            body=task["payload"].get("reason") or "Jump back in!",
            data={},
        )
    }


def _subliminal_generate_and_submit(task: dict) -> dict:
    from agent.pipelines import subliminal

    return subliminal.run(task)


def _moviequiz_generate_and_submit(task: dict) -> dict:
    from agent.pipelines import moviequiz

    return moviequiz.run(task)


def _palindrome_generate_and_submit(task: dict) -> dict:
    from agent.pipelines import palindrome

    return palindrome.run(task)
