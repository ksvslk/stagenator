"""Firestore state layer: decision ledger, task queue, playbook, directives.

This is Stagenator's durable substrate. Every observation, decision, action and
outcome lands here; the dashboard reads it live; crashed runs resume from it.

Design rules:
- Tasks are idempotent documents: (idempotency_key) dedupes across overlapping
  pulses and crash-retries. A task is claimed by a lease, not deleted.
- 3 failed attempts -> status "dead" (dead-letter) + CRITICAL log.
- The playbook is one document ("current") so the Strategist reads one snapshot
  and the dashboard can diff versions (history subcollection).
"""

import datetime as dt
import hashlib
import json
import logging
import secrets
from typing import Any

from google.cloud import firestore

from agent import config

log = logging.getLogger("stagenator.state")
_db: firestore.Client | None = None


def db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.HOME_PROJECT)
    return _db


def game_db(game: str) -> firestore.Client:
    return firestore.Client(project=config.GAMES[game]["project"])


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------- ledger ----

def ledger(kind: str, game: str | None = None, **fields: Any) -> str:
    """Append an entry to the decision ledger. Returns doc id."""
    doc = {
        "ts": now(),
        "kind": kind,  # signal | decision | action | outcome | rejected | error | brief
        "game": game,
        **fields,
    }
    ref = db().collection(config.COL_LEDGER).document()
    ref.set(doc)
    return ref.id


def ledger_update(doc_id: str, **fields: Any) -> None:
    db().collection(config.COL_LEDGER).document(doc_id).set(fields, merge=True)


def recent_ledger(hours: int = 24, kind: str | None = None) -> list[dict]:
    # Single-field ts query only (no composite index needed); kind filtered in
    # memory — the 24h ledger is small by construction (caps bound the volume).
    q = (
        db()
        .collection(config.COL_LEDGER)
        .where(filter=firestore.FieldFilter("ts", ">=", now() - dt.timedelta(hours=hours)))
    )
    entries = [d.to_dict() | {"id": d.id} for d in q.stream()]
    return [e for e in entries if e.get("kind") == kind] if kind else entries


# ------------------------------------------------------------- task queue ----

def idempotency_key(task_type: str, game: str, payload: dict) -> str:
    basis = json.dumps({"t": task_type, "g": game, "p": payload}, sort_keys=True, default=str)
    return hashlib.sha256(basis.encode()).hexdigest()[:24]


def enqueue(task_type: str, game: str, payload: dict, dedupe_key: str | None = None) -> str | None:
    """Create a pending task unless an identical one already exists (idempotent).

    Returns the task id, or None if deduped.
    """
    key = dedupe_key or idempotency_key(task_type, game, payload)
    ref = db().collection(config.COL_TASKS).document(key)

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        snap = ref.get(transaction=tx)
        if snap.exists:
            return None
        tx.set(
            ref,
            {
                "type": task_type,
                "game": game,
                "payload": payload,
                "status": "pending",
                "attempts": 0,   # claim count (shown on dashboard)
                "failures": 0,   # genuine failures — only this drives dead-lettering
                "created": now(),
                "updated": now(),
            },
        )
        return key

    result = _tx(db().transaction())
    if result:
        ledger("action", game, action=task_type, task=key, status="enqueued", payload=payload)
    return result


def claim_pending(limit: int = 10) -> list[dict]:
    """Lease pending tasks (also re-leases stale 'running' ones older than 15 min)."""
    tasks: list[dict] = []
    col = db().collection(config.COL_TASKS)
    stale = now() - dt.timedelta(minutes=15)
    pending = col.where(filter=firestore.FieldFilter("status", "==", "pending")).limit(limit).stream()
    # single-field query; staleness filtered in memory (running set is small)
    running = col.where(filter=firestore.FieldFilter("status", "==", "running")).limit(limit).stream()
    stuck = [s for s in running if (s.to_dict() or {}).get("updated") and s.to_dict()["updated"] < stale]
    for snap in list(pending) + stuck:
        ref = col.document(snap.id)

        lease = secrets.token_hex(8)

        @firestore.transactional
        def _claim(tx: firestore.Transaction, ref=ref, lease=lease):
            cur = ref.get(transaction=tx).to_dict()
            if not cur:  # deleted between stream and tx
                return None
            upd = cur.get("updated")
            if cur.get("status") == "running" and upd and upd > stale:
                return None  # a live owner still holds it
            # dead-letter on genuine FAILURES only — infra kills (which bump claims,
            # not failures) must never dead-letter a task that never actually failed.
            if cur.get("failures", 0) >= config.MAX_TASK_ATTEMPTS:
                tx.update(ref, {"status": "dead", "updated": now()})
                return {"dead": True, **cur, "id": ref.id}
            tx.update(ref, {"status": "running", "attempts": cur.get("attempts", 0) + 1,
                            "lease": lease, "leasedAt": now(), "updated": now()})
            return {**cur, "id": ref.id, "attempts": cur.get("attempts", 0) + 1, "lease": lease}

        claimed = _claim(db().transaction())
        if claimed and claimed.get("dead"):
            critical(f"Task {snap.id} dead-lettered after {config.MAX_TASK_ATTEMPTS} attempts", task=claimed)
        elif claimed:
            tasks.append(claimed)
    return tasks


