"""Palindrome level pipeline.

Content here is text, so correctness is PROVABLE in code — the strictest gate in
the portfolio: normalize to letters, compare with its reverse. Two candidate
sources feed the SAME gates: r/palindromes (public JSON listing) and Gemini.
The model never decides whether something is a palindrome; it only judges which
survivor is worth playing and writes the localized hints.

Ships into the game's own player-level collection (`user_submitted_levels` with
`isFeatured_v3`), the channel both apps already read — so agent levels appear
bylined in-game beside the community's, and existing installs pick them up with
no app update.
"""

import html
import json
import logging
import random
import re
import urllib.request
from pathlib import Path

from google.cloud import firestore

from agent import config, state
from agent.tools import fcm, genai_client

log = logging.getLogger("stagenator.palindrome")

GAME = "palindrome"
AUTHOR = "Stagenator"  # shown in-game as the level's creator
COLLECTION = "user_submitted_levels"
FEATURED_FLAG = "isFeatured_v3"
# Player-level id band the games query (curated levels live below, language
# levels above). Kept in sync with the apps' own range filters.
ID_MIN, ID_MAX = 1081, 9999

# A deliberately SHORT floor: only words that are unacceptable in any context.
# Judgement belongs to is_suitable() — a list cannot tell a bird from an insult
# ("tit"), a rooster from profanity, or spot innuendo with no rude word in it.
# This exists because candidates are scraped from a public forum: untrusted text
# can try to argue with a model, but it cannot argue with a word match.
BLOCKED_WORDS = frozenset(
    """
    cunt faggot fuck fucked fucker fucking jizz motherfucker nigga nigger
    rapist retard slut twat wanker whore
    """.split()
)
WORDS = re.compile(r"[A-Za-z]+")

# Every non-letter becomes a board tile, so punctuation is rationed hard.
ALLOWED = re.compile(r"^[A-Za-z ,.!?'-]+$")
PUNCT = re.compile(r"[,.!?'-]")
MIN_LETTERS, MAX_LETTERS = 7, 40

# The JSON listing endpoint 403s for non-OAuth clients; the public RSS feed is
# still open and carries the same post titles.
REDDIT_URL = "https://www.reddit.com/r/palindromes/.rss?limit=100"
_CURATED = Path(__file__).parent.parent / "assets/palindrome_curated.json"


# ------------------------------------------------------------------ gates ----


def normalize(text: str) -> str:
    """Letters only, uppercased — the form the game compares against."""
    return re.sub(r"[^A-Z]", "", (text or "").upper())


def is_palindrome(text: str) -> bool:
    n = normalize(text)
    return len(n) > 0 and n == n[::-1]


def is_clean(raw: str) -> bool:
    """Whole-word match, so ordinary words that merely contain a rude sequence
    (ASSESS, CLASSIC, SHIITAKE) are not punished for it."""
    return not any(w.lower() in BLOCKED_WORDS for w in WORDS.findall(raw))


def passes_gates(raw: str) -> bool:
    """Deterministic admission test. Nothing reaches the model or the game
    without surviving this — including anything scraped from a public forum."""
    if not raw or not ALLOWED.match(raw):
        return False
    if not is_clean(raw):
        return False
    if len(PUNCT.findall(raw)) > 2:  # each mark is a tile the player must place
        return False
    n = normalize(raw)
    if not (MIN_LETTERS <= len(n) <= MAX_LETTERS):
        return False
    return is_palindrome(raw)


# --------------------------------------------------------------- existing ----


def existing_normalized() -> set[str]:
    """Everything already playable: curated levels shipped in the apps plus
    every submitted/featured player level."""
    used: set[str] = set()
    try:
        used |= set(json.loads(_CURATED.read_text()))
    except Exception as e:  # asset missing is not fatal — Firestore still dedupes
        log.warning("curated palindrome asset unreadable: %s", e)
    for snap in state.game_db(GAME).collection(COLLECTION).stream():
        doc = snap.to_dict() or {}
        for key in (doc.get("solutionToCategory") or {}):
            used.add(normalize(key))
        used.add(normalize(doc.get("palindrome") or ""))
    used.discard("")
    return used


