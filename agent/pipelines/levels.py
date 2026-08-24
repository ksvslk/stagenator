"""Level pipelines — generate, validate (schema+dedup+QA), submit via Admin SDK.

Per-game submission replicates the admin dashboards' exact write paths:
- subliminal-words: Storage packs/{pack}/levels/{id}/... + level doc + counters
- ai-movie-quiz:    Storage video + levels/{n} + counters/levelsCounter
- palindrome:       user_submitted_levels curation (fill hints, levelId, isFeatured_v3)

Palindrome curation is fully implemented (pure Firestore + Gemini).
Subliminal Words (Runpod) and AI Movie Quiz (Veo) generation land next —
their submit paths are ready; generation raises a clear error until wired,
so tasks dead-letter visibly instead of pretending.
"""

import json

from google.cloud import firestore

from agent import config, state
from agent.tools import genai_client


def run(task: dict) -> dict:
    game = task["game"]
    if game == "palindrome":
        return _palindrome_curate(task)
    if game == "subliminal-words":
        return _subliminal_generate_and_submit(task)
    if game == "ai-movie-quiz":
        return _moviequiz_generate_and_submit(task)
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
    return {"push": fcm.send_topic_push(game, title="New levels are waiting",
                                        body=task["payload"].get("reason") or "Jump back in!",
                                        data={})}


# ------------------------------------------------------------- palindrome ----

HINT_LOCALES = ["hint", "hint_uk", "hint_fi", "hint_fr", "hint_de", "hint_es", "hint_ja", "hint_ru"]


def _palindrome_curate(task: dict) -> dict:
    """Publish the best pending user submission: validate palindrome, fill all
    hint locales via Gemini, assign next levelId, flip isFeatured_v3."""
    pal = state.game_db("palindrome")
    col = pal.collection("user_submitted_levels")

    pending = [
        s for s in col.where(filter=firestore.FieldFilter("levelId", "==", -1337)).limit(25).stream()
    ]
    if not pending:
        raise RuntimeError("no pending palindrome submissions to curate")

    # published levels for dedup + next levelId
    published = list(col.where(filter=firestore.FieldFilter("isFeatured_v3", "==", True)).stream())
    existing_texts = {_norm(s.to_dict().get("palindrome", "")) for s in published}
    next_id = max((s.to_dict().get("levelId", 0) for s in published), default=10000) + 1

    for snap in pending:
        d = snap.to_dict()
        text = d.get("palindrome", "")
        if not _is_palindrome(text) or _norm(text) in existing_texts:
            continue

        hints = _fill_hints(text)
        if hints is None:
            continue

        if config.DRY_RUN:
            return {"dry_run": True, "would_publish": text, "levelId": next_id}

        snap.reference.update(
            {
                "levelId": next_id,
                "isFeatured_v3": True,
                "solutionToCategory": json.dumps({text: hints}),
                "curatedBy": "stagenator",
                "curatedAt": state.now(),
            }
        )
        return {"published": text, "levelId": next_id, "player": d.get("player")}

    raise RuntimeError("no valid, novel palindrome among pending submissions")


def _norm(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


def _is_palindrome(s: str) -> bool:
    n = _norm(s)
    return len(n) >= 3 and n == n[::-1]


def _fill_hints(palindrome: str) -> dict | None:
    """Gemini fills every hint locale; QA-checks itself; None if unusable."""
    prompt = (
        f"You localize hints for a palindrome puzzle game. The palindrome is: {palindrome!r}\n"
        f"Produce a short category/hint describing what the palindrome is about, in these "
        f"locales: {HINT_LOCALES} (hint = English, hint_uk = Ukrainian, hint_fi = Finnish, "
        f"hint_fr = French, hint_de = German, hint_es = Spanish, hint_ja = Japanese, "
        f"hint_ru = Russian).\n"
        f'Reply as pure JSON: {{"ok": true, "hints": {{...}}}} — or {{"ok": false}} if the '
        f"palindrome is offensive, nonsensical, or unhintable."
    )
    reply = genai_client.generate_json(prompt)
    if not reply or not reply.get("ok"):
        return None
    hints = reply.get("hints") or {}
    if any(not hints.get(loc) for loc in HINT_LOCALES):
        return None
    return hints


# -------------------------------------------------- generation placeholders ----

def _subliminal_generate_and_submit(task: dict) -> dict:
    from agent.pipelines import subliminal

    return subliminal.run(task)


def _moviequiz_generate_and_submit(task: dict) -> dict:
    if config.DRY_RUN:
        return {"dry_run": True, "pipeline": "ai-movie-quiz"}
    raise RuntimeError("AI Movie Quiz generation not wired yet (Veo model id pending)")
