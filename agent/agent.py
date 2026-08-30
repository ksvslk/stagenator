"""Stagenator root agent — an ADK 2.0 Workflow graph.

One graph serves all trigger kinds; START input routes it:
  "pulse"     -> detect (code) -> [signals? strategist -> gate : idle] -> execute
  "nightly"   -> gather 24h context -> reflector -> apply playbook + brief
  "replenish" -> inventory/balances check -> replenish pipeline tasks -> execute
  "health"    -> run the full real-dependency health check

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
    if kind not in ("pulse", "nightly", "replenish", "health"):
        if kind:
            log.info("unknown trigger kind %r -> pulse", kind[:40])
        kind = "pulse"
    state.heartbeat(kind)  # after normalization -> bounded run_kind cardinality
    return Event(output=text.strip(), route=kind, state={"trigger": text.strip()})


def detect(node_input) -> Event:
    """Pulse path: deterministic signal detection. No signals -> no LLM."""
    # Mirror live owner directives into the playbook every pulse (cheap, no LLM) so
    # an intervention takes visible effect now, not only after the nightly rewrite.
    state.sync_directives_into_playbook()
    signals = rules.detect_signals()
    if not signals:
        return Event(output="idle", route="idle")
    directives = state.pending_directives()
    # Pre-gate: if every signaled game has all its action doors shut (caps spent /
    # push window saturated) and there is nothing else for the model to address,
    # don't wake it — code already knows the only possible answer is "nothing".
    games = {s.get("game") for s in signals}
    if not directives and None not in games and all(
        not guardrails.doors_open(g) for g in games
    ):
        state.ledger(
            "decision",
            None,
            action="gate",
            actions=0,
            enqueued=0,
            rejected=0,
            notes="players active, but all of today's action budgets are already "
            "used — AI not consulted",
        )
        return Event(output="idle", route="idle")
    context = {
        "now_utc": state.now().strftime("%Y-%m-%d %H:%M UTC (%A)"),
        "signals": signals,
        "playbook": state.get_playbook(),
        "health": state.health_status(),  # which dependencies are down — don't act on a broken one
        "caps": state.effective_caps(),  # today's LIVE budgets (owner overrides applied)
        "audience": state.audience_profile(),  # raw analytics context — info, never a limit
        "recent_actions": [
            {
                k: str(v)
                for k, v in e.items()
                if k in ("ts", "game", "action", "status", "reason")
            }
            for e in state.recent_ledger(hours=24, kind="action")
        ],
        "directives": directives,
    }
    return Event(output=json.dumps(context, default=str), route="decide")


def gate(node_input) -> dict:
    """Validate the Strategist's structured Decision, enqueue what passes."""
    # The decision is made NOW, before any action is adjudicated; stamp the summary
    # row with this so it always orders ahead of the enqueue/rejected rows below it.
    decided_at = state.now()
    # Defensive: if the model output isn't a dict (schema-coercion failure), degrade to
    # a no-op instead of crashing the whole pulse — same restraint as the Reflector.
    if not isinstance(node_input, dict):
        state.ledger(
            "decision",
            None,
            at=decided_at,
            action="strategist",
            actions=0,
            enqueued=0,
            rejected=0,
            notes="malformed strategist output — treated as no-op",
        )
        return {"enqueued": [], "rejected": [], "notes": ""}
    result = guardrails.gate_and_enqueue(node_input)
    # every decision is visible — including deliberate inaction
    state.ledger(
        "decision",
        None,
        at=decided_at,
        action="strategist",
        actions=len(node_input.get("actions", [])),
        enqueued=len(result["enqueued"]),
        rejected=len(result["rejected"]),
        notes=str(node_input.get("notes", ""))[:500],
        ruled_out=[str(r)[:200] for r in (node_input.get("ruled_out") or [])][:6],
    )
    # Resolve ONLY the directives the Strategist actually answered (keyed by id), so
    # unrelated open directives are never silently lost (A13/D16/F10).
    pending_ids = {d["id"] for d in state.pending_directives()}
    answered: dict = {}
    for a in node_input.get("actions", []):
        dr = a.get("directive_responses")
        if isinstance(dr, dict):
            answered.update(dr)
    for did, ans in answered.items():
        if did in pending_ids:
            state.resolve_directive(did, response=str(ans)[:1000])
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
            state.defer_task(
                task["id"], task.get("lease"), "budget/heavy-cap"
            )  # not a failed attempt
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
                state.defer_task(
                    task["id"], task.get("lease"), "not yet due"
                )  # scheduled, not a failure
                deferred += 1
                continue
        try:
            result = pipelines.run_task(task)
            recorded = state.finish_task(
                task["id"], task.get("lease"), ok=True, result=result
            )
            # "done" rows drive the caps — only the run that actually recorded the
            # outcome may write one (a fenced duplicate must not double-count), and a
            # zero-delivery success (e.g. no registered devices) must not burn the
            # day's budget: the player got nothing.
            delivered = not (isinstance(result, dict) and result.get("sent") == 0)
            state.ledger(
                "action",
                task["game"],
                action=task["type"],
                task=task["id"],
                status="done" if (recorded and delivered) else "noop",
                result=result,
            )
            ran.append(task["id"])
            if task["type"] in HEAVY_TASKS:
                heavy_done += 1
        except Exception as e:
            log.exception("task %s failed", task["id"])
            state.finish_task(task["id"], task.get("lease"), ok=False, error=str(e))
            failed.append({"task": task["id"], "error": str(e)})
    # Return TEXT, not a dict: the graph's terminal output is the run's response —
    # an idle pulse must still SAY it did nothing (eval + trigger callers read text).
    return json.dumps(
        {
            "ran": ran,
            "failed": failed,
            "deferred": deferred,
            "gate": node_input if isinstance(node_input, dict) else None,
        },
        default=str,
    )


