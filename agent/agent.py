"""Stagenator root agent — an ADK 2.0 Workflow graph.

One graph serves all trigger kinds; START input routes it:
  "pulse"     -> detect (code) -> [signals? strategist -> gate -> execute : idle]
  "nightly"   -> gather 24h context -> reflector -> apply playbook + brief
  "replenish" -> inventory/balances check -> replenish pipeline tasks -> execute
  "event:..." -> Eventarc fast path: treat as pulse with the event as a signal

LLM nodes: exactly two (strategist, reflector). Everything else is code.
"""

import datetime as dt
import json
import logging
import time as _time

from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.workflow import Workflow

from agent import config, guardrails, rules, state
from agent.reflector import apply_reflection, reflector
from agent.strategist import strategist

log = logging.getLogger("stagenator.agent")


# ------------------------------------------------------------------ nodes ----

def dispatch(node_input) -> Event:
    """Route by trigger kind (START gives types.Content)."""
    text = ""
    try:
        text = node_input.parts[0].text or ""
    except (AttributeError, IndexError):
        text = str(node_input)
    kind = text.strip().split(":", 1)[0] or "pulse"
    state.heartbeat(kind)
    if kind not in ("pulse", "nightly", "replenish", "health"):
        kind = "pulse"  # eventarc events ride the pulse path; detect() sees the payload
    return Event(output=text.strip(), route=kind, state={"trigger": text.strip()})


def detect(node_input: str) -> Event:
    """Pulse path: deterministic signal detection. No signals -> no LLM."""
    signals = rules.detect_signals()
    if node_input.startswith("event:"):
        # Eventarc fast path: the event itself is a signal
        try:
            payload = json.loads(node_input.split(":", 1)[1])
        except json.JSONDecodeError:
            payload = {"raw": node_input}
        payload.setdefault("signal", "eventarc")
        payload["ledger_id"] = state.ledger(
            "signal", payload.get("game"), signal=payload["signal"], data=payload
        )
        signals.append(payload)
    if not signals:
        return Event(output="idle", route="idle")
    context = {
        "signals": signals,
        "playbook": state.get_playbook(),
        "health": state.health_status(),  # which dependencies are down — don't act on a broken one
        "audience": state.audience_profile(),  # per-segment value: country tier, engagement, lapsing
        "recent_actions": [
            {k: str(v) for k, v in e.items() if k in ("ts", "game", "action", "status", "reason")}
            for e in state.recent_ledger(hours=24, kind="action")
        ],
        "directives": state.pending_directives(),
    }
    return Event(output=json.dumps(context, default=str), route="decide")


def gate(node_input: dict) -> dict:
    """Validate the Strategist's structured Decision, enqueue what passes."""
    result = guardrails.gate_and_enqueue(node_input)
    # every decision is visible — including deliberate inaction
    state.ledger(
        "decision", None, action="strategist",
        actions=len(node_input.get("actions", [])),
        enqueued=len(result["enqueued"]), rejected=len(result["rejected"]),
        notes=str(node_input.get("notes", ""))[:500],
    )
    for d in state.pending_directives():
        responses = [
            r for a in node_input.get("actions", []) for r in (a.get("directive_responses") or [])
        ]
        if responses:
            state.resolve_directive(d["id"], response=" ".join(responses))
    return result


# Long-running generation (Veo ~2min, Runpod ~1-2min) runs inside the
# scheduler-triggered HTTP handler, which has a 540s deadline. Bound each
# invocation well under that: stop claiming new work past the soft budget and
# process at most one heavy generation per invocation. Remaining tasks stay
# pending and drain on the next pulse (every 5 min) — the queue self-paces.
EXECUTOR_SOFT_BUDGET_S = 420
HEAVY_TASKS = {"level_pipeline"}


def execute(node_input) -> dict:
    """Drain the task queue, time-bounded so one trigger can't exceed the deadline."""
    from agent import pipelines

    ran, failed, deferred = [], [], 0
    started = _time.monotonic()
    heavy_done = 0
    for task in state.claim_pending():
        # stop before the deadline; leave the rest pending for the next pulse
        if _time.monotonic() - started > EXECUTOR_SOFT_BUDGET_S or (
            task["type"] in HEAVY_TASKS and heavy_done >= 1
        ):
            state.defer_task(task["id"], task.get("lease"), "budget/heavy-cap")  # not a failed attempt
            deferred += 1
            continue
        not_before = task.get("payload", {}).get("not_before")
        if not_before:
            try:
                nb = dt.datetime.fromisoformat(str(not_before))
                if nb.tzinfo is None:
                    nb = nb.replace(tzinfo=dt.UTC)
                due = state.now() < nb
            except (ValueError, TypeError):
                due = False  # unparseable schedule -> run now rather than crash/never
            if due:
                state.defer_task(task["id"], task.get("lease"), "not yet due")  # scheduled, not a failure
                continue
        try:
            result = pipelines.run_task(task)
            state.finish_task(task["id"], task.get("lease"), ok=True, result=result)
            state.ledger("action", task["game"], action=task["type"], task=task["id"],
                         status="done", result=result)
            ran.append(task["id"])
            if task["type"] in HEAVY_TASKS:
                heavy_done += 1
        except Exception as e:
            log.exception("task %s failed", task["id"])
            state.finish_task(task["id"], task.get("lease"), ok=False, error=str(e))
            failed.append({"task": task["id"], "error": str(e)})
    return {"ran": ran, "failed": failed, "deferred": deferred,
            "gate": node_input if isinstance(node_input, dict) else None}


