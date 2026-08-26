"""Resilience / recovery proof — happy AND unhappy paths.

The queue tests run against a real Firestore emulator (not mocks), so they exercise
the actual transactions and prove the durable state machine survives crashes,
infra kills, and repeated failures WITHOUT wedging or double-executing.

Run with the emulator:
    FIRESTORE_EMULATOR_HOST=localhost:8919 GOOGLE_CLOUD_PROJECT=demo-test \
        uv run pytest tests/integration/test_resilience.py -v
"""

import datetime as dt
import os

import pytest

NEEDS_EMU = pytest.mark.skipif(
    not os.getenv("FIRESTORE_EMULATOR_HOST"),
    reason="needs Firestore emulator (set FIRESTORE_EMULATOR_HOST)",
)

from agent import config, guardrails, state  # noqa: E402
from agent.pipelines import replenish  # noqa: E402


def _wipe(col):
    for d in state.db().collection(col).stream():
        d.reference.delete()


@pytest.fixture(autouse=True)
def clean():
    if os.getenv("FIRESTORE_EMULATOR_HOST"):
        for c in (config.COL_TASKS, config.COL_LEDGER, config.COL_PLAYBOOK):
            _wipe(c)
    yield


def _doc(tid):
    return state.db().collection(config.COL_TASKS).document(tid).get().to_dict()


def _make_stale(tid, minutes=16):
    state.db().collection(config.COL_TASKS).document(tid).update(
        {"updated": state.now() - dt.timedelta(minutes=minutes)}
    )


# ============================ QUEUE — the durable core ============================


@NEEDS_EMU
def test_happy_path_claim_finish():
    tid = state.enqueue("job", "g", {"x": 1})
    assert tid
    claimed = state.claim_pending()
    assert len(claimed) == 1 and claimed[0]["id"] == tid
    state.finish_task(tid, claimed[0]["lease"], ok=True, result={"done": 1})
    d = _doc(tid)
    assert d["status"] == "done" and d["result"] == {"done": 1}


@NEEDS_EMU
def test_failure_retries_then_deadletters_bounded():
    """A genuinely failing task retries a BOUNDED number of times, then dead-letters —
    never an infinite loop."""
    tid = state.enqueue("job", "g", {})
    for _ in range(config.MAX_TASK_ATTEMPTS):
        c = state.claim_pending()
        assert len(c) == 1
        state.finish_task(tid, c[0]["lease"], ok=False, error="boom")
    d = _doc(tid)
    assert d["status"] == "dead" and d["failures"] == config.MAX_TASK_ATTEMPTS
    # a dead task is never handed out again
    assert state.claim_pending() == []


@NEEDS_EMU
def test_infra_kill_does_not_deadletter_a_healthy_task():
    """Claimed-but-never-finished (scale-to-zero / 540s kill) is re-leased. Because
    dead-lettering is driven by FAILURES (not claim count), repeated infra kills never
    dead-letter a task that never actually failed."""
    tid = state.enqueue("job", "g", {})
    for _ in range(5):
        got = state.claim_pending()  # claim (attempts++)
        assert len(got) == 1
        _make_stale(tid)  # simulate the process dying before finish
    d = _doc(tid)
    assert d.get("failures", 0) == 0  # never a real failure
    assert d["status"] != "dead"  # so never dead-lettered
    assert d.get("attempts", 0) >= 5  # but the claim count reflects the kills


@NEEDS_EMU
def test_stale_release_fencing_prevents_double_write():
    """When a slow task is re-leased to a new owner, the OLD owner's late finish is a
    no-op — the fencing token stops split-brain double-writes."""
    tid = state.enqueue("job", "g", {})
    a = state.claim_pending()[0]  # owner A
    _make_stale(tid)
    b = state.claim_pending()[0]  # re-leased to owner B
    assert a["lease"] != b["lease"]
    # A finishes late — must NOT clobber B
    state.finish_task(tid, a["lease"], ok=True, result={"stale": "A"})
    d = _doc(tid)
    assert d.get("result", {}).get("stale") != "A"  # A fenced out
    assert d["status"] == "running"  # still B's
    # B finishes for real
    state.finish_task(tid, b["lease"], ok=True, result={"real": "B"})
    d = _doc(tid)
    assert d["status"] == "done" and d["result"] == {"real": "B"}


