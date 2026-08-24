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
REGION = os.getenv("DEPLOY_REGION", "us-central1")  # Cloud Run/Scheduler region; model uses GOOGLE_CLOUD_LOCATION=global
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
        "level_push_topic": None,  # no FCM in apps — promos banner is the channel
        "fcm_token_collections": [],
        "tier": 0,
        "level_backend": "user_submitted_levels",
        "play_package": "com.indest.hah",
        "app_store_id": "1673006365",
    },
}

# --- Hard caps (enforced in guardrails.py, never negotiable by the LLM) ---
CAPS = {
    "codes_per_user_per_week": 1,
    "codes_per_game_per_day": 20,
    "levels_per_game_per_day": 3,
    "push_actions_per_game_per_4h": 1,
    "drops_per_game_per_day": 2,
    "veo_videos_per_day": 5,
    "runpod_generations_per_day": 10,
}

# --- Firestore collections (all in HOME_PROJECT) ---
COL_LEDGER = "stagenator_ledger"
COL_TASKS = "stagenator_tasks"
COL_PLAYBOOK = "stagenator_playbook"  # doc id: "current"; history in subcollection
COL_DIRECTIVES = "stagenator_directives"
COL_BRIEFS = "stagenator_briefs"

# --- Alerting ---
# severity=CRITICAL structured logs drive the Cloud Monitoring email alert policy.
CRITICAL_LOG_NAME = "stagenator-critical"

# --- proffer.codes ---
CLAIM_BASE_URL = os.getenv("CLAIM_BASE_URL", "https://proffer.codes")

# --- Task pipeline ---
MAX_TASK_ATTEMPTS = 3
GA_POLL_MINUTES = 10  # look-back window per pulse (2x overlap over 5-min cadence)
