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
import time
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

_REDACTED = "<redacted-claim-link>"
# Keys whose value alone reconstructs a working claim link (URL = base + /drop|claim/ + id).
_LINK_KEYS = {"drop_id", "token", "claimUrl", "claim_url"}


def redact_claim_links(obj: Any) -> Any:
    """Strip working claim/drop links from anything bound for a world-readable
    collection (ledger + task docs any signed-in user can read). A live drop URL
    or its raw id lets a viewer claim codes ahead of real players; redact the
    link, keep everything else (counts, ids, media URLs) so the feed stays useful.
    Recursive, copy-on-write — never mutates the caller's dict."""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if k in _LINK_KEYS and isinstance(v, str):
                out[k] = _REDACTED
            else:
                out[k] = redact_claim_links(v)
        return out
    if isinstance(obj, list):
        return [redact_claim_links(v) for v in obj]
    if isinstance(obj, str) and ("/drop/" in obj or "/claim/" in obj):
        return _REDACTED
    return obj


def ledger(
    kind: str, game: str | None = None, at: dt.datetime | None = None, **fields: Any
) -> str:
    """Append an entry to the decision ledger. Returns doc id.

    `at` overrides the row's timestamp: a decision is stamped with the moment it
    was MADE (before its actions are adjudicated), so it always orders ahead of
    the enqueue/rejected rows it produces — even though the summary, which needs
    the final counts, is physically written a few ms later."""
    doc = {
        "ts": at or now(),
        "kind": kind,  # signal | decision | action | outcome | rejected | error | brief
        "game": game,
        **redact_claim_links(fields),
    }
    ref = db().collection(config.COL_LEDGER).document()
    ref.set(doc)
    return ref.id


def ledger_update(doc_id: str, **fields: Any) -> None:
    db().collection(config.COL_LEDGER).document(doc_id).set(
        redact_claim_links(fields), merge=True
    )


def recent_ledger(hours: int = 24, kind: str | None = None) -> list[dict]:
    # Single-field ts query only (no composite index needed); kind filtered in
    # memory — the 24h ledger is small by construction (caps bound the volume).
    q = (
        db()
        .collection(config.COL_LEDGER)
        .where(
            filter=firestore.FieldFilter("ts", ">=", now() - dt.timedelta(hours=hours))
        )
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(5000)  # bound reads/memory against a runaway ledger loop
    )
    entries = [d.to_dict() | {"id": d.id} for d in q.stream()]
    return [e for e in entries if e.get("kind") == kind] if kind else entries


# ------------------------------------------------------------- task queue ----

# Types under a 1/day cap: at most ONE active task per (type, game) per day, so the
# key ignores volatile payload (message, delay/not_before, n_codes). This makes two
# concurrent deciders (e.g. an overlapping pulse and a lease retry) collapse to the
# SAME task doc — the atomic enqueue transaction then dedupes them instead of both
# slipping through.
_DAILY_CAPPED_TYPES = {"level_pipeline", "code_drop", "individual_code", "level_push"}


def idempotency_key(task_type: str, game: str, payload: dict) -> str:
    if task_type in _DAILY_CAPPED_TYPES:
        basis = json.dumps({"t": task_type, "g": game}, sort_keys=True)
    else:
        basis = json.dumps(
            {"t": task_type, "g": game, "p": payload}, sort_keys=True, default=str
        )
    return hashlib.sha256(basis.encode()).hexdigest()[:24]


