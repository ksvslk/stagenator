"""Executor pipelines — plain code + tools, dispatched by task type.

Every pipeline is idempotent-per-task and raises on failure (the queue handles
retry/dead-letter). DRY_RUN stubs external side effects for eval/CI.
"""

from agent.pipelines import codes, levels, replenish

_DISPATCH = {
    "level_pipeline": levels.run,
    "level_push": levels.push_only,
    "code_drop": codes.run_drop,
    "individual_code": codes.run_individual,
    "promo_banner": codes.run_banner,
    "replenish_codes": replenish.run,
    "check_balances": replenish.check_balances,
    "audit_inventory": replenish.audit_inventory,
    "mint_import": replenish.check_mint_inbox,
    "poll_restock_inbox": replenish.poll_restock_inbox,
}


def run_task(task: dict) -> dict:
    handler = _DISPATCH.get(task["type"])
    if handler is None:
        raise ValueError(f"no pipeline for task type {task['type']!r}")
    return handler(task)
