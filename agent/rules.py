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


def top_country(game: str) -> str | None:
    """Best-effort: the country with the most active users right now. Used ONLY as an
    optional soft hint for localizing a level — returns None on any GA hiccup."""
    prop = config.GAMES[game]["ga_property"]
    req = RunRealtimeReportRequest(
        property=f"properties/{prop}",
        dimensions=[Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
        minute_ranges=[MinuteRange(start_minutes_ago=config.GA_POLL_MINUTES, end_minutes_ago=0)],
    )
    try:
        best, best_n = None, 0
        for r in ga().run_realtime_report(req).rows:
            n = int(r.metric_values[0].value)
            if n > best_n and r.dimension_values[0].value:
                best, best_n = r.dimension_values[0].value, n
        return best
    except Exception as e:
        log.warning("GA country query failed for %s: %s", game, e)
        return None


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


def refresh_cost_summary() -> None:
    """REAL spend from the BigQuery Cloud Billing export (no estimates).
    Covers EVERY project this agent system spans (config.STAGENATOR_PROJECTS):
    each active game's Firebase project plus the home/billing project
    (operation-sunrise) and take-codes. They all bill to one account, so their
    cost lands in one export table keyed by project.id. Sums net cost
    (cost + credits) month-to-date and today, per project and per service.
    If billing export isn't enabled/populated yet, marks the panel awaiting."""
    from google.cloud import bigquery

    from agent import state
    from agent.tools import runpod

    # Live external-service balance (Runpod prepaid) — shown even before GCP
    # billing export populates, so the panel always carries a real number.
    try:
        runpod_balance = runpod.account_balance()
    except Exception as e:  # never let a balance hiccup break cost refresh
        log.warning("runpod balance read failed: %s", e)
        runpod_balance = None

    bq = bigquery.Client(project=config.HOME_PROJECT)
    # Either export flavor works — both carry project.id/service/cost/credits.
    # Prefer standard (v1); fall back to the detailed/resource table (resource_v1).
    std = res = None
    for ds in bq.list_datasets():
        for t in bq.list_tables(ds.dataset_id):
            ref = f"`{config.HOME_PROJECT}.{ds.dataset_id}.{t.table_id}`"
            if t.table_id.startswith("gcp_billing_export_v1_"):
                std = std or ref
            elif t.table_id.startswith("gcp_billing_export_resource_v1_"):
                res = res or ref
    table = std or res

    projects = sorted(config.STAGENATOR_PROJECTS)
    doc = state.db().collection(config.COL_PLAYBOOK).document("cost_summary")
    if not table:
        doc.set({"status": "awaiting billing export (enable in console)",
                 "projects": projects,
                 "runpod_balance_usd": runpod_balance,
                 "runpod_note": "external prepaid service — not in GCP billing",
                 "updated": state.now()})
        return

    q = f"""
      SELECT
        project.id AS project,
        service.description AS service,
        SUM(cost) AS cost,
        SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
        SUM(CASE WHEN DATE(usage_start_time) = CURRENT_DATE() THEN cost ELSE 0 END) AS cost_today
      FROM {table}
      WHERE project.id IN UNNEST(@projects)
        AND DATE(_PARTITIONTIME) >= DATE_TRUNC(CURRENT_DATE(), MONTH)
      GROUP BY project, service
      ORDER BY cost DESC
    """
    job = bq.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("projects", "STRING", projects)]))
    rows = list(job.result())

    by_project: dict[str, dict] = {}
    services_tot: dict[str, float] = {}
    for r in rows:
        net = (r["cost"] or 0) + (r["credits"] or 0)
        p = r["project"] or "unknown"
        bp = by_project.setdefault(p, {"month": 0.0, "today": 0.0})
        bp["month"] += net
        bp["today"] += (r["cost_today"] or 0)
        if abs(net) > 0.0001:
            services_tot[r["service"]] = services_tot.get(r["service"], 0.0) + net

    by_project_out = sorted(
        ({"project": p, "month_usd": round(v["month"], 2), "today_usd": round(v["today"], 2)}
         for p, v in by_project.items()),
        key=lambda x: x["month_usd"], reverse=True)
    services = sorted(
        ({"service": s, "usd": round(u, 4)} for s, u in services_tot.items() if abs(u) > 0.0001),
        key=lambda x: x["usd"], reverse=True)
    month = round(sum(p["month_usd"] for p in by_project_out), 2)
    today = round(sum(p["today_usd"] for p in by_project_out), 2)
    budget_usd = config.MONTHLY_BUDGET_EUR * config.EUR_USD
    doc.set({
        "status": "live",
        "today_usd": today, "month_usd": month,
        "by_project": by_project_out,
        "services": services[:8],
        "projects": projects,
        "budget_usd": round(budget_usd, 2),
        "budget_pct": round(100 * month / budget_usd, 1) if budget_usd else 0,
        "runpod_balance_usd": runpod_balance,
        "runpod_note": "external prepaid service — not in GCP billing",
        "updated": state.now(),
    })