@NEEDS_EMU
def test_defer_is_not_a_failure():
    """A budget/not-yet-due deferral returns the task to pending WITHOUT counting as a
    failure — scheduling never burns the retry budget."""
    tid = state.enqueue("job", "g", {})
    c = state.claim_pending()[0]
    state.defer_task(tid, c["lease"], "budget")
    d = _doc(tid)
    assert d["status"] == "pending" and d.get("failures", 0) == 0
    # a stale lease can't defer someone else's task either
    a = state.claim_pending()[0]
    _make_stale(tid)
    state.claim_pending()  # re-leased to B
    state.defer_task(tid, a["lease"], "stale")  # A's defer — fenced, no-op
    assert _doc(tid)["status"] == "running"


@NEEDS_EMU
def test_poison_doc_can_reenqueue():
    """A dead-lettered/done key can be re-enqueued fresh — a recurring need is not
    deduped against its own tombstone forever."""
    key = "recurring-shortage"
    state.enqueue("job", "g", {}, dedupe_key=key)
    for _ in range(config.MAX_TASK_ATTEMPTS):
        c = state.claim_pending()[0]
        state.finish_task(key, c["lease"], ok=False, error="x")
    assert _doc(key)["status"] == "dead"
    again = state.enqueue("job", "g", {"fresh": 1}, dedupe_key=key)  # allowed
    assert again == key
    d = _doc(key)
    assert (
        d["status"] == "pending" and d["failures"] == 0 and d["payload"] == {"fresh": 1}
    )


@NEEDS_EMU
def test_idempotent_enqueue_dedupes_active_task():
    """An ACTIVE task with the same key is deduped — overlapping pulses can't
    double-enqueue the same work."""
    k = "same"
    assert state.enqueue("job", "g", {}, dedupe_key=k) == k
    assert (
        state.enqueue("job", "g", {}, dedupe_key=k) is None
    )  # still active -> deduped


@NEEDS_EMU
def test_fresh_claim_is_not_re_claimed():
    """A just-claimed (fresh) task is not handed out again to a second drain."""
    assert state.enqueue("job", "g", {}) is not None
    first = state.claim_pending()
    assert len(first) == 1
    second = state.claim_pending()  # immediately again — still fresh/running
    assert second == []


# ============================ GUARDRAILS — bad decisions bounce ============================


@NEEDS_EMU
def test_guardrails_enforce_daily_level_cap():
    # one level already shipped today
    state.ledger("action", "ai-movie-quiz", action="level_pipeline", status="done")
    verdict = guardrails.validate({"type": "ship_level", "game": "ai-movie-quiz"})
    assert verdict and "cap" in verdict["error"].lower()


@NEEDS_EMU
def test_guardrails_enforce_code_notification_cap():
    state.ledger("action", "ai-movie-quiz", action="code_drop", status="done")
    verdict = guardrails.validate(
        {"type": "send_code_drop", "game": "ai-movie-quiz", "n_codes": 3}
    )
    assert verdict and "cap" in verdict["error"].lower()


def test_guardrails_reject_unknown_and_none():
    # pure — no state needed
    assert guardrails.validate({"type": "none", "game": "x"})["silent"] is True
    assert (
        "unknown action"
        in guardrails.validate({"type": "nope", "game": "ai-movie-quiz"})["error"]
    )
    assert (
        "unknown game"
        in guardrails.validate({"type": "ship_level", "game": "nope"})["error"]
    )


# ============================ PARSING — malformed input never crashes ============================


def test_expiry_survives_weird_timestamp_shapes():
    """A legacy epoch-int / ISO / missing timestamp is parsed defensively — it never
    crashes and never mass-expires a live code."""
    today = state.now().date().isoformat()
    # epoch-ms int as promotionEnd far in the future -> valid, not falsely expired
    future_ms = int((state.now() + dt.timedelta(days=30)).timestamp() * 1000)
    assert replenish._judge({"promotionEnd": future_ms}, "google", 0, today) == "valid"
    # ISO date string today -> valid
    assert replenish._judge({"promotionEnd": today}, "google", 0, today) == "valid"
    # garbage promotionEnd -> falls through to age logic, does not crash / mass-expire
    assert replenish._judge(
        {"promotionEnd": "??", "createdAt": future_ms},
        "google",
        int(state.now().timestamp() * 1000),
        today,
    ) in ("valid", "suspect")
    # _ms handles all shapes without raising
    for v in (None, 123, 1.5, "2026-08-26", "not-a-date", object()):
        replenish._ms(v)  # must not raise


