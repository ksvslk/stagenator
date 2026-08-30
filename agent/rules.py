"""Signal detection — deterministic code, no LLM.

Each pulse: poll GA realtime for all games, read code inventory, diff against
the ledger, and emit only *new* signals. If this returns an empty list, the
pulse exits without ever invoking Gemini (a zero-cost tick).
"""

import datetime as dt
import logging

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    MinuteRange,
    RunRealtimeReportRequest,
    RunReportRequest,
)
from google.cloud import firestore

from agent import config, state

log = logging.getLogger("stagenator.rules")
_ga: BetaAnalyticsDataClient | None = None


def ga() -> BetaAnalyticsDataClient:
    global _ga
    if _ga is None:
        _ga = BetaAnalyticsDataClient()
    return _ga


def realtime_snapshot(game: str) -> dict:
    """Active users in the poll window, keyed platform:country.

    The breakdown is INFORMATION ONLY — shown on the signal and to the Strategist as
    context. It is never a dedup key, a notification limit, or a targeting rule: all
    limiting logic is per-game (one signal, one key) until the games ship setUserId
    and real per-player identity exists."""
    prop = config.GAMES[game]["ga_property"]
    req = RunRealtimeReportRequest(
        property=f"properties/{prop}",
        dimensions=[Dimension(name="platform"), Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
        minute_ranges=[
            MinuteRange(start_minutes_ago=config.GA_POLL_MINUTES, end_minutes_ago=0)
        ],
    )
    # NO try/except here: a failed realtime query is BLINDNESS, not "0 users". The caller
    # (detect_signals) catches it and alerts immediately.
    resp = ga().run_realtime_report(req)
    rows: dict = {}
    for r in resp.rows:
        key = f"{r.dimension_values[0].value or 'unknown'}:{r.dimension_values[1].value or 'unknown'}"
        rows[key] = rows.get(key, 0) + int(r.metric_values[0].value)
    return rows


def realtime_activity(game: str) -> dict:
    """Live gameplay evidence for the poll window: total event count + the top event
    names firing right now (level_start, level_complete, ...). This grounds "engaged"
    in what the active user is actually DOING, instead of inferring it from mere
    presence. Aggregate (not per-player), but with a lone active user it IS that user.
    Separate from realtime_snapshot and fully guarded — if it fails, the core signal
    still fires; we just lose the enrichment (never blindness)."""
    prop = config.GAMES[game]["ga_property"]
    req = RunRealtimeReportRequest(
        property=f"properties/{prop}",
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        minute_ranges=[
            MinuteRange(start_minutes_ago=config.GA_POLL_MINUTES, end_minutes_ago=0)
        ],
    )
    try:
        resp = ga().run_realtime_report(req)
        events = sorted(
            (
                (r.dimension_values[0].value, int(r.metric_values[0].value))
                for r in resp.rows
            ),
            key=lambda x: -x[1],
        )
        return {"total": sum(c for _, c in events), "top": events[:5]}
    except Exception as e:
        log.warning("GA realtime activity failed for %s: %s", game, e)
        return {}


def campaign_inventory(game: str, platform: str | None = None) -> dict:
    """Per-platform availableCodes on the game's adopted proffer.codes campaigns.

    Returns {"campaigns": {platform: {campaign_id, available}}, "campaign_id", "available"}
    where the top-level pair reflects the requested platform (or the lowest-stock
    one, which is what replenish cares about).
    """
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    q = (
        tc.collection("campaigns")
        .where(filter=firestore.FieldFilter("managedBy", "==", "stagenator"))
        .where(filter=firestore.FieldFilter("game", "==", game))
    )
    campaigns: dict[str, dict] = {}
    for snap in q.stream():
        d = snap.to_dict()
        p = d.get("stagenatorPlatform") or d.get("platform") or "unknown"
        campaigns[p] = {"campaign_id": snap.id, "available": d.get("availableCodes", 0)}
    if not campaigns:
        return {"campaign_id": None, "available": None, "campaigns": {}}
    if platform and platform in campaigns:
        chosen = campaigns[platform]
    else:
        chosen = min(campaigns.values(), key=lambda c: c["available"] or 0)
    return {**chosen, "campaigns": campaigns}


def refresh_cost_summary() -> None:
    """Spend panel data: Runpod prepaid balance only.

    GCP billing export was removed deliberately: the export proved unreliable
    (silently dead for a month, no backfill) and the GCP side of this system runs
    in cents. Runpod is the one real, prepaid spend lever — its live balance is
    what the agent (and the owner) actually needs to watch."""
    from agent import state
    from agent.tools import runpod

    try:
        runpod_balance = runpod.account_balance()
    except Exception as e:  # never let a balance hiccup break the pulse
        log.warning("runpod balance read failed: %s", e)
        runpod_balance = None

    state.db().collection(config.COL_PLAYBOOK).document("cost_summary").set(
        {
            "status": "live",
            "runpod_balance_usd": runpod_balance,
            "updated": state.now(),
        }
    )


def refresh_codes_summary() -> None:
    """Dashboard-readable summary of code stock + claims via agent links.

    Lives under the playbook path so existing rules cover it. Counts come from
    the agent's own claimTokens (tears via stagenator links) + campaign stock."""
    from agent import state

    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    ref = state.db().collection(config.COL_PLAYBOOK).document("codes_summary")
    # Prior per-token claim counts: the baseline for detecting NEW claims below.
    prev_counts: dict = (ref.get().to_dict() or {}).get("token_claims", {})
    summary: dict = {}
    token_claims: dict[str, int] = {}
    for game in config.ACTIVE_GAMES:
        inv = campaign_inventory(game)
        summary[game] = {"stock": inv["campaigns"]}
    tokens = (
        tc.collection("claimTokens")
        .where(filter=firestore.FieldFilter("createdBy", "==", "stagenator"))
        .stream()
    )
    for snap in tokens:
        d = snap.to_dict()
        g = d.get("game")
        if g in summary:
            s = summary[g].setdefault(
                "claims", {"links": 0, "codes_backing": 0, "teared": 0}
            )
            s["links"] += 1
            s["codes_backing"] += len(d.get("codeIds", []))
            s["teared"] += len(d.get("claimed", []))
            # push A/B attribution: which copy variant led to claimed codes
            v = d.get("variant")
            if v in ("a", "b"):
                ex = summary[g].setdefault("experiment", {})
                row = ex.setdefault(v, {"sends": 0, "claims": 0})
                row["sends"] += 1
                row["claims"] += len(d.get("claimed", []))
            # NEW claims since the last refresh -> an 'outcome' ledger entry, the
            # learning signal gather_day feeds the Reflector and the dashboard's
            # "results so far" counts. Per-token deltas stay correct when other
            # (expired) tokens are cleaned up; a claim is ledgered exactly once.
            token_claims[snap.id] = len(d.get("claimed", []))
            new = token_claims[snap.id] - int(prev_counts.get(snap.id, 0))
            if new > 0:
                state.ledger(
                    "outcome",
                    g,
                    action="codes_claimed",
                    count=new,
                    channel=d.get("kind"),
                    variant=d.get("variant"),
                )
    ref.set({"games": summary, "token_claims": token_claims, "updated": state.now()})


def _revenue(
    prop: str, start_date: str, end_date: str = "today"
) -> tuple[float, float, int]:
    """(totalRevenue, purchaseRevenue, activeUsers) for start_date..end_date. Raises on GA error."""
    req = RunReportRequest(
        property=f"properties/{prop}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        metrics=[
            Metric(name="totalRevenue"),
            Metric(name="purchaseRevenue"),
            Metric(name="activeUsers"),
        ],
    )
    rows = ga().run_report(req).rows
    if not rows:
        return (0.0, 0.0, 0)
    m = rows[0].metric_values
    return (float(m[0].value), float(m[1].value), int(m[2].value))


