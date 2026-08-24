"""Signal detection — deterministic code, no LLM.

Each pulse: poll GA realtime for all games, read code inventory, diff against
the ledger, and emit only *new* signals. If this returns an empty list, the
pulse exits without ever invoking Gemini (a zero-cost tick).
"""

import logging

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    Dimension,
    Metric,
    MinuteRange,
    RunRealtimeReportRequest,
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
    """Active users in the poll window, split new vs established, per platform."""
    prop = config.GAMES[game]["ga_property"]
    req = RunRealtimeReportRequest(
        property=f"properties/{prop}",
        dimensions=[Dimension(name="platform"), Dimension(name="newVsReturning")],
        metrics=[Metric(name="activeUsers")],
        minute_ranges=[MinuteRange(start_minutes_ago=config.GA_POLL_MINUTES, end_minutes_ago=0)],
    )
    rows = {}
    try:
        resp = ga().run_realtime_report(req)
        for r in resp.rows:
            platform = r.dimension_values[0].value or "unknown"
            cohort = r.dimension_values[1].value or "unknown"
            rows[f"{platform}:{cohort}"] = int(r.metric_values[0].value)
    except Exception as e:  # GA hiccup: log, return empty — next pulse overlaps
        log.warning("GA realtime failed for %s: %s", game, e)
    return rows


def campaign_inventory(game: str) -> dict:
    """availableCodes on the game's proffer.codes campaign (0 docs -> unknown)."""
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    q = tc.collection("campaigns").where(
        filter=firestore.FieldFilter("managedBy", "==", "stagenator")
    ).where(filter=firestore.FieldFilter("game", "==", game))
    for snap in q.stream():
        d = snap.to_dict()
        return {"campaign_id": snap.id, "available": d.get("availableCodes", 0), "platform": d.get("platform")}
    return {"campaign_id": None, "available": None}


def detect_signals() -> list[dict]:
    """The pulse's entire deterministic brain. Returns only NEW signals."""
    signals: list[dict] = []
    recent = state.recent_ledger(hours=4, kind="signal")
    seen = {(e.get("game"), e.get("signal"), e.get("detail")) for e in recent}

    for game in config.GAMES:
        snapshot = realtime_snapshot(game)
        active = sum(snapshot.values())
        if active > 0:
            for key, count in snapshot.items():
                platform, cohort = key.split(":", 1)
                sig = "new_user_active" if cohort.lower() == "new" else "user_active"
                detail = f"{platform}:{count}"
                if (game, sig, detail) not in seen:
                    signals.append(
                        {"game": game, "signal": sig, "detail": detail,
                         "platform": platform, "count": count}
                    )

        inv = campaign_inventory(game)
        if inv["campaign_id"] and inv["available"] is not None and inv["available"] <= 5:
            detail = f"{inv['campaign_id']}:{inv['available']}"
            if (game, "inventory_low", detail) not in seen:
                signals.append({"game": game, "signal": "inventory_low", **inv, "detail": detail})

    for s in signals:
        s["ledger_id"] = state.ledger("signal", s["game"], signal=s["signal"], detail=s.get("detail"), data=s)
    return signals
