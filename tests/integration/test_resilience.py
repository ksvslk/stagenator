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
        {"updated": state.now() - dt.timedelta(minutes=minutes)})


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
        got = state.claim_pending()          # claim (attempts++)
        assert len(got) == 1
        _make_stale(tid)                     # simulate the process dying before finish
    d = _doc(tid)
    assert d.get("failures", 0) == 0         # never a real failure
    assert d["status"] != "dead"             # so never dead-lettered
    assert d.get("attempts", 0) >= 5         # but the claim count reflects the kills


@NEEDS_EMU
def test_stale_release_fencing_prevents_double_write():
    """When a slow task is re-leased to a new owner, the OLD owner's late finish is a
    no-op — the fencing token stops split-brain double-writes."""
    tid = state.enqueue("job", "g", {})
    a = state.claim_pending()[0]             # owner A
    _make_stale(tid)
    b = state.claim_pending()[0]             # re-leased to owner B
    assert a["lease"] != b["lease"]
    # A finishes late — must NOT clobber B
    state.finish_task(tid, a["lease"], ok=True, result={"stale": "A"})
    d = _doc(tid)
    assert d.get("result", {}).get("stale") != "A"   # A fenced out
    assert d["status"] == "running"                  # still B's
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
    state.claim_pending()                    # re-leased to B
    state.defer_task(tid, a["lease"], "stale")   # A's defer — fenced, no-op
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
    again = state.enqueue("job", "g", {"fresh": 1}, dedupe_key=key)   # allowed
    assert again == key
    d = _doc(key)
    assert d["status"] == "pending" and d["failures"] == 0 and d["payload"] == {"fresh": 1}


@NEEDS_EMU
def test_idempotent_enqueue_dedupes_active_task():
    """An ACTIVE task with the same key is deduped — overlapping pulses can't
    double-enqueue the same work."""
    k = "same"
    assert state.enqueue("job", "g", {}, dedupe_key=k) == k
    assert state.enqueue("job", "g", {}, dedupe_key=k) is None   # still active -> deduped


@NEEDS_EMU
def test_fresh_claim_is_not_re_claimed():
    """A just-claimed (fresh) task is not handed out again to a second drain."""
    tid = state.enqueue("job", "g", {})
    first = state.claim_pending()
    assert len(first) == 1
    second = state.claim_pending()           # immediately again — still fresh/running
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
        {"type": "send_code_drop", "game": "ai-movie-quiz", "n_codes": 3})
    assert verdict and "cap" in verdict["error"].lower()


def test_guardrails_reject_unknown_and_none():
    # pure — no state needed
    assert guardrails.validate({"type": "none", "game": "x"})["silent"] is True
    assert "unknown action" in guardrails.validate({"type": "nope", "game": "ai-movie-quiz"})["error"]
    assert "unknown game" in guardrails.validate({"type": "ship_level", "game": "nope"})["error"]


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
    assert replenish._judge({"promotionEnd": "??", "createdAt": future_ms}, "google",
                            int(state.now().timestamp() * 1000), today) in ("valid", "suspect")
    # _ms handles all shapes without raising
    for v in (None, 123, 1.5, "2026-08-26", "not-a-date", object()):
        replenish._ms(v)  # must not raise


@NEEDS_EMU
def test_reflector_survives_bad_llm_output():
    """The nightly Reflector never crashes on malformed model output and never wipes
    the playbook — bad JSON / non-dict are rejected, valid output merges over current."""
    from agent.reflector import apply_reflection
    assert apply_reflection({"playbook_json": "not json at all"})["applied"] is False
    assert apply_reflection({"playbook_json": "null"})["applied"] is False        # valid JSON, not an object
    assert apply_reflection({"playbook_json": "[1,2,3]"})["applied"] is False      # array, not an object
    ok = apply_reflection({"playbook_json": '{"philosophy": "test"}',
                           "changes_summary": "s", "brief": "b"})
    assert ok["applied"] is True
    # omitted sections are preserved (merge over current), version still advances
    pb = state.get_playbook()
    assert pb.get("philosophy") == "test" and "knobs" in pb
