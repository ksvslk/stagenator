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


def _ab_variant(payload: dict, index: int) -> tuple[str | None, str | None]:
    """Built-in push A/B: when the Strategist wrote BOTH `message` and `message_alt`,
    alternate them (by recipient index for personal sends, by UTC day for topic sends)
    and return (chosen_message, variant_tag). Without an alt, no experiment: (message, None)."""
    msg, alt = payload.get("message"), payload.get("message_alt")
    if not (msg and alt):
        return msg, None
    return (msg, "a") if index % 2 == 0 else (alt, "b")


def _tc() -> firestore.Client:
    return firestore.Client(project=config.TAKECODES_PROJECT)


def _reserve_codes(campaign_id: str, n: int) -> list[str]:
    """Reserve n untorn, unreserved codes. Each grab is a transaction CONDITIONAL on
    the code still being free, so two concurrent reservations (or a retried task) can
    never stamp the same code into two claim tokens."""
    tc = _tc()
    col = tc.collection("campaigns").document(campaign_id).collection("codes")
    # Scan WIDE and filter client-side: a small doc-ID-ordered window (the old
    # limit(n*4)) starves — reservations are always grabbed from the window's start,
    # so held codes accumulate exactly there and mask plentiful free stock deeper in
    # the collection ("only 3/5 reservable" with 56 free). Campaigns are at most a
    # few hundred codes, so streaming them is cheap; the per-code transaction below
    # still guards every grab against races.
    q = col.where(filter=firestore.FieldFilter("isTorn", "==", False)).limit(500)
    candidates = [
        snap
        for snap in q.stream()
        if not (lambda d: d.get("reservedBy") or d.get("expired"))(snap.to_dict() or {})
    ]
    reserved: list[str] = []
    for snap in candidates:
        if len(reserved) >= n:
            break
        ref = snap.reference

        @firestore.transactional
        def _grab(tx: firestore.Transaction, ref=ref):
            cur = ref.get(transaction=tx).to_dict() or {}
            if cur.get("reservedBy") or cur.get("expired") or cur.get("isTorn"):
                return False  # taken between the query and now — skip it
            tx.update(ref, {"reservedBy": "stagenator", "reservedAt": state.now()})
            return True

        try:
            if _grab(tc.transaction()):
                reserved.append(snap.id)
        except Exception as e:  # a lost race is fine — just try the next code
            log.warning("reserve race on %s: %s", snap.id, e)
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
    # Mid-rollout a game has token collections but almost no registered devices
    # (only updated installs register; everyone else still hears the topic). A
    # personal drop then reaches a handful of devices while the topic reaches the
    # whole install base — so fall back to shared until registration catches up.
    if _registered_devices(game, need=MIN_PERSONAL_DEVICES) >= MIN_PERSONAL_DEVICES:
        return run_personal_codes(task)
    return _run_drop_shared(task)


# Below this many registered devices, a shared topic drop reaches more players
# than personal pushes would; above it, per-user delivery wins.
MIN_PERSONAL_DEVICES = 10


def _registered_devices(game: str, need: int) -> int:
    """Count devices with a push token, stopping once `need` are found."""
    found = 0
    gdb = state.game_db(game)
    for col in config.GAMES[game].get("fcm_token_collections") or []:
        for snap in gdb.collection(col).limit(need).stream():
            if (snap.to_dict() or {}).get("token"):
                found += 1
                if found >= need:
                    return found
    return found