@NEEDS_EMU
def test_reflector_survives_bad_llm_output():
    """The nightly Reflector never crashes on malformed model output and never wipes
    the playbook — bad JSON / non-dict are rejected, valid output merges over current."""
    from agent.reflector import apply_reflection

    assert apply_reflection({"playbook_json": "not json at all"})["applied"] is False
    assert (
        apply_reflection({"playbook_json": "null"})["applied"] is False
    )  # valid JSON, not an object
    assert (
        apply_reflection({"playbook_json": "[1,2,3]"})["applied"] is False
    )  # array, not an object
    ok = apply_reflection(
        {
            "playbook_json": '{"philosophy": "test"}',
            "changes_summary": "s",
            "brief": "b",
        }
    )
    assert ok["applied"] is True
    # omitted sections are preserved (merge over current), version still advances
    pb = state.get_playbook()
    assert pb.get("philosophy") == "test" and "knobs" in pb


# ==================== FIXES: side-effect idempotency & cap integrity ====================


@NEEDS_EMU
def test_once_runs_irreversible_side_effect_only_once():
    """once() records a completed side-effect on the task; a retry returns the cached
    result WITHOUT re-running it — the core fix for double mint / push / level ship."""
    state.enqueue("job", "g", {}, dedupe_key="once-task")
    tid = "once-task"
    calls = []

    def fn():
        calls.append(1)
        return {"n": len(calls)}

    r1 = state.once(tid, "deliver", fn)
    r2 = state.once(tid, "deliver", fn)  # simulated retry
    assert r1 == {"n": 1} and r2 == {"n": 1}
    assert len(calls) == 1  # side-effect ran exactly once


@NEEDS_EMU
def test_daily_capped_key_dedupes_despite_volatile_payload():
    """Two concurrent deciders producing a code_drop with DIFFERENT not_before/message/
    n_codes must collapse to one task — the fix for concurrent double-enqueue (GAP 2)."""
    a = state.enqueue(
        "code_drop",
        "ai-movie-quiz",
        {"not_before": "2026-01-01T00:00:00", "message": "hi", "n_codes": 5},
    )
    b = state.enqueue(
        "code_drop",
        "ai-movie-quiz",
        {"not_before": "2026-02-02T00:00:00", "message": "yo", "n_codes": 3},
    )
    assert a is not None and b is None  # second deduped to the same active task


@NEEDS_EMU
def test_reenqueue_after_done_clears_side_effects():
    """A fresh run of a re-enqueued key must NOT skip its side-effect because of a stale
    marker from the previous run."""
    state.enqueue("job", "g", {}, dedupe_key="k1")
    state.once("k1", "deliver", lambda: {"v": 1})
    c = state.claim_pending()[0]
    state.finish_task("k1", c["lease"], ok=True, result={})
    state.enqueue(
        "job", "g", {"fresh": 1}, dedupe_key="k1"
    )  # re-enqueue -> clears sideEffects
    calls = []

    def fn():
        calls.append(1)
        return {"v": 2}

    assert state.once("k1", "deliver", fn) == {"v": 2} and len(calls) == 1


@NEEDS_EMU
def test_heavy_task_gets_longer_stale_window():
    """A slow level_pipeline (heavy) must not be re-leased at 20 min (30-min window),
    but is at 31 — so a legit long run isn't double-executed (GAP 3)."""
    tid = state.enqueue("level_pipeline", "ai-movie-quiz", {})
    assert len(state.claim_pending()) == 1
    _make_stale(tid, minutes=20)
    assert state.claim_pending() == []  # heavy: still owned at 20 min
    _make_stale(tid, minutes=31)
    assert len(state.claim_pending()) == 1  # re-leased past 30 min


