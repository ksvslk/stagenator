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


def campaign_inventory(game: str, platform: str | None = None) -> dict:
    """Per-platform availableCodes on the game's adopted proffer.codes campaigns.

    Returns {"campaigns": {platform: {campaign_id, available}}, "campaign_id", "available"}
    where the top-level pair reflects the requested platform (or the lowest-stock
    one, which is what replenish cares about).
    """
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    q = tc.collection("campaigns").where(
        filter=firestore.FieldFilter("managedBy", "==", "stagenator")
    ).where(filter=firestore.FieldFilter("game", "==", game))
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


def refresh_codes_summary() -> None:
    """Dashboard-readable summary of code stock + claims via agent links.

    Lives under the playbook path so existing rules cover it. Counts come from
    the agent's own claimTokens (tears via stagenator links) + campaign stock."""
    from agent import state

    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    summary: dict = {}
    for game in config.GAMES:
        inv = campaign_inventory(game)
        summary[game] = {"stock": inv["campaigns"]}
    tokens = tc.collection("claimTokens").where(
        filter=firestore.FieldFilter("createdBy", "==", "stagenator")
    ).stream()
    for snap in tokens:
        d = snap.to_dict()
        g = d.get("game")
        if g in summary:
            s = summary[g].setdefault("claims", {"links": 0, "codes_backing": 0, "teared": 0})
            s["links"] += 1
            s["codes_backing"] += len(d.get("codeIds", []))
            s["teared"] += len(d.get("claimed", []))
    state.db().collection(config.COL_PLAYBOOK).document("codes_summary").set(
        {"games": summary, "updated": state.now()}
    )


def detect_signals() -> list[dict]:
    """The pulse's entire deterministic brain. Returns only NEW signals."""
    signals: list[dict] = []
    try:
        from agent.pipelines.codes import restore_banner_if_expired
        restore_banner_if_expired()
    except Exception as e:  # noqa: BLE001
        log.warning("banner restore check failed: %s", e)
    try:
        refresh_codes_summary()
    except Exception as e:  # noqa: BLE001 — summary is cosmetic, never blocks a pulse
        log.warning("codes summary refresh failed: %s", e)
    recent = state.recent_ledger(hours=4, kind="signal")
    seen = {(e.get("game"), e.get("signal"), e.get("detail")) for e in recent}

    for game in config.ACTIVE_GAMES:
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