def run_personal_codes(task: dict) -> dict:
    """Each registered device gets its OWN reserved, single-use code, pushed only
    to that device. Guarantees the code is ready for that user and only them."""
    game, payload = task["game"], task["payload"]
    gift_game = (
        payload.get("gift_game") or game
    )  # cross-promo: gift another game's code
    inv_campaign = payload.get("campaign_id") or _find_campaign(
        gift_game, payload.get("platform")
    )
    cap = min(payload.get("n_codes") or 10, state.effective_caps()["codes_per_game_per_day"])

    # collect registered devices (uid -> token) across android/ios
    gdb = state.game_db(game)
    devices: list[tuple[str, str]] = []  # (uid, token)
    seen_uids: set[str] = set()  # a user on both android+ios must get ONE code, not two
    for col in config.GAMES[game]["fcm_token_collections"]:
        for snap in gdb.collection(col).limit(cap * 2).stream():
            if snap.id in seen_uids:
                continue
            tok = (snap.to_dict() or {}).get("token")
            if tok:
                seen_uids.add(snap.id)
                devices.append((snap.id, tok))
            if len(devices) >= cap:
                break
    if not devices:
        return {"sent": 0, "note": "no registered devices"}

    if config.DRY_RUN:
        return {
            "dry_run": True,
            "would_send_personal": len(devices),
            "campaign": inv_campaign,
        }

    def _do():
        sent = []
        import datetime as _dt

        week_ago = state.now() - _dt.timedelta(days=7)
        for send_i, (uid, token) in enumerate(devices):
            if len(sent) >= cap:  # cap counts SENDS — dead tokens must not eat the batch
                break
            # per-user weekly cap: don't re-gift a player who got a code in the last 7 days
            _mkref = (
                state.db()
                .collection("stagenator_user_sends")
                .document(f"{game}__{uid}")
            )
            _last = (_mkref.get().to_dict() or {}).get("lastSentAt")
            if _last and _last > week_ago:
                continue
            code_ids = _reserve_codes(inv_campaign, 1)
            if not code_ids:
                state.critical(
                    f"{game}: out of codes mid personal-send",
                    game=game,
                    campaign=inv_campaign,
                )
                break
            tok = secrets.token_urlsafe(16)
            _tc().collection("claimTokens").document(tok).set(
                {
                    "kind": "single",
                    "campaignId": inv_campaign,
                    "codeIds": code_ids,
                    "claimed": [],
                    "game": gift_game,
                    "targetUser": uid,
                    "createdBy": "stagenator",
                    "createdAt": state.now(),
                    "expiresAt": state.now() + __import__("datetime").timedelta(days=7),
                }
            )
            url = f"{config.CLAIM_BASE_URL}/claim/{tok}"
            try:
                _msg, _variant = _ab_variant(payload, send_i)
                if _variant:
                    _tc().collection("claimTokens").document(tok).update({"variant": _variant})
                if gift_game != game:  # cross-promo: pitch the OTHER game
                    _gn = config.GAMES[gift_game]["display"]
                    _b = (
                        f"{_msg} " if _msg else ""
                    ) + f"A free {_gn} gift, reserved just for you — tap to claim."
                    _title = "🎁 A gift for you"
                else:
                    _b = (
                        f"{_msg} Your code is reserved just for you — tap to claim."
                        if _msg
                        else "You've earned a reward, reserved just for you — tap to claim."
                    )
                    _title = "🎁 A gift for you"
                fcm.send_to_token(
                    game, token, title=_title, body=_b, data={"claimUrl": url},
                    label=f"sg-{_variant}" if _variant else "stagenator",
                )
                _mkref.set({"lastSentAt": state.now(), "game": game}, merge=True)
                sent.append(uid)
            except Exception as e:  # one bad token never blocks the rest
                _tc().collection("campaigns").document(inv_campaign).collection(
                    "codes"
                ).document(code_ids[0]).update({"reservedBy": firestore.DELETE_FIELD})
                _tc().collection("claimTokens").document(tok).delete()
                log.warning("push to %s failed, code released: %s", uid, e)
                if "NotRegistered" in str(e) or "Unregistered" in str(e):
                    # dead install — drop the stale token so it never eats a batch again
                    for _col in config.GAMES[game].get("fcm_token_collections") or []:
                        try:
                            gdb.collection(_col).document(uid).delete()
                        except Exception:
                            pass
        return {
            "sent": len(sent),
            "personal": True,
            "each_reserved_single_use": True,
            **({"cross_promo": f"{game}->{gift_game}"} if gift_game != game else {}),
        }

    return state.once(task.get("id"), "deliver", _do)