@NEEDS_EMU
def test_gate_survives_malformed_strategist_output():
    """gate() degrades a non-dict decision to a no-op instead of crashing the pulse."""
    from agent.agent import gate

    assert gate("not a dict") == {"enqueued": [], "rejected": [], "notes": ""}
    assert gate(None)["enqueued"] == []


# ==================== HOUSEKEEPING: bounded growth, no trash ====================


@NEEDS_EMU
def test_housekeeping_prunes_only_dead_data(monkeypatch):
    """The janitor removes stale ledger/tasks/briefs but NEVER touches recent data,
    'outcome' rows inside 90d, or pending/running tasks."""
    monkeypatch.setattr(
        config, "DRY_RUN", False
    )  # exercise the real prune (local .env has it on)
    from agent.pipelines.replenish import housekeeping

    db = state.db()
    old = state.now() - dt.timedelta(days=40)
    very_old = state.now() - dt.timedelta(days=100)
    recent = state.now() - dt.timedelta(days=1)
    L, T, B = config.COL_LEDGER, config.COL_TASKS, config.COL_BRIEFS
    db.collection(L).document("l_old").set({"ts": old, "kind": "action"})
    db.collection(L).document("l_recent").set({"ts": recent, "kind": "action"})
    db.collection(L).document("o_40").set({"ts": old, "kind": "outcome"})
    db.collection(L).document("o_100").set({"ts": very_old, "kind": "outcome"})
    db.collection(T).document("t_done_old").set(
        {"status": "done", "updated": old, "type": "x"}
    )
    db.collection(T).document("t_dead_old").set(
        {"status": "dead", "updated": old, "type": "x"}
    )
    db.collection(T).document("t_done_recent").set(
        {"status": "done", "updated": recent, "type": "x"}
    )
    db.collection(T).document("t_pending_old").set(
        {"status": "pending", "updated": old, "type": "x"}
    )
    db.collection(B).document("b_old").set({"ts": old, "brief": "x"})
    db.collection(B).document("b_recent").set({"ts": recent, "brief": "y"})

    housekeeping({})

    def ex(col, doc):
        return db.collection(col).document(doc).get().exists

    assert not ex(L, "l_old") and ex(L, "l_recent")  # routine >30d gone, recent kept
    assert ex(L, "o_40") and not ex(L, "o_100")  # outcome kept to 90d, older pruned
    assert not ex(T, "t_done_old") and not ex(T, "t_dead_old")
    assert ex(T, "t_done_recent")  # <14d kept
    assert ex(T, "t_pending_old")  # pending NEVER deleted
    assert not ex(B, "b_old") and ex(B, "b_recent")
    # cleanup briefs (not wiped by the fixture)
    db.collection(B).document("b_recent").delete()


@NEEDS_EMU
def test_gate_resolves_only_answered_directives():
    """A Strategist answer resolves ONLY the directives it names by id — unrelated
    open directives are never blanket-resolved (A13/D16/F10)."""
    from agent.agent import gate

    db = state.db()
    db.collection(config.COL_DIRECTIVES).document("d1").set(
        {"status": "new", "text": "do X"}
    )
    db.collection(config.COL_DIRECTIVES).document("d2").set(
        {"status": "new", "text": "do Y"}
    )
    gate(
        {
            "actions": [
                {
                    "type": "none",
                    "game": "ai-movie-quiz",
                    "directive_responses": {"d1": "handled X"},
                }
            ],
            "notes": "",
        }
    )

    def st(d):
        return (
            db.collection(config.COL_DIRECTIVES).document(d).get().to_dict() or {}
        ).get("status")

    assert st("d1") == "handled"  # the one answered
    assert st("d2") == "new"  # untouched
    db.collection(config.COL_DIRECTIVES).document("d1").delete()
    db.collection(config.COL_DIRECTIVES).document("d2").delete()


@NEEDS_EMU
def test_realtime_failure_alerts_once_not_silent_zero(monkeypatch):
    """A realtime-query FAILURE must alert (email + critical) instead of silently reading
    0 — and dedupe to one alert per hour, not one per 5-min pulse."""
    from agent import rules

    sent = []
    monkeypatch.setattr(
        "agent.tools.mailbox.send_alert", lambda subj, body: sent.append(subj)
    )
    monkeypatch.setattr(rules.state, "critical", lambda *a, **k: None)
    rules._alert_realtime_blind("ai-movie-quiz", RuntimeError("400 newVsReturning"))
    rules._alert_realtime_blind(
        "ai-movie-quiz", RuntimeError("400 newVsReturning")
    )  # within 1h
    assert len(sent) == 1 and "BLIND" in sent[0]
    rules.state.db().collection(config.COL_PLAYBOOK).document("realtime_alert").delete()