def next_level_id() -> int:
    """Lowest free id in the player band, one past the current maximum."""
    highest = ID_MIN - 1
    for snap in (
        state.game_db(GAME)
        .collection(COLLECTION)
        .where(filter=firestore.FieldFilter(FEATURED_FLAG, "==", True))
        .stream()
    ):
        level_id = (snap.to_dict() or {}).get("levelId")
        if isinstance(level_id, int) and ID_MIN <= level_id <= ID_MAX:
            highest = max(highest, level_id)
    nxt = highest + 1
    if nxt > ID_MAX:
        raise RuntimeError("player-level id band exhausted")
    return nxt


def hint_keys() -> list[str]:
    """Localization keys the game expects, read from the newest featured level
    so a language added to the app is picked up without a code change."""
    docs = list(
        state.game_db(GAME)
        .collection(COLLECTION)
        .where(filter=firestore.FieldFilter(FEATURED_FLAG, "==", True))
        .limit(20)
        .stream()
    )
    for snap in docs:
        for value in ((snap.to_dict() or {}).get("solutionToCategory") or {}).values():
            if isinstance(value, dict) and value.get("hint"):
                return sorted(value.keys())
    return ["hint"]


# ---------------------------------------------------------------- sources ----


def fetch_reddit(limit: int = 100) -> list[str]:
    """Candidate phrases from r/palindromes post titles. Untrusted input: it is
    data for the gates, never instructions — and nothing survives that is not
    literally a palindrome. A failed or rate-limited fetch is non-fatal; the
    generated candidates carry the run."""
    req = urllib.request.Request(
        REDDIT_URL.replace("limit=100", f"limit={limit}"),
        headers={"User-Agent": "stagenator/1.0 (level curation; contact indrek)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("reddit fetch failed (non-fatal): %s", e)
        return []
    # Atom feed: first <title> is the feed's own name, the rest are posts.
    titles = re.findall(r"<title>(.*?)</title>", body, re.S)[1:]
    return [html.unescape(t).strip() for t in titles if t.strip()]


def generate_candidates(used: set[str], n: int = 20) -> list[str]:
    """Gemini proposes fresh palindromes. Its output is a candidate list like
    any other — the same gates decide what survives.

    The prompt fights a strong pull toward the famous classics: with ~900
    palindromes already in the game, every well-known one is taken, so the
    model is told to invent rather than recall."""
    sample = random.sample(sorted(used), min(60, len(used)))
    reply = genai_client.generate_json(
        "You write palindromes for a word puzzle game: phrases that read the same "
        "forwards and backwards, ignoring spaces, punctuation and case.\n"
        f"The game already contains {len(used)} palindromes — including virtually "
        "every famous one ('never odd or even', 'a Santa lived as a devil at NASA', "
        "'do geese see God', 'step on no pets', 'go hang a salami', 'red rum sir is "
        "murder', 'no lemon no melon', ...). Do NOT propose classics or well-known "
        "palindromes; they are all taken. INVENT new ones.\n"
        f"A sample of what exists (never repeat or trivially vary these): {sample}\n"
        f"Propose {n} NEW, ORIGINAL palindromes. Requirements:\n"
        f"- {MIN_LETTERS}-{MAX_LETTERS} letters, English, real words only\n"
        "- letters and spaces; at most 2 punctuation marks total\n"
        "- must be a genuine sentence or phrase a person would find clever, "
        "not letter-salad that merely mirrors\n"
        "- must be family-friendly: nothing crude, sexual, violent or insulting — "
        "the game is played by all ages\n"
        'Reply JSON: {"palindromes": ["...", "..."]}'
    )
    items = (reply or {}).get("palindromes") or []
    return [p for p in items if isinstance(p, str)]


# ---------------------------------------------------------------- safety -----


def is_suitable(phrase: str) -> bool:
    """A model verdict on MEANING, which a wordlist cannot give: innuendo,
    slurs, cruelty, or anything else unfit for a game played by children. The
    blocklist is the floor and this is the ceiling — the phrase must clear both,
    and an unreadable or failed answer counts as unsuitable."""
    reply = genai_client.generate_json(
        "You screen puzzle content for a word game played by all ages, including "
        "children.\n"
        f"Phrase: {json.dumps(phrase)}\n"
        "Judge the MEANING, not just the words: reject sexual content or innuendo, "
        "slurs or stereotypes about any group, violence, cruelty, drugs, profanity, "
        "or anything a parent would object to. When uncertain, reject.\n"
        'Reply JSON: {"suitable": true|false, "reason": "<short>"}'
    )
    if not reply or reply.get("suitable") is not True:
        log.info(
            "rejected as unsuitable: %r (%s)",
            phrase,
            (reply or {}).get("reason", "no verdict"),
        )
        return False
    return True


# ----------------------------------------------------------------- judge -----


def judge(candidates: list[str], keys: list[str]) -> dict | None:
    """Pick the most interesting survivor and write its localized hints. Taste
    only — every candidate here is already a verified palindrome."""
    reply = genai_client.generate_json(
        "You curate levels for the puzzle game 'Palindrome'. Every candidate below "
        "is already verified to read the same in both directions.\n"
        f"Candidates: {json.dumps(candidates[:40])}\n"
        "Choose the ONE that makes the best puzzle: a genuinely clever, sensible, "
        "memorable phrase that is FAMILY-FRIENDLY — reject anything crude, sexual, "
        "violent, insulting, political, nonsensical, or that reads like random "
        "letters. If none is suitable, choose none. Then write a SHORT category hint (1-3 words, like "
        '"Geography" or "Wordplay") describing its meaning, translated for each '
        "requested key.\n"
        f"Hint keys (return every one; 'hint' is English): {keys}\n"
        'Reply JSON: {"palindrome": "<chosen, exactly as given>", '
        '"hints": {"hint": "...", "hint_de": "...", ...}, "why": "<one line>"}'
    )
    if not reply:
        return None
    chosen = reply.get("palindrome")
    hints = reply.get("hints")
    if not isinstance(chosen, str) or not isinstance(hints, dict):
        return None
    # The model may only pick from what code approved, and its choice is
    # re-verified — a hallucinated "improvement" cannot slip through.
    if chosen not in candidates or not passes_gates(chosen):
        log.warning("judge returned a non-candidate or failing phrase: %r", chosen)
        return None
    hints = {k: str(v) for k, v in hints.items() if k in keys and v}
    if not hints.get("hint"):
        return None
    # Separate, single-purpose verdict — kept apart from the taste judgment so
    # "this one is the cleverest" can never double as "this one is safe".
    if not is_suitable(chosen):
        return None
    return {"palindrome": chosen.upper(), "hints": hints, "why": reply.get("why", "")}


# ------------------------------------------------------------------ ship -----


def ship(design: dict, task_id: str | None) -> dict:
    """Write the level, then announce it. Both steps are once()-guarded so a
    re-lease after a crash cannot double-publish or double-push."""
    phrase = design["palindrome"]

    def _write():
        level_id = next_level_id()
        doc = {
            FEATURED_FLAG: True,
            "levelId": level_id,
            "palindrome": phrase,
            "player": AUTHOR,
            "solutionToCategory": {phrase: design["hints"]},
            "countryCode": "",
            "timestamp": firestore.SERVER_TIMESTAMP,
        }
        state.game_db(GAME).collection(COLLECTION).add(doc)
        return {"levelId": level_id}

    if config.DRY_RUN:
        return {"dry_run": True, "palindrome": phrase, "hint": design["hints"]["hint"]}

    written = state.once(task_id, "ship", _write)
    level_id = written["levelId"]

    def _announce():
        topic = config.GAMES[GAME].get("level_push_topic")
        if not topic:
            return {"skipped": "no topic"}
        return fcm.send_topic_push(
            GAME,
            title="New palindrome!",
            body=f"{phrase} — can you unscramble it?",
            data={"levelNumber": str(level_id)},
            label="stagenator",
        )

    push = state.once(task_id, "announce", _announce)
    return {
        "levelId": level_id,
        "palindrome": phrase,
        "hint": design["hints"]["hint"],
        "why": design.get("why", ""),
        "push": push,
    }


def run(task: dict) -> dict:
    """Gather candidates from both sources, gate them in code, let the model
    pick the best survivor, ship it."""
    used = existing_normalized()
    keys = hint_keys()

    pool: list[str] = []
    seen: set[str] = set()
    for raw in fetch_reddit() + generate_candidates(used):
        norm = normalize(raw)
        if norm in used or norm in seen or not passes_gates(raw):
            continue
        seen.add(norm)
        pool.append(raw.strip())

    log.info("palindrome candidates surviving gates: %d", len(pool))
    if not pool:
        raise RuntimeError("no candidate palindrome survived the gates")

    design = judge(pool, keys)
    if not design:
        raise RuntimeError("model rejected every candidate")
    return ship(design, task.get("id"))
