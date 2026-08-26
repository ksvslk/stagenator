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
        return {"name": name, "category": category, "ok": True, "warn": False,
                "critical": critical, "detail": str(detail or "ok")[:220], "ms": ms()}
    except _Warn as w:
        return {"name": name, "category": category, "ok": True, "warn": True,
                "critical": critical, "detail": str(w)[:220], "ms": ms()}
    except Exception as e:  # any real failure is surfaced, never hidden
        return {"name": name, "category": category, "ok": False, "warn": False,
                "critical": critical, "detail": f"{type(e).__name__}: {e}"[:220], "ms": ms()}


# ----------------------------------------------------------------- checks ----

def _ga_realtime(game):
    def fn():
        from google.analytics.data_v1beta.types import (
            Dimension,
            Metric,
            MinuteRange,
            RunRealtimeReportRequest,
        )

        from agent import rules
        prop = config.GAMES[game]["ga_property"]
        req = RunRealtimeReportRequest(
            property=f"properties/{prop}",
            dimensions=[Dimension(name="platform"), Dimension(name="country")],
            metrics=[Metric(name="activeUsers")],
            minute_ranges=[MinuteRange(start_minutes_ago=config.GA_POLL_MINUTES, end_minutes_ago=0)],
        )
        resp = rules.ga().run_realtime_report(req)  # raises on 400 (bad dim) / 403 (perms)
        active = sum(int(r.metric_values[0].value) for r in resp.rows)
        return f"query ok · {active} active now"
    return fn


def _ga_export(game):
    def fn():
        from google.cloud import bigquery
        prop = config.GAMES[game]["ga_property"]
        bq = bigquery.Client(project=config.HOME_PROJECT)
        ev = sorted(t.table_id for t in bq.list_tables(f"analytics_{prop}")
                    if t.table_id.startswith("events_"))
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
        msg = messaging.Message(topic=topic,
                                notification=messaging.Notification(title="health", body="health"))
        messaging.send(msg, app=fcmtool._app(game), dry_run=True)  # validates creds, sends nothing
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
    if bal < 5:
        raise _Warn(f"balance low: ${bal:.2f}")
    return f"balance ${bal:.2f}"


def _gmail():
    import imaplib

    from agent.tools.mailbox import USER, _pw
    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(USER, _pw())
    return "IMAP login ok"


def _billing():
    from google.cloud import bigquery
    bq = bigquery.Client(project=config.HOME_PROJECT)
    for ds in bq.list_datasets():
        for t in bq.list_tables(ds.dataset_id):
            if t.table_id.startswith("gcp_billing_export"):
                return f"table {t.table_id}"
    raise _Warn("billing export not populated yet")


def _proffer():
    import requests
    r = requests.get(config.CLAIM_BASE_URL, timeout=12)
    r.raise_for_status()
    return f"HTTP {r.status_code}"


def _secrets():
    missing = [k for k in ("ASC_KEY_CONTENT", "GMAIL_APP_PASSWORD", "RUNPOD_API_KEY")
               if not os.getenv(k)]
    if missing:
        raise Exception(f"missing env: {missing}")
    return "ASC · Gmail · Runpod present"


def _queue():
    from google.cloud import firestore
    dead = list(state.db().collection(config.COL_TASKS)
                .where(filter=firestore.FieldFilter("status", "==", "dead")).limit(25).stream())
    if dead:
        raise _Warn(f"{len(dead)} dead-lettered task(s)")
    return "no dead-letters"


# ------------------------------------------------------------------- run ----

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
    checks.append(_run("BigQuery billing", "analytics", False, _billing))
    checks.append(_run("App Store Connect API", "external", True, _asc))
    checks.append(_run("Runpod", "external", False, _runpod))
    checks.append(_run("Gmail (IMAP)", "external", True, _gmail))
    checks.append(_run("proffer.codes site", "external", False, _proffer))
    checks.append(_run("Secrets", "internal", True, _secrets))
    checks.append(_run("Task queue", "internal", False, _queue))

    critical_fail = [c for c in checks if not c["ok"] and c["critical"]]
    any_fail = [c for c in checks if not c["ok"]]
    warns = [c for c in checks if c.get("warn")]
    status = "down" if critical_fail else ("degraded" if (any_fail or warns) else "healthy")

    doc = {
        "status": status,
        "checks": checks,
        "ok": sum(1 for c in checks if c["ok"] and not c.get("warn")),
        "warn": len(warns),
        "fail": len(any_fail),
        "total": len(checks),
        "trigger": trigger,
        "ran_at": state.now(),
    }
    state.db().collection(config.COL_PLAYBOOK).document("health").set(doc)

    if any_fail:
        state.critical(
            f"Health check {status.upper()}: "
            + ", ".join(f"{c['name']} ({c['detail']})" for c in any_fail),
            failures=any_fail, trigger=trigger,
        )
    log.info("health check %s: %d ok, %d warn, %d fail (trigger=%s)",
             status, doc["ok"], doc["warn"], doc["fail"], trigger)
    return doc
