"""Real health checks — every external dependency and internal resource the agent
actually touches, exercised for real. Run once at deploy and once a day.

The point: NOTHING is swallowed. A broken dependency shows up RED with its error,
so a blind GA layer (or a dead DB, a bad app password, an expired ASC key) can never
again masquerade as "healthy". Each check runs the genuine call path — e.g. the GA
check makes the exact realtime query the pulse uses, so an invalid-dimension 400
fails the check instead of hiding behind an empty result.

Writes stagenator_playbook/health for Mission Control and raises a CRITICAL alert
when a critical dependency is down.
"""

import logging
import os
import time as _time

from agent import config, state

log = logging.getLogger("stagenator.health")


class _Warn(Exception):
    """Non-critical degradation (e.g. low balance, export not populated yet)."""


def _run(name: str, category: str, critical: bool, fn) -> dict:
    t0 = _time.monotonic()
    ms = lambda: int((_time.monotonic() - t0) * 1000)  # noqa: E731
    try:
        detail = fn()
        return {
            "name": name,
            "category": category,
            "ok": True,
            "warn": False,
            "critical": critical,
            "detail": str(detail or "ok")[:220],
            "ms": ms(),
        }
    except _Warn as w:
        return {
            "name": name,
            "category": category,
            "ok": True,
            "warn": True,
            "critical": critical,
            "detail": str(w)[:220],
            "ms": ms(),
        }
    except Exception as e:  # any real failure is surfaced, never hidden
        return {
            "name": name,
            "category": category,
            "ok": False,
            "warn": False,
            "critical": critical,
            "detail": f"{type(e).__name__}: {e}"[:220],
            "ms": ms(),
        }


# ----------------------------------------------------------------- checks ----


def _ga_realtime(game):
    def fn():
        from agent import rules

        active = sum(
            rules.realtime_snapshot(game).values()
        )  # the EXACT query the pulse runs
        return f"query ok · {active} active now"

    return fn


def _ga_export(game):
    def fn():
        from google.cloud import bigquery

        prop = config.GAMES[game]["ga_property"]
        bq = bigquery.Client(project=config.HOME_PROJECT)
        ev = sorted(
            t.table_id
            for t in bq.list_tables(f"analytics_{prop}")
            if t.table_id.startswith("events_")
        )
        if not ev:
            raise _Warn("no events_ tables yet")
        return f"latest {ev[-1]}"

    return fn


def _fs_home():
    state.db().collection(config.COL_PLAYBOOK).document("heartbeat").get()
    return "read ok"


def _fs_game(game):
    def fn():
        gdb = state.game_db(game)
        next(iter(gdb.collections()), None)  # a cheap real read
        return "read ok"

    return fn


def _fs_takecodes():
    from google.cloud import firestore

    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    list(tc.collection("campaigns").limit(1).stream())
    return "read ok"


def _fcm(game):
    def fn():
        from firebase_admin import messaging

        from agent.tools import fcm as fcmtool

        topic = config.GAMES[game].get("level_push_topic")
        if not topic:
            return "no topic (n/a)"
        msg = messaging.Message(
            topic=topic,
            notification=messaging.Notification(title="health", body="health"),
        )
        messaging.send(
            msg, app=fcmtool._app(game), dry_run=True
        )  # validates creds, sends nothing
        return "credentials + dry-run ok"

    return fn


def _asc():
    from agent.tools import asc

    prods = asc.list_products(config.GAMES["subliminal-words"]["app_store_id"])
    return f"authenticated · {len(prods)} products"


def _runpod():
    from agent.tools import runpod

    bal = runpod.account_balance()
    if bal is None:
        raise _Warn("balance unreadable (key lacks account scope?)")
    if bal < 1:
        raise _Warn(f"balance low: ${bal:.2f}")
    return f"balance ${bal:.2f}"


def _gmail():
    import imaplib

    from agent.tools.mailbox import USER, _pw

    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(USER, _pw())
    return "IMAP login ok"


def _proffer():
    import requests

    r = requests.get(config.CLAIM_BASE_URL, timeout=12)
    r.raise_for_status()
    return f"HTTP {r.status_code}"


def _secrets():
    missing = [
        k
        for k in ("ASC_KEY_CONTENT", "GMAIL_APP_PASSWORD", "RUNPOD_API_KEY")
        if not os.getenv(k)
    ]
    if missing:
        raise Exception(f"missing env: {missing}")
    return "ASC · Gmail · Runpod present"


def _queue():
    import datetime as dt

    from google.cloud import firestore

    dead = list(
        state.db()
        .collection(config.COL_TASKS)
        .where(filter=firestore.FieldFilter("status", "==", "dead"))
        .limit(25)
        .stream()
    )
    # Only FRESH dead-letters (24h) degrade health: the failure already alerted when it
    # happened (email + feed row); an old corpse is history, not an active problem, and
    # must not keep the system amber for the 14 days until housekeeping prunes it.
    cutoff = state.now() - dt.timedelta(hours=24)
    fresh = [
        d for d in dead if ((d.to_dict() or {}).get("updated") or state.now()) >= cutoff
    ]
    if fresh:
        raise _Warn(f"{len(fresh)} dead-lettered task(s) in the last 24h")
    if dead:
        return f"no fresh dead-letters ({len(dead)} older, pruned after 14d)"
    return "no dead-letters"