def enqueue(
    task_type: str, game: str, payload: dict, dedupe_key: str | None = None
) -> str | None:
    """Create a pending task unless an identical one already exists (idempotent).

    Returns the task id, or None if deduped.
    """
    key = dedupe_key or idempotency_key(task_type, game, payload)
    ref = db().collection(config.COL_TASKS).document(key)

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        snap = ref.get(transaction=tx)
        if snap.exists:
            cur = snap.to_dict() or {}
            # A previously terminal task (dead-lettered or done) with this key can be
            # retried fresh — otherwise a recurring need (e.g. a code shortage) would be
            # deduped against its own tombstone forever. An active task still dedupes.
            if cur.get("status") in ("dead", "done"):
                tx.update(
                    ref,
                    {
                        "status": "pending",
                        "attempts": 0,
                        "failures": 0,
                        "payload": payload,
                        "updated": now(),
                        "error": firestore.DELETE_FIELD,
                        "lease": firestore.DELETE_FIELD,
                        "sideEffects": firestore.DELETE_FIELD,
                    },
                )
                return key
            return None
        tx.set(
            ref,
            {
                "type": task_type,
                "game": game,
                "payload": payload,
                "status": "pending",
                "attempts": 0,  # claim count (shown on dashboard)
                "failures": 0,  # genuine failures — only this drives dead-lettering
                "created": now(),
                "updated": now(),
            },
        )
        return key

    result = _tx(db().transaction())
    if result:
        ledger(
            "action",
            game,
            action=task_type,
            task=key,
            status="enqueued",
            payload=payload,
        )
    return result


def claim_pending(limit: int = 10) -> list[dict]:
    """Lease pending tasks (also re-leases stale 'running' ones older than 15 min)."""
    tasks: list[dict] = []
    col = db().collection(config.COL_TASKS)

    def _stale_before(task_type: str | None):
        mins = (
            config.HEAVY_TASK_STALE_MIN
            if task_type in config.HEAVY_TASKS
            else config.TASK_STALE_MIN
        )
        return now() - dt.timedelta(minutes=mins)

    pending = (
        col.where(filter=firestore.FieldFilter("status", "==", "pending"))
        .limit(limit)
        .stream()
    )
    # scan a wide running window so a mass crash (>limit stuck) is fully recovered, not
    # just the first few; oldest-updated first so the longest-stuck are re-leased soonest.
    running = (
        col.where(filter=firestore.FieldFilter("status", "==", "running"))
        .limit(100)
        .stream()
    )
    stuck = sorted(
        [
            s
            for s in running
            if (x := (s.to_dict() or {})).get("updated")
            and x["updated"] < _stale_before(x.get("type"))
        ],
        key=lambda s: (s.to_dict() or {}).get("updated"),
    )
    for snap in list(pending) + stuck:
        ref = col.document(snap.id)

        lease = secrets.token_hex(8)

        @firestore.transactional
        def _claim(tx: firestore.Transaction, ref=ref, lease=lease):
            cur = ref.get(transaction=tx).to_dict()
            if not cur:  # deleted between stream and tx
                return None
            upd = cur.get("updated")
            if (
                cur.get("status") == "running"
                and upd
                and upd > _stale_before(cur.get("type"))
            ):
                return None  # a live owner still holds it
            # dead-letter on genuine FAILURES only — infra kills (which bump claims,
            # not failures) must never dead-letter a task that never actually failed.
            if cur.get("failures", 0) >= config.MAX_TASK_ATTEMPTS:
                tx.update(ref, {"status": "dead", "updated": now()})
                return {"dead": True, **cur, "id": ref.id}
            tx.update(
                ref,
                {
                    "status": "running",
                    "attempts": cur.get("attempts", 0) + 1,
                    "lease": lease,
                    "leasedAt": now(),
                    "updated": now(),
                },
            )
            return {
                **cur,
                "id": ref.id,
                "attempts": cur.get("attempts", 0) + 1,
                "lease": lease,
            }

        claimed = _claim(db().transaction())
        if claimed and claimed.get("dead"):
            critical(
                f"Task {snap.id} dead-lettered after {config.MAX_TASK_ATTEMPTS} attempts",
                game=claimed.get("game"),
                task=claimed,
            )
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
        tx.update(
            ref,
            {
                "status": "pending",
                "updated": now(),
                "lastDefer": reason,
                "lease": firestore.DELETE_FIELD,
            },
        )

    _tx(db().transaction())


