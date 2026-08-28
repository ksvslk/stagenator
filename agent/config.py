"""Stagenator configuration: games, caps, model, environment.

All hard limits live HERE, in code — never in prompts. The Strategist's output
is validated against these caps before any action executes (guardrails.py).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Explicit path: the server may be spawned with a different cwd
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Model (hackathon requires Gemini 3.5 Flash or newer) ---
MODEL = os.getenv("MODEL_NAME", "gemini-3.7-flash")

# --- Projects ---
HOME_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "operation-sunrise")
REGION = os.getenv(
    "DEPLOY_REGION", "us-central1"
)  # Cloud Run/Scheduler region; model uses GOOGLE_CLOUD_LOCATION=global
TAKECODES_PROJECT = "take-codes"

# --- Runtime flags ---
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# --- Games ---
GAMES: dict[str, dict] = {
    "subliminal-words": {
        "display": "Subliminal Words",
        "project": "subliminal-words",
        "ga_property": "459624157",
        "platforms": ["android", "ios"],
        "level_push_topic": "sw_plus_levels",  # fired by game's own notifyOnNewLevel
        "fcm_token_collections": ["fcmTokensAndroid", "fcmTokensIos"],
        "tier": 0,  # 0 = analytics-only; 1 = identity-linked (set via playbook when app update ships)
        "level_backend": "packs",  # Storage + packs/{packId}/levels/{id} transaction
        "play_package": "com.indest.subliminalwords",
        "app_store_id": "6468366578",
    },
    "ai-movie-quiz": {
        "display": "AI Movie Quiz",
        "project": "operation-sunrise",
        "ga_property": "504066506",
        "platforms": ["android", "ios"],
        "level_push_topic": "new_levels",
        "fcm_token_collections": [],  # app subscribes by topic
        "tier": 0,
        "level_backend": "levels_counter",  # levels/{n} + counters/levelsCounter
        "play_package": "com.indest.aimoviequiz",
        "app_store_id": "6752119990",
    },
    "palindrome": {
        "display": "Palindrome",
        "project": "hah-35889753",
        "ga_property": "307701325",
        "platforms": ["android", "ios"],
        # The agent announces its own levels here (unlike SW/AMQ, whose game
        # backends fire their own new-level push).
        "level_push_topic": "player_levels",
        "fcm_token_collections": ["fcmTokensAndroid", "fcmTokensIos"],
        "tier": 1,  # Android 7.1.0 live: per-player identity + tokens
        # No proffer.codes campaign exists for this game yet — code actions are
        # refused at the gate rather than failing three times and dead-lettering.
        "codes_enabled": False,
        "level_backend": "palindrome",  # text levels into user_submitted_levels
        "play_package": "com.indest.hah",
        "app_store_id": "1673006365",
    },
}

ACTIVE_GAMES = {g for g, cfg in GAMES.items() if cfg.get("active", True)}


# --- Hard caps (enforced in guardrails.py, never negotiable by the LLM) ---
CAPS = {
    "codes_per_user_per_week": 1,
    "codes_per_game_per_day": 20,
    "code_actions_per_game_per_day": 1,  # 1 code-notification/game/day
    "levels_per_game_per_day": 1,
    "push_actions_per_game_per_4h": 2,  # lets a NEW player get welcome level + code in one window
    "drops_per_game_per_day": 2,
    "veo_videos_per_day": 5,
    "runpod_generations_per_day": 10,
}

# --- Firestore collections (all in HOME_PROJECT) ---
# STAGENATOR_COLLECTION_PREFIX isolates local/eval runs from the production
# ledger the dashboard shows (set to "stagenator_eval" in local .env).
_PREFIX = os.getenv("STAGENATOR_COLLECTION_PREFIX", "stagenator")
COL_LEDGER = f"{_PREFIX}_ledger"
COL_TASKS = f"{_PREFIX}_tasks"
COL_PLAYBOOK = f"{_PREFIX}_playbook"  # doc id: "current"; history in subcollection
COL_DIRECTIVES = f"{_PREFIX}_directives"
COL_BRIEFS = f"{_PREFIX}_briefs"

# --- Cost model (USD, for the Mission Control spend estimate) ---
UNIT_COSTS = {
    "veo_clip": 0.40,  # AI Movie Quiz level (Veo 3.1 Lite, 8s)
    "runpod_puzzle": 0.05,  # Subliminal Words level (ComfyUI endpoint run)
    "gemini_call": 0.004,  # a Strategist/Reflector/QA/design Gemini call
}

# --- Alerting ---
# critical() emails the owner immediately (deduped ~1/hr per issue); the CRITICAL log is supplementary.
CRITICAL_LOG_NAME = "stagenator-critical"

# --- proffer.codes ---
CLAIM_BASE_URL = os.getenv("CLAIM_BASE_URL", "https://proffer.codes")

# --- Task pipeline ---
MAX_TASK_ATTEMPTS = 3
# Heavy generation tasks (Veo / Runpod) can legitimately run several minutes; give
# them a longer stale window before re-lease so a slow-but-alive run is not double-executed.
HEAVY_TASKS = {"level_pipeline"}
TASK_STALE_MIN = 15
HEAVY_TASK_STALE_MIN = 30

# Bounded-growth retention (the housekeeping janitor). All comfortably exceed the
# longest window anything READS (guardrails 24h, nightly reflector 7d), so pruning
# can never remove data a live path still depends on.
LEDGER_RETENTION_DAYS = 30  # routine ledger entries
LEDGER_OUTCOME_RETENTION_DAYS = (
    90  # keep 'outcome' rows longer — they're the learning signal
)
TASK_RETENTION_DAYS = 14  # done/dead task docs (never pending/running)
BRIEF_RETENTION_DAYS = 30
RESERVATION_MAX_AGE_DAYS = (
    14  # release a code held longer than any token's TTL (max 7d)
)

GA_POLL_MINUTES = 10  # look-back window per pulse (2x overlap over 5-min cadence)