# ------------------------------------------------------------------- run ----


def _benign_warn(name: str) -> bool:
    """Warnings that are informational, not actionable — don't email for these
    (they still surface on the dashboard). The analytics exports simply
    take time to populate; that is not a problem the owner needs to act on."""
    return name.startswith("GA4 export")


def _email_degrade(status: str, problems: list[dict], trigger: str) -> None:
    """Email the owner a plain-language summary of what degraded."""
    lines = [f"Stagenator health is {status.upper()}.", ""]
    for c in problems:
        tag = "DOWN" if not c["ok"] else "low"
        lines.append(f"  [{tag}] {c['name']}: {c['detail']}")
    lines += [
        "",
        f"Checked {state.now():%Y-%m-%d %H:%M} UTC (trigger: {trigger}).",
        "Dashboard: https://stagenator-mission.web.app",
        "",
        "One email per change — not a daily repeat for the same issue.",
    ]
    try:
        from agent.tools import mailbox

        mailbox.send_alert(f"Stagenator health: {status}", "\n".join(lines))
    except Exception as e:
        log.warning("health degrade email failed: %s", e)


def run_health_checks(trigger: str = "manual") -> dict:
    """Run every check, write the report, escalate on a critical failure."""
    checks: list[dict] = []
    checks.append(_run("Firestore · home", "storage", True, _fs_home))
    checks.append(_run("Firestore · take-codes", "storage", True, _fs_takecodes))
    for g in sorted(config.ACTIVE_GAMES):
        checks.append(_run(f"GA realtime · {g}", "analytics", True, _ga_realtime(g)))
        checks.append(_run(f"GA4 export · {g}", "analytics", False, _ga_export(g)))
        checks.append(_run(f"Firestore · {g}", "storage", True, _fs_game(g)))
        checks.append(_run(f"FCM · {g}", "messaging", True, _fcm(g)))
    checks.append(_run("App Store Connect API", "external", True, _asc))
    checks.append(_run("Runpod", "external", False, _runpod))
    checks.append(_run("Gmail (IMAP)", "external", True, _gmail))
    checks.append(_run("proffer.codes site", "external", False, _proffer))
    checks.append(_run("Secrets", "internal", True, _secrets))
    checks.append(_run("Task queue", "internal", False, _queue))

    critical_fail = [c for c in checks if not c["ok"] and c["critical"]]
    any_fail = [c for c in checks if not c["ok"]]
    warns = [c for c in checks if c.get("warn")]
    status = (
        "down" if critical_fail else ("degraded" if (any_fail or warns) else "healthy")
    )

    problems = [c for c in checks if not c["ok"] or c.get("warn")]
    problem_names = sorted(c["name"] for c in problems)

    # Only email for REAL problems: any hard failure, plus meaningful warnings
    # (low balance, dead-lettered work, etc.). Benign "not populated yet" warnings
    # on the analytics-export checks still show on the dashboard but don't email.
    alert_problems = [c for c in problems if not c["ok"] or not _benign_warn(c["name"])]
    alert_names = sorted(c["name"] for c in alert_problems)

    prev = state.db().collection(config.COL_PLAYBOOK).document("health").get()
    prev = prev.to_dict() if getattr(prev, "exists", False) else {}

    doc = {
        "status": status,
        "checks": checks,
        "problems": problem_names,
        "alert_problems": alert_names,
        "ok": sum(1 for c in checks if c["ok"] and not c.get("warn")),
        "warn": len(warns),
        "fail": len(any_fail),
        "total": len(checks),
        "trigger": trigger,
        "ran_at": state.now(),
    }
    state.db().collection(config.COL_PLAYBOOK).document("health").set(doc)

    # Email on real problems only, and only when the set CHANGED — so an ongoing
    # issue (e.g. low balance) sends one email, not a daily repeat, and benign
    # export warnings never trigger a message.
    if alert_problems and set(prev.get("alert_problems", [])) != set(alert_names):
        _email_degrade(status, alert_problems, trigger)

    if any_fail:
        state.critical(
            f"Health check {status.upper()}: "
            + ", ".join(f"{c['name']} ({c['detail']})" for c in any_fail),
            _email=False,  # _email_degrade already emailed (change-gated) — no duplicate
            failures=any_fail,
            trigger=trigger,
        )
    log.info(
        "health check %s: %d ok, %d warn, %d fail (trigger=%s)",
        status,
        doc["ok"],
        doc["warn"],
        doc["fail"],
        trigger,
    )
    return doc