def finish_task(
    task_id: str,
    lease: str | None,
    ok: bool,
    error: str | None = None,
    result: dict | None = None,
) -> bool:
    """Record a task outcome. Returns True if THIS call recorded it (False = fenced by
    a newer lease — the caller must not write cap-counted ledger rows). Lease-checked so a stale duplicate can't overwrite the
    real owner's result. A genuine failure increments `failures`; dead-lettering is
    driven by `failures` (not claim count), so infra kills don't dead-letter."""
    ref = db().collection(config.COL_TASKS).document(task_id)

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        cur = ref.get(transaction=tx).to_dict()
        if not cur or (lease and cur.get("lease") not in (None, lease)):
            return None  # fenced — we no longer own it; don't clobber the new owner
        if ok:
            tx.update(
                ref,
                {
                    "status": "done",
                    "updated": now(),
                    # task docs are world-readable — strip live claim links
                    "result": redact_claim_links(result or {}),
                    "lease": firestore.DELETE_FIELD,
                },
            )
            return ("done", cur.get("game"))
        failures = cur.get("failures", 0) + 1
        dead = failures >= config.MAX_TASK_ATTEMPTS
        tx.update(
            ref,
            {
                "status": "dead" if dead else "pending",
                "updated": now(),
                "error": error,
                "failures": failures,
                "lease": firestore.DELETE_FIELD,
            },
        )
        return ("dead" if dead else "retry", cur.get("game"))

    outcome = _tx(db().transaction())
    if outcome is None:
        return (
            False  # fenced — nothing recorded; caller must NOT write a cap-counted row
        )
    status, game = outcome
    if status == "dead":
        critical(f"Task {task_id} dead-lettered: {error}", game=game, task_id=task_id)
    return True  # this call's outcome was recorded


def once(task_id: str | None, step: str, fn):
    """Run an irreversible side-effect AT MOST once per task. If the step is already
    recorded as done on the task doc, return its recorded result and skip re-running.

    This ties external side-effects (code mint, FCM push, level publish) to the task
    that owns them: a crash-then-re-lease no longer re-runs the whole pipeline and
    double-charges. The residual window is only between the side-effect completing and
    this marker write — vastly smaller than the whole pipeline.
    """
    if not task_id:
        return fn()  # untracked (adhoc/test) call — no idempotency record to key on
    ref = db().collection(config.COL_TASKS).document(task_id)
    snap = ref.get()
    if not snap.exists:
        return fn()
    done = ((snap.to_dict() or {}).get("sideEffects") or {}).get(step)
    if done is not None:
        return done.get("result") if isinstance(done, dict) else done
    result = fn()
    try:
        ref.set(
            # store a redacted copy (task docs are world-readable); the caller
            # still gets the real result back to finish its own work
            {
                "sideEffects": {
                    step: {"result": redact_claim_links(result), "at": now()}
                },
                "updated": now(),
            },
            merge=True,
        )
    except Exception as e:  # marker is best-effort — never fail the run on it
        log.warning("once() marker write failed for %s/%s: %s", task_id, step, e)
    return result


# --------------------------------------------------------------- playbook ----