def _run_drop_shared(task: dict) -> dict:
    """Live path for topic-only games (no per-user FCM tokens, e.g. ai-movie-quiz):
    reserve N codes, post one shared /drop/ link, topic-push it. Each anonymous
    visitor tears their own distinct reserved code — never a shared code."""
    from agent import rules

    game, payload = task["game"], task["payload"]
    gift_game = (
        payload.get("gift_game") or game
    )  # cross-promo: gift another game's code
    n = min(payload.get("n_codes") or 5, 10)

    if config.DRY_RUN:  # report intent WITHOUT reserving any codes
        plats = list(rules.campaign_inventory(gift_game).get("campaigns", {}))
        return {"dry_run": True, "would_reserve_per_platform": n, "platforms": plats}

    def _do():
        # Reserve a pool PER platform (from the GIFT game's campaigns) so one /drop/
        # link serves iOS and Android with a redeemable code.
        pools: dict[str, dict] = {}
        for plat, camp in (
            rules.campaign_inventory(gift_game).get("campaigns", {}).items()
        ):
            cid = camp.get("campaign_id")
            if not cid:
                continue
            ids = _reserve_codes(cid, n)
            if ids:
                pools[plat] = {"campaignId": cid, "codeIds": ids}
        if not pools:
            raise RuntimeError(f"no codes reservable for {game}")

        drop_id = secrets.token_urlsafe(12)
        _tc().collection("claimTokens").document(drop_id).set(
            {
                "kind": "drop",
                "pools": pools,  # {apple|google: {campaignId, codeIds}}
                "claimed": [],  # flat list; each entry tagged with its platform
                "game": gift_game,
                "createdBy": "stagenator",
                "createdAt": state.now(),
                "expiresAt": state.now() + __import__("datetime").timedelta(days=3),
            }
        )
        url = f"{config.CLAIM_BASE_URL}/drop/{drop_id}"

        topic = config.GAMES[game]["level_push_topic"]
        push = None
        if topic:
            _msg, _variant = _ab_variant(payload, state.now().toordinal())
            if _variant:
                _tc().collection("claimTokens").document(drop_id).update({"variant": _variant})
            if gift_game != game:  # cross-promo pitch for the other game
                _gn = config.GAMES[gift_game]["display"]
                _body = (
                    f"{_msg} " if _msg else ""
                ) + f"Grab a free {_gn} code — first come, first served! ⏳"
            else:
                _body = (
                    f"{_msg} Limited codes — first come, first served! ⏳"
                    if _msg
                    else "A limited code drop just went live — grab yours before they're gone! ⏳"
                )
            push = fcm.send_topic_push(
                game, title="🎁 Limited code drop", body=_body, data={"claimUrl": url},
                label=f"sg-{_variant}" if _variant else "stagenator",
            )
        return {
            "drop_id": drop_id,
            "url": url,
            "codes": sum(len(v["codeIds"]) for v in pools.values()),
            "platforms": list(pools),
            "push": push,
        }

    return state.once(task.get("id"), "deliver", _do)


def run_individual(task: dict) -> dict:
    """Tier-1 individual code: single-use token pushed to one user's device."""
    game, payload = task["game"], task["payload"]
    if config.GAMES[game]["tier"] < 1:
        raise RuntimeError(
            f"{game} is Tier 0 — individual codes need the identity-linked app update"
        )
    inv_campaign = payload.get("campaign_id") or _find_campaign(
        game, payload.get("platform")
    )

    if config.DRY_RUN:
        return {"dry_run": True, "campaign": inv_campaign}

    def _do():
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
        try:
            push = fcm.send_user_push(
                game,
                payload.get("user"),
                title="A gift for you 🎁",
                body="You've earned a reward — tap to claim it.",
                data={"claimUrl": url},
            )
        except Exception:  # release the reserved code + token so nothing is orphaned
            _tc().collection("campaigns").document(inv_campaign).collection(
                "codes"
            ).document(code_ids[0]).update({"reservedBy": firestore.DELETE_FIELD})
            _tc().collection("claimTokens").document(token).delete()
            raise
        return {"token": token, "url": url, "push": push}

    return state.once(task.get("id"), "deliver", _do)


def _find_campaign(game: str, platform: str | None = None) -> str:
    from agent import rules

    # android/ios (game vocabulary) -> google/apple (store vocabulary)
    store = {"android": "google", "ios": "apple"}.get(platform or "", platform)
    inv = rules.campaign_inventory(game, platform=store)
    if not inv["campaign_id"]:
        raise RuntimeError(f"no stagenator-managed proffer.codes campaign for {game}")
    return inv["campaign_id"]
