"""Stagenator root agent — an ADK 2.0 Workflow graph.

One graph serves all trigger kinds; START input routes it:
  "pulse"     -> detect (code) -> [signals? strategist -> gate -> execute : idle]
  "nightly"   -> gather 24h context -> reflector -> apply playbook + brief
  "replenish" -> inventory/balances check -> replenish pipeline tasks -> execute
  "event:..." -> Eventarc fast path: treat as pulse with the event as a signal

LLM nodes: exactly two (strategist, reflector). Everything else is code.
"""

import json
import logging

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
    if kind not in ("pulse", "nightly", "replenish"):
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


def execute(node_input) -> dict:
    """Drain the task queue through the executor pipelines. Fault-isolated per task."""
    from agent import pipelines

    ran, failed = [], []
    for task in state.claim_pending():
        not_before = task.get("payload", {}).get("not_before")
        if not_before and not_before > state.now().isoformat():
            state.finish_task(task["id"], ok=False, error="not yet due")  # back to pending
            continue
        try:
            result = pipelines.run_task(task)
            state.finish_task(task["id"], ok=True, result=result)
            state.ledger("action", task["game"], action=task["type"], task=task["id"],
                         status="done", result=result)
            ran.append(task["id"])
        except Exception as e:  # noqa: BLE001 — one bad task never wedges the loop
            log.exception("task %s failed", task["id"])
            state.finish_task(task["id"], ok=False, error=str(e))
            failed.append({"task": task["id"], "error": str(e)})
    return {"ran": ran, "failed": failed, "gate": node_input if isinstance(node_input, dict) else None}


def idle(node_input) -> dict:
    return {"status": "idle", "note": "no signals — zero-cost tick"}


def gather_day(node_input: str) -> str:
    """Nightly path: assemble the Reflector's full context."""
    context = {
        "ledger_24h": state.recent_ledger(hours=24),
        "playbook": state.get_playbook(),
        "directives": state.pending_directives(),
        "inventory": {g: rules.campaign_inventory(g) for g in config.GAMES},
    }
    return json.dumps(context, default=str)


def apply_night(node_input: dict) -> dict:
    result = apply_reflection(node_input)
    for d in state.pending_directives():
        state.resolve_directive(d["id"], response="Folded into tonight's playbook update.")
    return result


def plan_replenish(node_input: str) -> dict:
    """Replenish path: audit first, import any minted CSVs, then escalate shortages."""
    enqueued = []
    for t, g in (("audit_inventory", "all"), ("mint_import", "all")):
        tid = state.enqueue(t, g, {}, dedupe_key=f"{t}-{state.now().date().isoformat()}")
        if tid:
            enqueued.append(tid)
    for game in config.GAMES:
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
    description="Autonomous engagement & retention agent for three mobile games.",
    edges=[
        ("START", dispatch),
        # routed by trigger kind
        (dispatch, {"pulse": detect, "nightly": gather_day, "replenish": plan_replenish}),
        # pulse / eventarc fast path
        (detect, {"decide": strategist, "idle": idle}),
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