DEFAULT_PLAYBOOK: dict = {
    "version": 1,
    "philosophy": (
        "Very few users right now — keep it SIMPLE, don't over-think it. Any active or "
        "returning player is worth the day's single code and level, so act readily "
        "WITHOUT gating on deep 'is this player engaged enough' analysis. That "
        "engagement/value analysis only matters LATER — to choose WHO gets the one "
        "scarce code once many players compete for it. At this scale: 1 code + 1 level "
        "per game per day is plenty, and every active player deserves it."
    ),
    "knobs": {
        "code_send_windows_utc": [
            {"start": 16, "end": 21}
        ],  # learned over time; no nested arrays (Firestore)
        "min_days_inactive_for_code": 0,  # growth phase: active players get codes too
        "level_cadence_per_game_days": 2,
    },
    "segment_rules": [
        {
            "id": "new-user-welcome",
            "when": "first_open detected",
            "action": "ensure fresh level live; note for D1 follow-up",
        },
        {
            "id": "lapsed-return",
            "when": "user active after >=3 days away",
            "action": "consider code drop for their game",
        },
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


def earnings() -> dict:
    """Cached per-game earnings (GA4 revenue), refreshed nightly. Empty until first run."""
    snap = db().collection(config.COL_PLAYBOOK).document("earnings").get()
    return (snap.to_dict() or {}).get("games", {}) if snap.exists else {}


def audience_profile() -> dict:
    """Cached per-game audience value profile (country tier, engagement, lapsing),
    refreshed nightly from the GA4 export. Empty until the first nightly run."""
    snap = db().collection(config.COL_PLAYBOOK).document("audience").get()
    return (snap.to_dict() or {}).get("games", {}) if snap.exists else {}


def push_outcomes() -> dict:
    """Cached push effectiveness (notification open/dismiss/receive per game), nightly."""
    snap = db().collection(config.COL_PLAYBOOK).document("push_outcomes").get()
    return (snap.to_dict() or {}).get("games", {}) if snap.exists else {}


def merge_cap_overrides(
    defaults: dict, doc: dict | None, at: dt.datetime, game: str | None = None
) -> dict:
    """Pure merge of owner cap-overrides onto the hard defaults. Safety contract:
    only KNOWN cap keys apply; each value is clamped to [0, 10x default] (a code-level
    ceiling no doc can exceed); a missing/expired doc changes nothing. Top-level keys
    apply to every game; a `games.<id>` sub-map applies only to that game (and wins).
    The overrides doc is written ONLY by the owner (console/script) — no model output
    path writes it — so the LLM still cannot negotiate a single cap."""
    if not doc:
        return dict(defaults)
    expires = doc.get("expires")
    if expires is None or not hasattr(expires, "timestamp") or expires <= at:
        return dict(defaults)  # no expiry = invalid (overrides must be temporary)
    out = dict(defaults)

    def _apply(src: dict) -> None:
        for key, dflt in defaults.items():
            v = src.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                out[key] = max(0, min(dflt * 10, v))

    _apply(doc)
    scoped = (doc.get("games") or {}).get(game) if game else None
    if isinstance(scoped, dict):
        _apply(scoped)
    return out


_caps_memo: dict[str | None, tuple[float, dict]] = {}


def effective_caps(game: str | None = None) -> dict:
    """config.CAPS with any live owner overrides applied (60s memo, per game).
    Fail-safe: any read problem -> the hard-coded defaults, never looser."""
    t = time.monotonic()
    hit = _caps_memo.get(game)
    if hit and t - hit[0] < 60:
        return hit[1]
    caps = dict(config.CAPS)
    try:
        snap = db().collection(config.COL_PLAYBOOK).document("cap_overrides").get()
        caps = merge_cap_overrides(
            config.CAPS, snap.to_dict() if snap.exists else None, now(), game
        )
    except Exception as e:
        log.warning("cap_overrides read failed — using defaults: %s", e)
    _caps_memo[game] = (t, caps)
    return caps


def codes_summary() -> dict:
    """Per-game code stock/claims summary (incl. push A/B experiment tallies)."""
    doc = db().collection(config.COL_PLAYBOOK).document("codes_summary").get()
    return (doc.to_dict() or {}).get("games", {}) if doc.exists else {}


def health_status() -> dict:
    """Compact dependency health for the decision layer: overall status plus which
    dependencies are currently DOWN or degraded. Lets the Strategist avoid proposing
    actions that depend on something that isn't working."""
    snap = db().collection(config.COL_PLAYBOOK).document("health").get()
    if not snap.exists:
        return {}
    h = snap.to_dict() or {}
    checks = h.get("checks", [])
    return {
        "status": h.get("status"),
        "down": [c["name"] for c in checks if not c.get("ok")],
        "degraded": [c["name"] for c in checks if c.get("warn")],
    }


def update_playbook(new_doc: dict, reason: str) -> None:
    ref = db().collection(config.COL_PLAYBOOK).document("current")

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        old = ref.get(transaction=tx)
        cur = old.to_dict() if old.exists else None
        version = (cur.get("version", 0) + 1) if cur else 1
        doc = {**new_doc, "version": version, "updated": now()}
        if cur:  # archive the prior version atomically with the bump
            tx.set(
                ref.collection("history").document(),
                {**cur, "archived": now(), "reason": reason},
            )
        tx.set(ref, doc)
        return version

    version = _tx(db().transaction())
    ledger("decision", None, action="playbook_update", reason=reason, version=version)


# -------------------------------------------------------------- directives ----


def pending_directives() -> list[dict]:
    q = (
        db()
        .collection(config.COL_DIRECTIVES)
        .where(filter=firestore.FieldFilter("status", "==", "new"))
    )
    return [d.to_dict() | {"id": d.id} for d in q.stream()]


def resolve_directive(directive_id: str, response: str) -> None:
    db().collection(config.COL_DIRECTIVES).document(directive_id).update(
        {"status": "handled", "response": response, "handled_at": now()}
    )


def sync_directives_into_playbook() -> None:
    """Deterministically mirror the LIVE owner-directive set into the playbook's
    `ceo_directives`, every pulse — so a fresh directive shows on the dashboard Plan
    panel and reaches the Strategist immediately, not only after the nightly rewrite.

    `ceo_directives` becomes a pure projection of the unresolved directive docs
    (status 'new'), newest first. When the Strategist resolves one (or supersedes an
    older one), it drops out here on the next pulse — so the panel and the model can
    never disagree with what you've actually sent. Merge-only: never bumps the
    playbook version or archives history (that stays the Reflector's job)."""
    projected = directive_projection()
    ref = db().collection(config.COL_PLAYBOOK).document("current")
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("ceo_directives") == projected:
        return  # already in sync — no write
    ref.set({"ceo_directives": projected, "updated": now()}, merge=True)


def directive_projection() -> list[dict]:
    """The unresolved owner directives, newest first — the single source of truth
    for the playbook's `ceo_directives` field (see sync_directives_into_playbook)."""
    return [
        {"id": d["id"], "text": str(d.get("text", "")), "ts": str(d.get("ts", ""))}
        for d in sorted(
            pending_directives(), key=lambda d: str(d.get("ts", "")), reverse=True
        )
    ]


# ---------------------------------------------------------------- alerts ----


def _diagnose(message: str, context: dict) -> dict | None:
    """One bounded LLM call turning a raw error into {likely_cause, suggested_fix}.
    Pure best-effort: any failure returns None and the alert proceeds raw."""
    try:
        from agent.tools import genai_client  # lazy — and genai only logs on failure

        reply = genai_client.generate_json(
            "You are the on-call diagnosis for Stagenator, an autonomous agent on Cloud Run "
            "(Firestore state; tools: GA4, FCM push, App Store Connect minting, Runpod ComfyUI "
            "image gen, Veo video gen, Gmail, proffer.codes code drops).\n"
            f"An operation just failed. Error: {message[:400]}\n"
            f"Context: { {k: str(v)[:200] for k, v in context.items()} }\n"
            "In ONE short sentence each, give your best hypothesis:\n"
            '{"likely_cause": "...", "suggested_fix": "..."}'
        )
        if isinstance(reply, dict) and reply.get("likely_cause"):
            return {
                "likely_cause": str(reply["likely_cause"])[:300],
                "suggested_fix": str(reply.get("suggested_fix", ""))[:300],
            }
    except Exception as e:
        log.warning("error diagnosis skipped: %s", e)
    return None


def clear_step(task_id: str | None, step: str) -> None:
    """Erase a once() marker so the step re-runs on the next attempt (e.g. a design
    whose generated clip failed QA must be redesigned, not replayed)."""
    if not task_id:
        return
    try:
        db().collection(config.COL_TASKS).document(task_id).update(
            {f"sideEffects.{step}": firestore.DELETE_FIELD}
        )
    except Exception as e:  # best-effort — a missing doc just means nothing to clear
        log.warning("clear_step(%s, %s) failed: %s", task_id, step, e)


def critical(message: str, _email: bool = True, **context: Any) -> None:
    """A genuinely-urgent failure: CRITICAL log + an IMMEDIATE email to the owner.

    Email is deduped per issue (~1/hour, keyed on the message head) so a repeating
    failure sends one email, not a storm. Every step is guarded — critical() is called
    from error paths and must never raise or make a bad situation worse."""
    payload = {
        "alert": "stagenator",
        "message": message,
        **{k: str(v)[:500] for k, v in context.items()},
    }
    log.critical(json.dumps(payload))
    if config.DRY_RUN:
        return

    # Each step is INDEPENDENTLY guarded so a Firestore outage (which kills dedup and
    # the ledger) can never stop the email — SMTP does not depend on Firestore, and a
    # home-project outage is precisely when the owner must hear about it.
    key = hashlib.sha1(message[:100].encode()).hexdigest()[:12]
    deduped = False
    try:
        ref = db().collection(config.COL_PLAYBOOK).document("critical_alerts")
        sent = ref.get().to_dict() or {}
        last = sent.get(key)
        if last and (now() - last).total_seconds() < 3600:
            return  # same issue already surfaced within the hour
        fresh = {
            k: v
            for k, v in sent.items()
            if hasattr(v, "timestamp") and (now() - v).total_seconds() < 86400
        }
        fresh[key] = now()
        ref.set(fresh)
        deduped = True
    except (
        Exception
    ) as e:  # dedup unavailable -> proceed WITHOUT it (better loud than lost)
        log.warning("critical dedup unavailable (%s) — alerting without it", e)

    # Best-effort one-shot diagnosis — a labeled HYPOTHESIS, never a dependency.
    diagnosis = None
    try:
        diagnosis = _diagnose(message, context)
    except Exception:
        pass

    if _email:
        try:
            from agent.tools import mailbox  # lazy: mailbox imports state

            body = message
            if context:
                body += "\n\n" + "\n".join(
                    f"  {k}: {str(v)[:300]}" for k, v in context.items()
                )
            if diagnosis:
                body += (
                    f"\n\nAgent's read (hypothesis, not verified):"
                    f"\n  likely cause: {diagnosis.get('likely_cause')}"
                    f"\n  try: {diagnosis.get('suggested_fix')}"
                )
            body += f"\n\n{now():%Y-%m-%d %H:%M} UTC · one email per issue per hour"
            if not deduped:
                body += "\n(dedup store unreachable — this issue may repeat hourly)"
            body += "\nDashboard: https://stagenator-mission.web.app"
            mailbox.send_alert(f"Stagenator CRITICAL: {message[:90]}", body)
        except Exception as e:  # alerting must never break the caller's error path
            log.warning("critical-email failed: %s", e)

    # Dashboard feed row (red ERROR) — best-effort; the Reflector sees it as context.
    try:
        ledger(
            "error",
            context.get("game"),
            message=message[:500],
            **(
                {
                    "likely_cause": diagnosis.get("likely_cause"),
                    "suggested_fix": diagnosis.get("suggested_fix"),
                }
                if diagnosis
                else {}
            ),
            **{k: str(v)[:300] for k, v in context.items() if k != "game"},
        )
    except Exception as e:
        log.warning("critical ledger row failed: %s", e)


def heartbeat(run_kind: str) -> None:
    """INFO heartbeat log (absence trips the missing-heartbeat alert) + a
    dashboard-readable doc (lives under the playbook path so the already-
    deployed owner-read rules cover it)."""
    log.info(
        json.dumps(
            {"heartbeat": "stagenator", "run": run_kind, "ts": now().isoformat()}
        )
    )
    try:
        db().collection(config.COL_PLAYBOOK).document("heartbeat").set(
            {"kind": run_kind, "at": now()}, merge=True
        )
    except Exception as e:
        log.warning("heartbeat doc write failed: %s", e)