def refresh_daily_history() -> None:
    """30-day per-day history for the dashboard chart: players + revenue per game
    (GA4 daily report — backfills the past month in one query) and the agent's own
    actions/errors per day (from the ledger). Recomputed idempotently; throttled by
    the caller. Powers the 'is it working' trend view."""

    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    from agent import state

    doc_ref = state.db().collection(config.COL_PLAYBOOK).document("daily_history")
    days: dict[str, dict] = {}

    for game in config.ACTIVE_GAMES:
        prop = config.GAMES[game]["ga_property"]
        try:
            req = RunReportRequest(
                property=f"properties/{prop}",
                dimensions=[Dimension(name="date")],
                metrics=[
                    Metric(name="activeUsers"),
                    Metric(name="totalRevenue"),
                    Metric(name="userEngagementDuration"),
                ],
                date_ranges=[DateRange(start_date="30daysAgo", end_date="today")],
            )
            for r in ga().run_report(req).rows:
                d = r.dimension_values[0].value  # YYYYMMDD
                key = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                day = days.setdefault(key, {})
                g = day.setdefault(game, {})
                players = int(float(r.metric_values[0].value or 0))
                g["players"] = players
                g["revenue_usd"] = round(float(r.metric_values[1].value or 0), 2)
                secs = float(r.metric_values[2].value or 0)
                g["engagement_min"] = round(secs / 60 / players, 1) if players else 0.0
        except Exception as e:
            log.warning("daily history GA failed for %s: %s", game, e)

    # agent's own activity per day (ledger keeps 30d)
    for e in state.recent_ledger(hours=24 * 30):
        ts = e.get("ts")
        if not ts:
            continue
        key = f"{ts:%Y-%m-%d}"
        day = days.setdefault(key, {})
        agg = day.setdefault("_agent", {"actions": 0, "errors": 0})
        # only PLAYER-FACING actions mark an "agent acted" day — daily maintenance
        # (audits, housekeeping, restock polls) runs every day and would paint every
        # square green, destroying the chart's action→effect reading
        if (
            e.get("kind") == "action"
            and e.get("status") == "done"
            and e.get("action") in ("level_pipeline", "code_drop", "individual_code", "level_push")
        ):
            agg["actions"] += 1
            # per-game count so the dashboard's per-game chart can mark only
            # the days the agent acted on THAT game
            game = e.get("game")
            if game:
                g = day.setdefault(game, {})
                g["actions"] = int(g.get("actions") or 0) + 1
        elif e.get("kind") == "error":
            agg["errors"] += 1

    doc_ref.set({"days": days, "updated": state.now()})


