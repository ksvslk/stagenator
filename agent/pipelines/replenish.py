"""Replenish pipeline: mint new store codes when campaigns run low; balance checks.

- App Store offer codes: App Store Connect API (wired when ASC key arrives).
- Google Play promo codes: Play Console via headless Chrome DevTools MCP
  (the ONE browser-automation surface; wired at the replenish milestone).
- Runpod prepaid balance: checked daily; low balance -> CRITICAL alert.
"""

from agent import config, state


def run(task: dict) -> dict:
    game, payload = task["game"], task["payload"]
    if config.DRY_RUN:
        return {"dry_run": True, "would_mint_for": payload.get("campaign")}
    # Real minting lands with ASC key (iOS) and Play Console session (Android).
    state.critical(
        f"Code inventory low for {game} and minting not yet wired — top up campaign "
        f"{payload.get('campaign')} manually",
        game=game,
    )
    raise RuntimeError("minting not wired yet (ASC key / Play Console session pending)")


def check_balances(task: dict) -> dict:
    if config.DRY_RUN:
        return {"dry_run": True, "check": "runpod"}
    from agent.tools import runpod

    balance = runpod.account_balance()
    if balance is not None and balance < 5.0:
        state.critical(f"Runpod balance low: ${balance:.2f} — top up soon", balance=balance)
    return {"runpod_balance": balance}
