"""Trigger endpoints — the ambient seam (pattern: ambient-expense-agent).

Cloud Scheduler POSTs /triggers/pulse | /triggers/nightly | /triggers/replenish;
Eventarc POSTs Firestore events to /triggers/event. Each run is a fresh,
recorded session driven through the shared Runner, so every scheduled turn is
inspectable in the ADK web UI and the ledger."""

import json
import logging
import uuid

from fastapi import APIRouter, Request
from google.genai import types

log = logging.getLogger("stagenator.triggers")
router = APIRouter(prefix="/triggers", tags=["stagenator"])

SCHEDULER_USER = "scheduler"


async def _run(request: Request, message: str) -> dict:
    runner = request.app.state.runner
    session = await runner.session_service.create_session(
        app_name=request.app.state.agent_app_name,
        user_id=SCHEDULER_USER,
        session_id=f"run-{uuid.uuid4().hex[:12]}",
    )
    outputs = []
    async for event in runner.run_async(
        user_id=SCHEDULER_USER,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=message)]),
    ):
        if getattr(event, "output", None) is not None:
            outputs.append(event.output)
    return {"session": session.id, "final": outputs[-1] if outputs else None}


@router.post("/pulse")
async def pulse(request: Request) -> dict:
    return await _run(request, "pulse")


@router.post("/nightly")
async def nightly(request: Request) -> dict:
    return await _run(request, "nightly")


@router.post("/replenish")
async def replenish(request: Request) -> dict:
    return await _run(request, "replenish")


@router.post("/event")
async def event(request: Request) -> dict:
    """Eventarc Firestore trigger — fast path. Body may be CloudEvent JSON."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    payload = {
        "game": _game_from_event(body),
        "signal": "eventarc",
        "raw_subject": body.get("subject") or body.get("source") or "",
    }
    return await _run(request, f"event:{json.dumps(payload)}")


def _game_from_event(body: dict) -> str | None:
    from agent import config

    subject = str(body.get("subject", "")) + str(body.get("source", ""))
    for game, cfg in config.GAMES.items():
        if cfg["project"] in subject:
            return game
    return None
