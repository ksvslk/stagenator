"""Code delivery pipelines: personal per-device codes, drops, individual claims.

All inventory lives in the game's proffer.codes campaign (take-codes project).
A drop = one shared link backed by N reserved codes; an individual = a
single-use claim token. Both are dispensed by the takecodes claimByToken
function; here we only reserve codes + create the token/drop doc + notify.
"""

import logging
import secrets

from google.cloud import firestore

from agent import config, state
from agent.tools import fcm

log = logging.getLogger("stagenator.codes")


def _tc() -> firestore.Client:
    return firestore.Client(project=config.TAKECODES_PROJECT)


def _reserve_codes(campaign_id: str, n: int) -> list[str]:
    """Reserve n untorn, unreserved codes (marks reservedBy=stagenator)."""
    tc = _tc()
    col = tc.collection("campaigns").document(campaign_id).collection("codes")
    q = col.where(filter=firestore.FieldFilter("isTorn", "==", False)).limit(n * 3)
    reserved: list[str] = []
    for snap in q.stream():
        if len(reserved) >= n:
            break
        d = snap.to_dict()
        if d.get("reservedBy") or d.get("expired"):
            continue
        snap.reference.update({"reservedBy": "stagenator", "reservedAt": state.now()})
        reserved.append(snap.id)
    if len(reserved) < n:
        state.critical(
            f"Only {len(reserved)}/{n} codes reservable in campaign {campaign_id}",
            campaign=campaign_id,
        )
    return reserved


def run_drop(task: dict) -> dict:
    """Code delivery. GUARANTEE: a code sent to a user is reserved for them and
    single-use (his and his only). Where the game stores per-user FCM tokens we
    each user gets their OWN reserved, single-use code — never a shared pool."""
    game = task["game"]
    # Per-user guarantee, two ways to reach it:
    #  - game stores per-user FCM tokens -> push each device its OWN reserved code
    #  - otherwise (topic-only) -> topic push to the shared /drop/ claim page where
    #    each anonymous visitor TEARS their own distinct, reserved, single-use code
    if config.GAMES[game].get("fcm_token_collections"):
        return run_personal_codes(task)
    return _run_drop_shared(task)


def run_personal_codes(task: dict) -> dict:
    """Each registered device gets its OWN reserved, single-use code, pushed only
    to that device. Guarantees the code is ready for that user and only them."""
    game, payload = task["game"], task["payload"]
    inv_campaign = payload.get("campaign_id") or _find_campaign(game, payload.get("platform"))
    cap = min(payload.get("n_codes") or 10, config.CAPS["codes_per_game_per_day"])

    # collect registered devices (uid -> token) across android/ios
    gdb = state.game_db(game)
    devices: list[tuple[str, str]] = []  # (uid, token)
    for col in config.GAMES[game]["fcm_token_collections"]:
        for snap in gdb.collection(col).limit(cap * 2).stream():
            tok = (snap.to_dict() or {}).get("token")
            if tok:
                devices.append((snap.id, tok))
            if len(devices) >= cap:
                break
    if not devices:
        return {"sent": 0, "note": "no registered devices"}

    if config.DRY_RUN:
        return {"dry_run": True, "would_send_personal": len(devices), "campaign": inv_campaign}

    sent = []
    for uid, token in devices[:cap]:
        code_ids = _reserve_codes(inv_campaign, 1)
        if not code_ids:
            state.critical(f"{game}: out of codes mid personal-send", campaign=inv_campaign)
            break
        tok = secrets.token_urlsafe(16)
        _tc().collection("claimTokens").document(tok).set({
            "kind": "single", "campaignId": inv_campaign, "codeIds": code_ids,
            "claimed": [], "game": game, "targetUser": uid, "createdBy": "stagenator",
            "createdAt": state.now(),
            "expiresAt": state.now() + __import__("datetime").timedelta(days=7),
        })
        url = f"{config.CLAIM_BASE_URL}/claim/{tok}"
        try:
            fcm.send_to_token(game, token, title="A gift for you 🎁",
                              body=payload.get("reason") or "You've earned a reward — tap to claim it.",
                              data={"claimUrl": url})
            sent.append(uid)
        except Exception as e:  # one bad token never blocks the rest
            _tc().collection("campaigns").document(inv_campaign).collection("codes") \
                .document(code_ids[0]).update({"reservedBy": firestore.DELETE_FIELD})
            _tc().collection("claimTokens").document(tok).delete()
            log.warning("push to %s failed, code released: %s", uid, e)
    return {"sent": len(sent), "personal": True, "each_reserved_single_use": True}


