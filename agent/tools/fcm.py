"""FCM delivery — topic pushes and (Tier 1) per-user token pushes.

Uses firebase-admin with one app per game project (each game's FCM lives in
its own Firebase project). Data payloads carry claimUrl for apps that have
the tap handler; apps without it still open on tap (re-engagement)."""

import logging

import firebase_admin
from firebase_admin import credentials, messaging

from agent import config

log = logging.getLogger("stagenator.fcm")
_apps: dict[str, firebase_admin.App] = {}


def _app(game: str) -> firebase_admin.App:
    project = config.GAMES[game]["project"]
    if project not in _apps:
        _apps[project] = firebase_admin.initialize_app(
            credentials.ApplicationDefault(), {"projectId": project}, name=project
        )
    return _apps[project]


def send_topic_push(game: str, title: str, body: str, data: dict, label: str = "stagenator") -> dict:
    topic = config.GAMES[game]["level_push_topic"]
    if not topic:
        raise RuntimeError(f"{game} has no push topic")
    msg = messaging.Message(
        topic=topic,
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
        fcm_options=messaging.FCMOptions(analytics_label=label),  # -> notification_open/dismiss/receive in GA4
    )
    message_id = messaging.send(msg, app=_app(game))
    log.info("topic push sent game=%s topic=%s id=%s", game, topic, message_id)
    return {"message_id": message_id, "topic": topic}


def send_to_token(game: str, token: str, title: str, body: str, data: dict, label: str = "stagenator") -> dict:
    """Send to a single raw device token (the caller already resolved it)."""
    msg = messaging.Message(
        token=token,
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
        fcm_options=messaging.FCMOptions(analytics_label=label),
    )
    return {"message_id": messaging.send(msg, app=_app(game))}


def send_user_push(game: str, uid: str, title: str, body: str, data: dict) -> dict:
    """Push to one user's registered device tokens (fcmTokensAndroid/Ios docs keyed by uid)."""
    from agent import state

    if not uid:
        raise ValueError("send_user_push needs a uid")
    gdb = state.game_db(game)
    tokens = []
    for col in config.GAMES[game]["fcm_token_collections"]:
        snap = gdb.collection(col).document(uid).get()
        if snap.exists and snap.to_dict().get("token"):
            tokens.append(snap.to_dict()["token"])
    if not tokens:
        raise RuntimeError(f"no FCM tokens for user {uid} in {game}")
    msg = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in data.items()},
        fcm_options=messaging.FCMOptions(analytics_label="stagenator"),
    )
    resp = messaging.send_each_for_multicast(msg, app=_app(game))
    return {"success": resp.success_count, "failure": resp.failure_count, "tokens": len(tokens)}