def refresh_earnings() -> None:
    """Per-game EARNINGS the agent can actually move. YESTERDAY (the last COMPLETE day) is
    the headline — 'today' is partial and ad revenue lags — with 7d and 30d for trend. From
    GA4 revenue (purchases + subs + ads); lifetime is excluded (pre-agent, not actionable).
    Honest $0 when there's no recent revenue."""
    from agent import state

    out: dict = {}
    for game in config.ACTIVE_GAMES:
        prop = config.GAMES[game]["ga_property"]
        try:
            yday, _purch_y, users_y = _revenue(prop, "yesterday", "yesterday")
            d7, _, _ = _revenue(prop, "7daysAgo")
            d30, _, users30 = _revenue(prop, "30daysAgo")
            out[game] = {
                "yesterday_usd": round(yday, 2),
                "d7_usd": round(d7, 2),
                "d30_usd": round(d30, 2),
                "yesterday_arpu": round(yday / users_y, 3) if users_y else 0.0,
                "arpu_30d": round(d30 / users30, 3) if users30 else 0.0,
                "status": (
                    "live"
                    if d30 > 0
                    else "no recent revenue — quiet / not instrumented"
                ),
            }
        except Exception as e:
            out[game] = {"status": f"unavailable: {e}"[:140]}
    state.db().collection(config.COL_PLAYBOOK).document("earnings").set(
        {"games": out, "updated": state.now()}
    )