def refresh_impact_summary() -> None:
    """Measurable, validated results from real data — for Mission Control + demo."""
    from google.cloud import firestore

    from agent import state

    led = state.recent_ledger(hours=24 * 14)
    def n(pred):
        return sum(1 for e in led if pred(e))

    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    minted = claimed = expired_found = 0
    for game in config.ACTIVE_GAMES:
        for camp in campaign_inventory(game).get("campaigns", {}).values():
            col = tc.collection("campaigns").document(camp["campaign_id"]).collection("codes")
            for s in col.stream():
                d = s.to_dict()
                if d.get("mintedBy") == "stagenator":
                    minted += 1
                if d.get("isTorn") and str(d.get("tornBy", "")).startswith("stagenator"):
                    claimed += 1
                if d.get("expired"):
                    expired_found += 1

    state.db().collection(config.COL_PLAYBOOK).document("impact").set({
        "functional": {
            "actions_executed": n(lambda e: e["kind"] == "action" and e.get("status") == "done"),
            "decisions": n(lambda e: e["kind"] == "decision"),
            "guardrail_blocks": n(lambda e: e["kind"] == "rejected"),
            "nightly_briefs": n(lambda e: e["kind"] == "brief"),
            "codes_minted": minted,
            "codes_claimed": claimed,
            "dead_codes_quarantined": expired_found,
        },
        "outcome_note": "engagement/retention lift instrumented (per-code funnel, "
                        "GA level outcomes, Reflector evidence) — awaiting user scale",
        "updated": state.now(),
    })


def refresh_codes_summary() -> None:
    """Dashboard-readable summary of code stock + claims via agent links.

    Lives under the playbook path so existing rules cover it. Counts come from
    the agent's own claimTokens (tears via stagenator links) + campaign stock."""
    from agent import state

    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    summary: dict = {}
    for game in config.ACTIVE_GAMES:
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
    for _fn in (refresh_codes_summary, refresh_cost_summary, refresh_impact_summary):
        try:
            _fn()
        except Exception as e:
            log.warning("%s failed: %s", _fn.__name__, e)
    recent = state.recent_ledger(hours=4, kind="signal")
    seen = {(e.get("game"), e.get("signal"), e.get("detail")) for e in recent}

    for game in config.ACTIVE_GAMES:
        snapshot = realtime_snapshot(game)
        active = sum(snapshot.values())
        if active > 0:
            country = top_country(game)
            for key, count in snapshot.items():
                platform, cohort = key.split(":", 1)
                sig = "new_user_active" if cohort.lower() == "new" else "user_active"
                detail = f"{platform}:{count}"
                if (game, sig, detail) not in seen:
                    sigd = {"game": game, "signal": sig, "detail": detail,
                            "platform": platform, "count": count}
                    if country:
                        sigd["country"] = country
                    signals.append(sigd)

        inv = campaign_inventory(game)
        if inv["campaign_id"] and inv["available"] is not None and inv["available"] <= 5:
            detail = f"{inv['campaign_id']}:{inv['available']}"
            if (game, "inventory_low", detail) not in seen:
                signals.append({"game": game, "signal": "inventory_low", **inv, "detail": detail})

    for s in signals:
        s["ledger_id"] = state.ledger("signal", s["game"], signal=s["signal"], detail=s.get("detail"), data=s)
    return signals