def idle(node_input) -> Event:
    """No signals: a zero-cost tick. Emits a text-bearing event so idle runs are
    visible to the eval harness and callers, then hands off to execute (queue drain)."""
    return Event(
        output="zero-cost tick — no signals",
        message="idle — no signals; zero-cost tick",
    )


def gather_day(node_input: str) -> str:
    """Nightly path: assemble a COMPACT, aggregated context for the Reflector.

    Bounded regardless of volume: actions aggregated to counts, outcomes kept in
    full (they're what learning needs), verbose fields (media URLs, prompts) and
    the Reflector's own past briefs dropped."""
    for _fn in (
        rules.refresh_audience_profile,
        rules.refresh_earnings,
        rules.refresh_push_outcomes,
    ):  # nightly GA4 refreshes
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
        elif (
            kind == "outcome"
        ):  # claims, redemptions, retention movement — keep in full
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
                hourly[h] = hourly.get(h, 0) + int(
                    e.get("count") or (e.get("data") or {}).get("count") or 1
                )
    activity_by_hour = dict(sorted(hourly.items(), key=lambda x: int(x[0])))

    context = {
        "period": "last 24h",
        "signals": signal_counts,
        "activity_by_hour_utc": activity_by_hour,
        "actions_taken": action_counts,  # aggregated counts, not raw entries
        "rejections": rejections[:20],
        "outcomes": outcomes,  # the actual results to learn from
        "codes": {
            g: rules.campaign_inventory(g).get("campaigns", {})
            for g in config.ACTIVE_GAMES
        },
        "earnings": state.earnings(),  # the ultimate goal — weigh engagement against it
        "push_outcomes": state.push_outcomes(),  # notification open/dismiss — did the pushes land?
        # push copy A/B: sends + claimed codes per variant — learn which style works
        "push_experiments": {
            g: (v.get("experiment") or None)
            for g, v in (state.codes_summary() or {}).items()
        },
        "playbook": state.get_playbook(),
        "directives": state.pending_directives(),
    }
    return json.dumps(context, default=str)


def apply_night(node_input: dict) -> dict:
    # The nightly reflection folds directive GUIDANCE into the playbook, but does NOT
    # mark directives handled — only an explicit Strategist answer (by id) resolves one,
    # so an owner instruction is never silently cleared unaddressed (F10).
    return apply_reflection(node_input)


def health(node_input) -> dict:
    """Run the full dependency health check and write stagenator_playbook/health."""
    from agent import health as _health

    trigger = "manual"
    try:
        trigger = (
            str(node_input).split(":", 1)[1] if ":" in str(node_input) else "scheduled"
        )
    except Exception:
        pass
    return _health.run_health_checks(trigger=trigger)


def plan_replenish(node_input: str) -> dict:
    """Replenish path: audit first, import any minted CSVs, then escalate shortages."""
    enqueued = []
    for t, g in (
        ("audit_inventory", "all"),
        ("mint_import", "all"),
        ("poll_restock_inbox", "all"),
        ("cleanup_storage", "all"),
        ("housekeeping", "all"),
    ):
        tid = state.enqueue(
            t, g, {}, dedupe_key=f"{t}-{state.now().date().isoformat()}"
        )
        if tid:
            enqueued.append(tid)
    for game in config.ACTIVE_GAMES:
        inv = rules.campaign_inventory(game)
        if inv["campaign_id"] and (inv["available"] or 0) <= 5:
            t = state.enqueue("replenish_codes", game, {"campaign": inv["campaign_id"]})
            if t:
                enqueued.append(t)
    t = state.enqueue(
        "check_balances",
        "subliminal-words",
        {"scope": "runpod"},
        dedupe_key=f"balances-{state.now().date().isoformat()}",
    )
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
        (
            dispatch,
            {
                "pulse": detect,
                "nightly": gather_day,
                "replenish": plan_replenish,
                "health": health,
            },
        ),
        # pulse path
        (
            detect,
            {"decide": strategist, "idle": idle},
        ),  # idle still drains the queue
        (idle, execute),  # idle still drains the queue — after saying so
        (strategist, gate),
        (gate, execute),
        # nightly learning loop
        (gather_day, reflector),
        (reflector, apply_night),
        # replenish
        (plan_replenish, execute),
    ],
)

# NOTE: App name MUST equal the agent package directory ("agent/") — ADK resolves
# sessions/eval by it; "stagenator" here would break with "Session not found".
# The Workflow above carries the product name; this carries the module identity.
app = App(name="agent", root_agent=root_agent)