def refresh_audience_profile() -> None:
    """Per-game audience BREAKDOWN from the GA4 BigQuery export (last 14d): top countries
    with player counts, engagement time, platform split, and lapsing counts. This is RAW
    context handed to the Strategist to interpret — no value judgement is imposed here
    (the model weighs country/engagement/recency itself). Degrades to 'awaiting export'."""
    from google.cloud import bigquery

    from agent import state

    bq = bigquery.Client(project=config.HOME_PROJECT)
    out: dict = {}
    for game in config.ACTIVE_GAMES:
        prop = config.GAMES[game]["ga_property"]
        table = f"`{config.HOME_PROJECT}.analytics_{prop}.events_*`"
        try:
            q = f"""
              WITH p AS (
                SELECT user_pseudo_id AS u,
                  ANY_VALUE(geo.country) AS country,
                  ANY_VALUE(platform) AS platform,
                  SUM((SELECT ep.value.int_value FROM UNNEST(event_params) ep
                       WHERE ep.key='engagement_time_msec'))/1000 AS engage_sec,
                  MAX(event_timestamp) AS last_ts
                FROM {table}
                WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY))
                GROUP BY u )
              SELECT country,
                COUNT(*) AS players,
                ROUND(AVG(engage_sec), 0) AS avg_engage_sec,
                COUNTIF(platform='IOS') AS ios,
                COUNTIF(platform='ANDROID') AS android,
                COUNTIF(last_ts < UNIX_MICROS(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 DAY))) AS lapsing
              FROM p GROUP BY country ORDER BY players DESC LIMIT 12
            """
            rows = list(bq.query(q).result())
        except Exception as e:
            log.warning("audience profile failed for %s: %s", game, e)
            out[game] = {"status": "awaiting GA4 export"}
            continue
        countries = [
            {
                "country": r["country"] or "unknown",
                "players": r["players"],
                "avg_engage_sec": r["avg_engage_sec"] or 0,
                "ios": r["ios"],
                "android": r["android"],
                "lapsing": r["lapsing"] or 0,
            }
            for r in rows
        ]
        out[game] = {
            "players_14d": sum(c["players"] for c in countries),
            "top_countries": countries,
        }
    # Tier-1 reachability: installs the agent can address 1:1 (per-uid push
    # tokens the game app registers). Independent of GA; appears per game as
    # soon as its fcm_token_collections are configured.
    for game in config.ACTIVE_GAMES:
        cols = config.GAMES[game].get("fcm_token_collections") or []
        entry = out.setdefault(game, {})
        if not cols:
            entry["reachable"] = {"note": "no per-user tokens yet (tier 0)"}
            continue
        try:
            gdb = state.game_db(game)
            week_ago = state.now() - dt.timedelta(days=7)
            registered = active_7d = 0
            for col in cols:
                for snap in gdb.collection(col).limit(500).stream():
                    d = snap.to_dict() or {}
                    if not d.get("token"):
                        continue
                    registered += 1
                    ts = d.get("updatedAt")
                    if ts is not None and ts >= week_ago:
                        active_7d += 1
            entry["reachable"] = {
                "registered_installs": registered,
                "active_7d": active_7d,
            }
        except Exception as e:
            log.warning("reachability failed for %s: %s", game, e)
    state.db().collection(config.COL_PLAYBOOK).document("audience").set(
        {"games": out, "updated": state.now()}
    )


