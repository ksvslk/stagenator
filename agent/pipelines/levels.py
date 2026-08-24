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

# Full locale set observed on published levels (18 keys; "hint" = English)
MIN_QUALITY = 6  # Gemini 1-10 judgment; below this a submission is skipped, not published
HINT_LOCALES = ["hint", "hint_cn", "hint_de", "hint_es", "hint_et", "hint_fi", "hint_fr",
                "hint_id", "hint_it", "hint_ja", "hint_ko", "hint_nl", "hint_pl", "hint_pt",
                "hint_ro", "hint_sv", "hint_tr", "hint_uk"]


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

    # dedup set: published submissions + the levels bundled in the app binaries
    published = list(col.where(filter=firestore.FieldFilter("isFeatured_v3", "==", True)).stream())
    existing_texts = {_norm(s.to_dict().get("palindrome", "")) for s in published}
    bundled = state.db().collection("stagenator_reference").document("palindrome_bundled").get()
    if bundled.exists:
        existing_texts |= set(bundled.to_dict().get("normalized", []))
    next_id = max((s.to_dict().get("levelId", 0) for s in published), default=10000) + 1

    for snap in pending:
        d = snap.to_dict()
        text = d.get("palindrome", "")
        if not _is_palindrome(text) or _norm(text) in existing_texts:
            continue

        hints = _fill_hints(text)
        if hints is None:
            continue
        quality = hints.pop("_quality", None)

        if config.DRY_RUN:
            return {"dry_run": True, "would_publish": text, "levelId": next_id, "quality": quality}

        snap.reference.update(
            {
                "levelId": next_id,
                "isFeatured_v3": True,
                "solutionToCategory": {text: hints},  # Firestore map, matching published levels
                "curatedBy": "stagenator",
                "curatedAt": state.now(),
            }
        )
        return {"published": text, "levelId": next_id, "player": d.get("player"), "quality": quality}

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
        f"(The palindrome itself may be in any language — figure out its meaning first.)\n"
        f"Produce a SHORT category/hint (1-3 words) describing what the palindrome is about, "
        f"for every one of these locale keys: {HINT_LOCALES} — where hint=English, cn=Chinese, "
        f"de=German, es=Spanish, et=Estonian, fi=Finnish, fr=French, id=Indonesian, it=Italian, "
        f"ja=Japanese, ko=Korean, nl=Dutch, pl=Polish, pt=Portuguese, ro=Romanian, sv=Swedish, "
        f"tr=Turkish, uk=Ukrainian.\n"
        f"Also JUDGE the level quality: is it a real word/phrase in some language (not random "
        f"letters), is the meaning graspable, would it make a satisfying puzzle? Score 1-10.\n"
        f'Reply as pure JSON: {{"ok": true, "quality": <1-10>, "language": "...", '
        f'"hints": {{"hint": "...", "hint_cn": "...", ...}}}} — or {{"ok": false, "quality": 0, '
        f'"reason": "..."}} if offensive, nonsensical, not a real palindromic phrase, or unhintable.'
    )
    reply = genai_client.generate_json(prompt)
    if not reply or not reply.get("ok"):
        return None
    if (reply.get("quality") or 0) < MIN_QUALITY:
        return None
    hints = reply.get("hints") or {}
    if any(not hints.get(loc) for loc in HINT_LOCALES):
        return None
    hints["_quality"] = reply.get("quality")
    return hints


# -------------------------------------------------- generation placeholders ----

def _subliminal_generate_and_submit(task: dict) -> dict:
    from agent.pipelines import subliminal

    return subliminal.run(task)


def _moviequiz_generate_and_submit(task: dict) -> dict:
    if config.DRY_RUN:
        return {"dry_run": True, "pipeline": "ai-movie-quiz"}
    raise RuntimeError("AI Movie Quiz generation not wired yet (Veo model id pending)")