def idle(node_input) -> Event:
    result = {"status": "idle", "note": "no signals — zero-cost tick"}
    # message gives the run a text-bearing event (eval harness + web UI need one)
    return Event(output=result, message="idle — no signals; zero-cost tick")


def gather_day(node_input: str) -> str:
    """Nightly path: assemble a COMPACT, aggregated context for the Reflector.

    Bounded regardless of volume: actions aggregated to counts, outcomes kept in
    full (they're what learning needs), verbose fields (media URLs, prompts) and
    the Reflector's own past briefs dropped."""
    for _fn in (rules.refresh_audience_profile, rules.refresh_earnings):  # nightly GA4 refreshes
        try:
            _fn()
        except Exception as e:
            log.warning("%s failed: %s", _fn.__name__, e)
    led = state.recent_ledger(hours=24)
    action_counts: dict[str, int] = {}
    rejections: list[str] = []
    outcomes: list[dict] = []
    signal_counts: dict[str, int] = {}
    for e in led:
        kind = e.get("kind")
        if kind == "action" and e.get("status") == "done":
            key = f"{e.get('game')}:{e.get('action')}"
            action_counts[key] = action_counts.get(key, 0) + 1
        elif kind == "rejected":
            rejections.append(f"{e.get('game')}:{e.get('action')} — {e.get('reason')}")
        elif kind == "outcome":  # claims, redemptions, retention movement — keep in full
            outcomes.append({k: v for k, v in e.items() if k not in ("id",)})
        elif kind == "signal":
            s = e.get("signal", "?")
            signal_counts[s] = signal_counts.get(s, 0) + 1
    # when are players actually active? (last 7d, by UTC hour) — lets the Reflector
    # learn peak send windows at scale, so sends land when the MOST players are online.
    hourly: dict[str, int] = {}
    for e in state.recent_ledger(hours=24 * 7, kind="signal"):
        if str(e.get("signal", "")).endswith("user_active"):
            t = e.get("ts")
            if t is not None and hasattr(t, "hour"):
                h = str(t.hour)
                hourly[h] = hourly.get(h, 0) + int(e.get("count", 1) or 1)
    activity_by_hour = dict(sorted(hourly.items(), key=lambda x: int(x[0])))

    context = {
        "period": "last 24h",
        "signals": signal_counts,
        "activity_by_hour_utc": activity_by_hour,
        "actions_taken": action_counts,      # aggregated counts, not raw entries
        "rejections": rejections[:20],
        "outcomes": outcomes,                # the actual results to learn from
        "codes": {g: rules.campaign_inventory(g).get("campaigns", {}) for g in config.ACTIVE_GAMES},
        "earnings": state.earnings(),   # the ultimate goal — weigh engagement against it
        "playbook": state.get_playbook(),
        "directives": state.pending_directives(),
    }
    return json.dumps(context, default=str)


def apply_night(node_input: dict) -> dict:
    result = apply_reflection(node_input)
    for d in state.pending_directives():
        state.resolve_directive(d["id"], response="Folded into tonight's playbook update.")
    return result


def health(node_input) -> dict:
    """Run the full dependency health check and write stagenator_playbook/health."""
    from agent import health as _health
    trigger = "manual"
    try:
        trigger = str(node_input).split(":", 1)[1] if ":" in str(node_input) else "scheduled"
    except Exception:
        pass
    return _health.run_health_checks(trigger=trigger)


def plan_replenish(node_input: str) -> dict:
    """Replenish path: audit first, import any minted CSVs, then escalate shortages."""
    enqueued = []
    for t, g in (("audit_inventory", "all"), ("mint_import", "all"),
                 ("poll_restock_inbox", "all"), ("cleanup_storage", "all")):
        tid = state.enqueue(t, g, {}, dedupe_key=f"{t}-{state.now().date().isoformat()}")
        if tid:
            enqueued.append(tid)
    for game in config.ACTIVE_GAMES:
        inv = rules.campaign_inventory(game)
        if inv["campaign_id"] and (inv["available"] or 0) <= 5:
            t = state.enqueue("replenish_codes", game, {"campaign": inv["campaign_id"]})
            if t:
                enqueued.append(t)
    t = state.enqueue("check_balances", "subliminal-words", {"scope": "runpod"},
                      dedupe_key=f"balances-{state.now().date().isoformat()}")
    if t:
        enqueued.append(t)
    return {"enqueued": enqueued}


# ------------------------------------------------------------------ graph ----

root_agent = Workflow(
    name="stagenator",
    description="Autonomous engagement & retention agent for mobile games.",
    edges=[
        ("START", dispatch),
        # routed by trigger kind
        (dispatch, {"pulse": detect, "nightly": gather_day, "replenish": plan_replenish,
                    "health": health}),
        # pulse / eventarc fast path
        (detect, {"decide": strategist, "idle": execute}),  # idle still drains the queue
        (strategist, gate),
        (gate, execute),
        # nightly learning loop
        (gather_day, reflector),
        (reflector, apply_night),
        # replenish
        (plan_replenish, execute),
    ],
)

app = App(name="agent", root_agent=root_agent)