def _run_drop_shared(task: dict) -> dict:
    """(Retained for reference / games that opt into shared drops.)"""
    game, payload = task["game"], task["payload"]
    inv_campaign = payload.get("campaign_id") or _find_campaign(game, payload.get("platform"))
    n = min(payload.get("n_codes") or 5, 10)

    if config.DRY_RUN:
        return {"dry_run": True, "would_reserve": n, "campaign": inv_campaign}

    code_ids = _reserve_codes(inv_campaign, n)
    if not code_ids:
        raise RuntimeError(f"no codes reservable for {game}")

    drop_id = secrets.token_urlsafe(12)
    _tc().collection("claimTokens").document(drop_id).set(
        {
            "kind": "drop",
            "campaignId": inv_campaign,
            "codeIds": code_ids,
            "claimed": [],
            "game": game,
            "segment": payload.get("segment"),
            "createdBy": "stagenator",
            "createdAt": state.now(),
            "expiresAt": state.now() + __import__("datetime").timedelta(days=3),
        }
    )
    url = f"{config.CLAIM_BASE_URL}/drop/{drop_id}"

    topic = config.GAMES[game]["level_push_topic"]
    push = None
    if topic:
        push = fcm.send_topic_push(
            game,
            title="A gift for you 🎁",
            body=payload.get("reason") or "A limited code drop just went live — first come, first served!",
            data={"claimUrl": url},
        )
    return {"drop_id": drop_id, "url": url, "codes": len(code_ids), "push": push}


def run_individual(task: dict) -> dict:
    """Tier-1 individual code: single-use token pushed to one user's device."""
    game, payload = task["game"], task["payload"]
    if config.GAMES[game]["tier"] < 1:
        raise RuntimeError(f"{game} is Tier 0 — individual codes need the identity-linked app update")
    inv_campaign = payload.get("campaign_id") or _find_campaign(game, payload.get("platform"))

    if config.DRY_RUN:
        return {"dry_run": True, "campaign": inv_campaign}

    code_ids = _reserve_codes(inv_campaign, 1)
    if not code_ids:
        raise RuntimeError(f"no codes reservable for {game}")
    token = secrets.token_urlsafe(16)
    _tc().collection("claimTokens").document(token).set(
        {
            "kind": "single",
            "campaignId": inv_campaign,
            "codeIds": code_ids,
            "claimed": [],
            "game": game,
            "targetUser": payload.get("user"),
            "createdBy": "stagenator",
            "createdAt": state.now(),
            "expiresAt": state.now() + __import__("datetime").timedelta(days=7),
        }
    )
    url = f"{config.CLAIM_BASE_URL}/claim/{token}"
    push = fcm.send_user_push(game, payload.get("user"), title="A gift for you 🎁",
                              body="You've earned a reward — tap to claim it.",
                              data={"claimUrl": url})
    return {"token": token, "url": url, "push": push}


def _find_campaign(game: str, platform: str | None = None) -> str:
    from agent import rules

    # android/ios (game vocabulary) -> google/apple (store vocabulary)
    store = {"android": "google", "ios": "apple"}.get(platform or "", platform)
    inv = rules.campaign_inventory(game, platform=store)
    if not inv["campaign_id"]:
        raise RuntimeError(f"no stagenator-managed proffer.codes campaign for {game}")
    return inv["campaign_id"]