def defer_task(task_id: str, lease: str | None, reason: str) -> None:
    """Return a claimed task to pending. Lease-checked: if we no longer own the
    lease (a stale re-lease handed it to another invocation) this is a no-op, so
    a defer can't clobber the new owner. Deferral is not a failure — `failures`
    is untouched, so it never contributes to dead-lettering."""
    ref = db().collection(config.COL_TASKS).document(task_id)

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        cur = ref.get(transaction=tx).to_dict()
        if not cur or (lease and cur.get("lease") not in (None, lease)):
            return
        tx.update(ref, {"status": "pending", "updated": now(),
                        "lastDefer": reason, "lease": firestore.DELETE_FIELD})

    _tx(db().transaction())


def finish_task(task_id: str, lease: str | None, ok: bool,
                error: str | None = None, result: dict | None = None) -> None:
    """Record a task outcome. Lease-checked so a stale duplicate can't overwrite the
    real owner's result. A genuine failure increments `failures`; dead-lettering is
    driven by `failures` (not claim count), so infra kills don't dead-letter."""
    ref = db().collection(config.COL_TASKS).document(task_id)

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        cur = ref.get(transaction=tx).to_dict()
        if not cur or (lease and cur.get("lease") not in (None, lease)):
            return False  # we no longer own it — don't clobber the new owner
        if ok:
            tx.update(ref, {"status": "done", "updated": now(),
                            "result": result or {}, "lease": firestore.DELETE_FIELD})
            return False
        failures = cur.get("failures", 0) + 1
        dead = failures >= config.MAX_TASK_ATTEMPTS
        tx.update(ref, {"status": "dead" if dead else "pending", "updated": now(),
                        "error": error, "failures": failures, "lease": firestore.DELETE_FIELD})
        return dead

    dead = _tx(db().transaction())
    if dead:
        critical(f"Task {task_id} dead-lettered: {error}", task_id=task_id)


# --------------------------------------------------------------- playbook ----

DEFAULT_PLAYBOOK: dict = {
    "version": 1,
    "philosophy": (
        "Very few users right now: every single active player matters. React to any "
        "activity. Prefer shipping fresh levels (free, always welcome) over codes "
        "(scarce). Codes go to returning/lapsing players, not already-engaged ones."
    ),
    "knobs": {
        "code_send_windows_utc": [{"start": 16, "end": 21}],  # learned over time; no nested arrays (Firestore)
        "min_days_inactive_for_code": 3,
        "level_cadence_per_game_days": 2,
    },
    "segment_rules": [
        {"id": "new-user-welcome", "when": "first_open detected", "action": "ensure fresh level live; note for D1 follow-up"},
        {"id": "lapsed-return", "when": "user active after >=3 days away", "action": "consider code drop for their game"},
    ],
    "capability_tiers": {g: config.GAMES[g]["tier"] for g in config.ACTIVE_GAMES},
    "ceo_directives": [],
    "evidence": {},  # rule_id -> weak|strong + notes, maintained by Reflector
}


def get_playbook() -> dict:
    ref = db().collection(config.COL_PLAYBOOK).document("current")
    snap = ref.get()
    if not snap.exists:
        doc = DEFAULT_PLAYBOOK | {"updated": now()}
        ref.set(doc)
        return doc
    return snap.to_dict()


def update_playbook(new_doc: dict, reason: str) -> None:
    ref = db().collection(config.COL_PLAYBOOK).document("current")
    old = ref.get()
    if old.exists:
        ref.collection("history").document().set(old.to_dict() | {"archived": now(), "reason": reason})
    new_doc["version"] = (old.to_dict() or {}).get("version", 0) + 1 if old.exists else 1
    new_doc["updated"] = now()
    ref.set(new_doc)
    ledger("decision", None, action="playbook_update", reason=reason, version=new_doc["version"])


# -------------------------------------------------------------- directives ----

def pending_directives() -> list[dict]:
    q = db().collection(config.COL_DIRECTIVES).where(
        filter=firestore.FieldFilter("status", "==", "new")
    )
    return [d.to_dict() | {"id": d.id} for d in q.stream()]


def resolve_directive(directive_id: str, response: str) -> None:
    db().collection(config.COL_DIRECTIVES).document(directive_id).update(
        {"status": "handled", "response": response, "handled_at": now()}
    )


# ---------------------------------------------------------------- alerts ----

def critical(message: str, **context: Any) -> None:
    """Emit a severity=CRITICAL structured log -> Cloud Monitoring email alert."""
    payload = {"alert": "stagenator", "message": message, **{k: str(v)[:500] for k, v in context.items()}}
    log.critical(json.dumps(payload))


def heartbeat(run_kind: str) -> None:
    """INFO heartbeat log (absence trips the missing-heartbeat alert) + a
    dashboard-readable doc (lives under the playbook path so the already-
    deployed owner-read rules cover it)."""
    log.info(json.dumps({"heartbeat": "stagenator", "run": run_kind, "ts": now().isoformat()}))
    try:
        db().collection(config.COL_PLAYBOOK).document("heartbeat").set(
            {"kind": run_kind, "at": now()}, merge=True
        )
    except Exception as e:
        log.warning("heartbeat doc write failed: %s", e)