def refresh_push_outcomes() -> None:
    """Push EFFECTIVENESS from GA4: notification_receive / _open / _dismiss counts per game
    (30d) with an open rate. The real 'did the push land?' outcome the Reflector learns from.
    Populates once the analytics_label sends accrue events and users opt in; degrades honestly."""
    from agent import state

    out: dict = {}
    for game in config.ACTIVE_GAMES:
        prop = config.GAMES[game]["ga_property"]
        req = RunReportRequest(
            property=f"properties/{prop}",
            date_ranges=[DateRange(start_date="30daysAgo", end_date="today")],
            dimensions=[Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
        )
        try:
            counts = {
                r.dimension_values[0].value: int(r.metric_values[0].value)
                for r in ga().run_report(req).rows
            }
            recv = counts.get("notification_receive", 0)
            opened = counts.get("notification_open", 0)
            dism = counts.get("notification_dismiss", 0)
            out[game] = {
                "received": recv,
                "opened": opened,
                "dismissed": dism,
                "open_rate": round(opened / recv, 3) if recv else None,
                "status": (
                    "live"
                    if recv
                    else "no notification events yet — pushes are now labelled; accrues as they're sent"
                ),
            }
        except Exception as e:
            out[game] = {"status": f"unavailable: {e}"[:140]}
    state.db().collection(config.COL_PLAYBOOK).document("push_outcomes").set(
        {"games": out, "updated": state.now()}
    )


def _alert_realtime_blind(game: str, err: Exception) -> None:
    """A realtime-query FAILURE = the agent is BLIND to live players. Make it loud NOW
    (email + CRITICAL), not once-daily via the health check. Deduped to ~1/hour/game so
    a persistent outage sends one alert, not one every 5-min pulse."""
    now = state.now()
    ref = state.db().collection(config.COL_PLAYBOOK).document("realtime_alert")
    last = (ref.get().to_dict() or {}).get(game)
    if last and (now - last).total_seconds() < 3600:
        log.warning("GA realtime BLIND for %s (already alerted): %s", game, err)
        return
    log.error("GA realtime BLIND for %s: %s", game, err)
    ref.set({game: now}, merge=True)
    try:
        from agent.tools import mailbox

        mailbox.send_alert(
            f"Stagenator is BLIND to live players — {game}",
            f"The GA4 realtime query for {game} FAILED, so the agent cannot see who is online "
            f"and is silently reading 0 active users.\n\nError: {err}\n\nThis is a realtime "
            f"VISIBILITY OUTAGE, not a quiet period. {now:%Y-%m-%d %H:%M} UTC.\n"
            f"Dashboard: https://stagenator-mission.web.app",
        )
    except Exception as e:
        log.warning("realtime-blind alert email failed: %s", e)
    state.critical(
        f"GA realtime blind for {game}: {err}", _email=False, game=game
    )  # own email above


def _recently_seen() -> set:
    """Signals (one per game) to suppress this pulse. Two rules:

    1h dedup per game-signal — enough to stop the SAME session re-waking Gemini every
    5 min, short enough that a distinct player an hour later is seen again. Delivery
    volume is bounded by the gate's caps, not by this window.

    OUTCOME-AWARE: "seen" is not "served". If the game logged an ERROR after a
    segment's signal (e.g. the level it triggered dead-lettered), that segment is
    UN-MUTED immediately — an unserved player must not stay invisible behind a
    suppression for a delivery that never happened."""
    signals = state.recent_ledger(hours=1, kind="signal")
    errors = state.recent_ledger(hours=1, kind="error")
    last_err: dict = {}
    for e in errors:
        g, ts = e.get("game"), e.get("ts")
        if g and ts and (g not in last_err or ts > last_err[g]):
            last_err[g] = ts
    seen = set()
    for e in signals:
        g, ts = e.get("game"), e.get("ts")
        if g in last_err and ts and last_err[g] > ts:
            continue  # a failure post-dates this signal — segment stays live for a retry
        seen.add((g, e.get("signal"), e.get("detail")))
    return seen


def detect_signals() -> list[dict]:
    """The pulse's entire deterministic brain. Returns only NEW signals."""
    signals: list[dict] = []
    refreshers = [refresh_codes_summary, refresh_cost_summary]
    try:  # daily-history chart data: recompute at most every 6h (2 GA queries)
        from agent import state as _st
        hist = _st.db().collection(config.COL_PLAYBOOK).document("daily_history").get().to_dict()
        stale = not hist or (_st.now() - hist.get("updated", _st.now())).total_seconds() > 6 * 3600
        if stale:
            refreshers.append(refresh_daily_history)
    except Exception:
        pass
    for _fn in refreshers:
        try:
            _fn()
        except Exception as e:
            log.warning("%s failed: %s", _fn.__name__, e)
    seen = _recently_seen()

    for game in config.ACTIVE_GAMES:
        try:
            snapshot = realtime_snapshot(game)
        except (
            Exception
        ) as e:  # realtime FAILURE -> blindness -> alert now, don't read 0 silently
            _alert_realtime_blind(game, e)
            snapshot = {}
        active = sum(snapshot.values())
        if active > 0:
            # ONE signal per game — the breakdown rides along as data, never as a key.
            if (game, "user_active", "user_active") in seen:
                log.info(
                    "signal suppressed (seen<1h, served): %s user_active count=%s",
                    game,
                    active,
                )
            else:
                sig = {
                    "game": game,
                    "signal": "user_active",
                    "detail": "user_active",
                    "count": active,
                    "breakdown": snapshot,
                }
                activity = realtime_activity(game)  # what they're actually doing, live
                if activity.get("total"):
                    sig["events_10min"] = activity["total"]
                    sig["top_events"] = [f"{n} ({c})" for n, c in activity["top"]]
                signals.append(sig)

        inv = campaign_inventory(game)
        if (
            inv["campaign_id"]
            and inv["available"] is not None
            and inv["available"] <= 5
        ):
            detail = inv[
                "campaign_id"
            ]  # stable — don't re-fire on each stock decrement
            if (game, "inventory_low", detail) not in seen:
                signals.append(
                    {"game": game, "signal": "inventory_low", **inv, "detail": detail}
                )

    for s in signals:
        s["ledger_id"] = state.ledger(
            "signal",
            s["game"],
            signal=s["signal"],
            detail=s.get("detail"),
            count=s.get(
                "count"
            ),  # top-level so hourly-activity aggregation can read it
            data=s,
        )
    return signals