@NEEDS_EMU
def test_critical_emails_immediately_with_hourly_dedup(monkeypatch):
    """critical() emails the owner at once, dedupes the same issue for 1h, still emails
    a DIFFERENT issue, and never raises even if mailing breaks."""
    monkeypatch.setattr(config, "DRY_RUN", False)
    sent = []
    monkeypatch.setattr(
        "agent.tools.mailbox.send_alert", lambda subj, body: sent.append(subj)
    )
    monkeypatch.setattr(
        state,
        "_diagnose",
        lambda m, c: {
            "likely_cause": "api key lacks run scope",
            "suggested_fix": "issue a run-capable key",
        },
    )
    state.critical("Task X dead-lettered: 403 runpod")
    state.critical("Task X dead-lettered: 403 runpod")  # same issue, within the hour
    state.critical("Apple mint failed for sw/apple")  # different issue
    assert len(sent) == 2 and "dead-lettered" in sent[0] and "mint failed" in sent[1]
    # _email=False suppresses (callers with their own tailored email)
    state.critical("Health check DOWN: x", _email=False)
    assert len(sent) == 2
    # a broken mailer must never propagate out of an error path
    monkeypatch.setattr(
        "agent.tools.mailbox.send_alert",
        lambda subj, body: (_ for _ in ()).throw(RuntimeError("smtp down")),
    )
    state.critical("another fresh issue")  # must not raise
    # every deduped critical also lands as a red 'error' row in the dashboard feed:
    # 4 distinct issues surfaced (the repeat was deduped), regardless of email success
    errors = state.recent_ledger(hours=1, kind="error")
    assert len(errors) == 4
    assert any("dead-lettered" in str(e.get("message")) for e in errors)
    # the model's diagnosis rides along on the feed row (labeled hypothesis)
    assert any(e.get("likely_cause") == "api key lacks run scope" for e in errors)
    state.db().collection(config.COL_PLAYBOOK).document("critical_alerts").delete()


@NEEDS_EMU
def test_failed_attempt_does_not_consume_daily_cap():
    """A dead/failed level attempt (only an 'enqueued' ledger row, never 'done') must
    NOT consume the 1-level/day budget — the player got nothing, so the agent may retry."""
    state.ledger(
        "action", "subliminal-words", action="level_pipeline", status="enqueued"
    )
    assert (
        guardrails.validate({"type": "ship_level", "game": "subliminal-words"}) is None
    )
    # a COMPLETED ship does consume it
    state.ledger("action", "subliminal-words", action="level_pipeline", status="done")
    verdict = guardrails.validate({"type": "ship_level", "game": "subliminal-words"})
    assert verdict and "cap" in verdict["error"].lower()


@NEEDS_EMU
def test_dedup_unmutes_segment_after_failure():
    """'Seen' is not 'served': a game's signal stays muted for 1h — UNLESS
    the game logged an error after it (the delivery failed), in which case it
    un-mutes immediately so a new/returning player triggers a retry."""
    import time as _t

    from agent import rules

    state.ledger(
        "signal", "subliminal-words", signal="user_active", detail="iOS:United States"
    )
    assert (
        "subliminal-words",
        "user_active",
        "iOS:United States",
    ) in rules._recently_seen()
    _t.sleep(0.05)  # ensure the error timestamp post-dates the signal
    state.ledger("error", "subliminal-words", message="task dead-lettered: 403")
    assert (
        "subliminal-words",
        "user_active",
        "iOS:United States",
    ) not in rules._recently_seen()
    # an error must NOT un-mute other games' segments
    state.ledger(
        "signal", "ai-movie-quiz", signal="user_active", detail="ANDROID:Estonia"
    )
    assert ("ai-movie-quiz", "user_active", "ANDROID:Estonia") in rules._recently_seen()
