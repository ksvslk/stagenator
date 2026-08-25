"""Code delivery pipelines: drops, individual claims, Palindrome banner.

All inventory lives in the game's proffer.codes campaign (take-codes project).
A drop = one shared link backed by N reserved codes; an individual = a
single-use claim token. Both are dispensed by the takecodes claimByToken
function; here we only reserve codes + create the token/drop doc + notify.
"""

import secrets

from google.cloud import firestore

from agent import config, state
from agent.tools import fcm


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
    """Segment drop: /drop/<id> link backed by N codes, announced by topic push."""
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


def run_banner(task: dict) -> dict:
    """Palindrome's live channel: the app reads promos/menu_button ({activated,
    link, title}). We write THAT doc (the only one it renders), saving the prior
    content to promos/stagenator_menu_button_backup so the cross-promo is
    restored when the drop ends."""
    payload = task["payload"]
    drop = run_drop({**task, "game": "palindrome",
                     "payload": {**payload, "n_codes": payload.get("n_codes") or 5}}) \
        if not payload.get("url") else {"url": payload["url"]}

    if config.DRY_RUN:
        return {"dry_run": True, **drop}

    pal = state.game_db("palindrome")
    menu = pal.collection("promos").document("menu_button")
    prior = menu.get()
    if prior.exists and prior.to_dict().get("managedBy") != "stagenator":
        pal.collection("promos").document("stagenator_menu_button_backup").set(
            prior.to_dict() | {"backedUpAt": state.now()})
    menu.set({
        "activated": True,
        "title": payload.get("title") or "🎁 Free gift — tap to claim",
        "link": drop["url"],
        "managedBy": "stagenator",
        "expiresAt": state.now() + __import__("datetime").timedelta(days=3),
        "updatedAt": state.now(),
    })
    return {"banner": "menu_button", **drop}


def restore_banner_if_expired() -> None:
    """Called each pulse: when the drop banner is past expiry, restore the
    prior cross-promo (or deactivate) so the slot isn't hijacked forever."""
    pal = state.game_db("palindrome")
    menu = pal.collection("promos").document("menu_button")
    snap = menu.get()
    d = snap.to_dict() or {}
    if d.get("managedBy") != "stagenator":
        return
    exp = d.get("expiresAt")
    if exp and exp > state.now():
        return
    backup = pal.collection("promos").document("stagenator_menu_button_backup").get()
    if backup.exists:
        b = {k: v for k, v in backup.to_dict().items() if k not in ("backedUpAt",)}
        menu.set(b)
    else:
        menu.set({"activated": False}, merge=True)


def _find_campaign(game: str, platform: str | None = None) -> str:
    from agent import rules

    # android/ios (game vocabulary) -> google/apple (store vocabulary)
    store = {"android": "google", "ios": "apple"}.get(platform or "", platform)
    inv = rules.campaign_inventory(game, platform=store)
    if not inv["campaign_id"]:
        raise RuntimeError(f"no stagenator-managed proffer.codes campaign for {game}")
    return inv["campaign_id"]
