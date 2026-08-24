"""Runpod: puzzle generation endpoint (wired when key arrives) + balance check."""

import logging
import os

import requests

log = logging.getLogger("stagenator.runpod")

API = "https://api.runpod.io/graphql"


def _key() -> str | None:
    return os.getenv("RUNPOD_API_KEY")


def account_balance() -> float | None:
    key = _key()
    if not key:
        log.warning("RUNPOD_API_KEY not set — balance unknown")
        return None
    q = {"query": "query { myself { clientBalance } }"}
    r = requests.post(API, json=q, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["myself"]["clientBalance"]


def generate_puzzle(prompt: dict) -> dict:
    raise RuntimeError("Runpod puzzle endpoint not wired yet (needs endpoint id + key)")
