"""Replenish pipeline: inventory AUDIT (fully autonomous) + minting escalation.

Audit (the agent owns this end-to-end, daily):
- Apple promo codes expire 28 days after generation — anything older is a dud.
- Google promo codes die with their promotion (promotionEnd metadata when
  stagenator minted them; legacy codes without metadata: >1y old = expired,
  younger-but-unknown = suspect).
- availableCodes is corrected to count only valid codes; expired/suspect stock
  is flagged on the code docs so reservation skips it.

Minting (alert-driven until headless console sessions are wired):
- When valid stock dips below threshold, the agent raises a CRITICAL alert
  carrying the exact runbook; the flow is proven via Chrome DevTools automation
  (Play Console promotion -> CSV -> seedCampaign-schema import).
"""

import datetime as dt
import logging

from google.cloud import firestore

from agent import config, state

log = logging.getLogger("stagenator.replenish")

APPLE_CODE_LIFETIME_DAYS = 28
GOOGLE_LEGACY_MAX_AGE_DAYS = 365
LOW_STOCK_THRESHOLD = 5

RUNBOOK = (
    "Mint flow (proven, ~5 min): Play Console -> app -> Monetize > Promo codes -> "
    "Create promo code (product per campaign, 1y window) -> download CSV -> import via "
    "seedCampaign schema (codes+secrets+availableCodes). Apple: App Store Connect -> "
    "App -> Promo Codes (28-day lifetime!). Stagenator imports the CSV automatically "
    "when placed in gs://operation-sunrise.firebasestorage.app/stagenator_mint_inbox/."
)


def audit_inventory(task: dict) -> dict:
    """Autonomous expiry audit across every adopted campaign."""
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    now_ms = int(state.now().timestamp() * 1000)
    today = state.now().date().isoformat()
    report: dict = {}

    # ONLY audit campaigns WE own. Never read or modify the owner's other campaigns —
    # applying our Apple/Play expiry heuristic to a game we don't manage wrongly marks
    # its codes expired and zeroes its availableCodes (a serious overreach bug, now fixed).
    managed_q = tc.collection("campaigns").where(
        filter=firestore.FieldFilter("managedBy", "==", "stagenator")
    )
    for camp_snap in managed_q.stream():
        camp = camp_snap.reference
        d = camp_snap.to_dict()
        platform = d.get("stagenatorPlatform") or d.get("platform") or "unknown"
        game = d.get("game") or d.get("title") or "?"
        valid = expired = suspect = torn = 0

        for code_snap in camp.collection("codes").stream():
            try:
                c = code_snap.to_dict() or {}
                if c.get("isTorn"):
                    torn += 1
                    continue
                if c.get("expired"):
                    expired += 1
                    continue
                verdict = _judge(c, platform, now_ms, today)
                if verdict == "expired":
                    code_snap.reference.update(
                        {"expired": True, "expiredReason": "audit"}
                    )
                    expired += 1
                elif verdict == "suspect":
                    if not c.get("suspect"):
                        code_snap.reference.update({"suspect": True})
                    suspect += 1
                else:
                    valid += 1
            except Exception as e:  # one malformed code never aborts the whole audit
                log.warning("audit skipped a code in %s: %s", camp_snap.id, e)
                suspect += 1

        if d.get("availableCodes") != valid:
            camp.update({"availableCodes": valid})
        report[f"{game}/{platform}"] = {
            "valid": valid,
            "expired": expired,
            "suspect": suspect,
            "torn": torn,
        }

        if valid < LOW_STOCK_THRESHOLD:
            state.critical(
                f"Code stock low for {game}/{platform}: {valid} valid "
                f"({expired} expired, {suspect} suspect). {RUNBOOK}",
                campaign=camp_snap.id,
                game=game,
            )

    return report


def _ms(v) -> int | None:
    """Best-effort epoch-ms from int/float, ISO string, or Firestore Timestamp/datetime.
    Returns None when the shape is unrecognizable — callers treat that as 'unknown',
    NEVER as 'expired', so a legacy timestamp shape can't mass-tear live codes."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if hasattr(v, "timestamp"):  # datetime / Firestore Timestamp
        try:
            return int(v.timestamp() * 1000)
        except Exception:
            return None
    t = str(v).strip()
    if t.isdigit():
        return int(t)
    try:
        return int(
            dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000
        )
    except Exception:
        return None


def _end_date(v) -> str | None:
    """promotionEnd as a YYYY-MM-DD string for a date compare, or None if unparseable.
    An ISO date/datetime string is used directly (no tz round-trip that could shift the
    day); only epoch/Timestamp shapes are converted."""
    t = str(v).strip()
    if len(t) >= 10 and t[4:5] == "-" and t[7:8] == "-":
        return t[:10]
    ms = _ms(v)
    if ms is not None:
        return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).date().isoformat()
    return None


def _judge(code: dict, platform: str, now_ms: int, today: str) -> str:
    """valid | suspect | expired, per store expiry rules. Timestamps are parsed
    defensively — an unparseable value is 'unknown' and never tears a code outright."""
    if code.get("promotionEnd"):
        end = _end_date(code["promotionEnd"])
        if end is not None:
            return "valid" if end >= today else "expired"
        # unparseable promotionEnd -> fall through to age logic, don't mass-expire
    created_ms = _ms(code.get("createdAt"))
    if platform == "apple":
        if created_ms is None:
            return (
                "expired"  # untraceable apple code is >28d old with practical certainty
            )
        age_days = (now_ms - created_ms) / 86_400_000
        return "valid" if age_days <= APPLE_CODE_LIFETIME_DAYS else "expired"
    if created_ms is None:
        return "suspect"
    age_days = (now_ms - created_ms) / 86_400_000
    if age_days > GOOGLE_LEGACY_MAX_AGE_DAYS:
        return "expired"
    return "suspect" if age_days > 60 else "valid"


def check_mint_inbox(task: dict) -> dict:
    """Autonomous half of minting: import any CSV dropped in the mint inbox.

    A human (or a future headless-console run) drops `{campaignId}.csv` into
    stagenator_mint_inbox/; the agent validates, imports via seedCampaign
    schema, stamps promotion metadata, and archives the file.
    """
    import csv as csv_mod
    import hashlib
    import io
    import time

    from google.cloud import storage

    bucket = storage.Client(project=config.HOME_PROJECT).bucket(
        f"{config.HOME_PROJECT}.firebasestorage.app"
    )
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    imported: dict = {}
    for blob in bucket.list_blobs(prefix="stagenator_mint_inbox/"):
        if not blob.name.endswith(".csv"):
            continue
        campaign_id = blob.name.split("/")[-1][:-4].split("__")[0]
        promotion_end = blob.name[:-4].split("__")[1] if "__" in blob.name else None
        camp = tc.collection("campaigns").document(campaign_id)
        if not camp.get().exists:
            state.critical(
                f"mint inbox: unknown campaign {campaign_id}", blob=blob.name
            )
            continue

        # CLAIM the file first (rename out of the .csv namespace) so a crash or an
        # overlapping run can never re-import it and double-count the stock. A file
        # left as .processing is a visible signal for manual recovery.
        try:
            blob = bucket.rename_blob(blob, blob.name + ".processing")
        except Exception:
            continue  # already claimed by another run

        rows = list(csv_mod.reader(io.StringIO(blob.download_as_text())))
        codes = [r[0].strip() for r in rows[1:] if r and r[0].strip()]
        # Deterministic id per code → re-running is idempotent (overwrite, not duplicate).
        # Chunk to stay under Firestore's 500-op batch cap (2 writes per code).
        for i in range(0, len(codes), 200):
            batch = tc.batch()
            for code_str in codes[i : i + 200]:
                cid = hashlib.sha1(f"{campaign_id}:{code_str}".encode()).hexdigest()[
                    :16
                ]
                batch.set(
                    camp.collection("codes").document(cid),
                    {
                        "id": cid,
                        "isTorn": False,
                        "codeType": "one_time_code",
                        "createdAt": int(time.time() * 1000),
                        "mintedBy": "stagenator",
                        **({"promotionEnd": promotion_end} if promotion_end else {}),
                    },
                )
                batch.set(camp.collection("secrets").document(cid), {"code": code_str})
            batch.commit()
        camp.update({"availableCodes": firestore.Increment(len(codes))})
        bucket.rename_blob(
            blob,
            blob.name.replace(
                "stagenator_mint_inbox/", "stagenator_mint_done/"
            ).replace(".processing", ""),
        )
        imported[campaign_id] = len(codes)
        state.ledger(
            "action",
            None,
            action="mint_import",
            status="done",
            result={"campaign": campaign_id, "codes": len(codes)},
        )
    return {"imported": imported}


def run(task: dict) -> dict:
    """replenish_codes task.

    Apple campaigns with a giftIapId: FULLY AUTONOMOUS — mint a fresh offer-code
    batch via the ASC API and import 50 into the campaign (rest of the batch
    stays unimported; batches are free and expiry-tracked).
    Everything else (Play Console; apple without a configured gift): escalate
    with the runbook.
    """
    import random
    import string
    import time as _time

    game, payload = task["game"], task["payload"]
    if config.DRY_RUN:
        return {"dry_run": True, "would_replenish": payload.get("campaign")}

    campaign_id = payload.get("campaign")
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    camp = tc.collection("campaigns").document(campaign_id)
    d = camp.get().to_dict() or {}
    platform = d.get("stagenatorPlatform") or d.get("platform")

    if platform == "apple":
        from agent.tools import asc

        if not d.get("giftIapId") and not d.get("giftSubscriptionId"):
            gift = _select_gift(game, d)
            if not gift:
                state.critical(
                    f"Could not select a gift product for {game}/apple. {RUNBOOK}",
                    game=game,
                )
                return {
                    "escalated": True,
                    "campaign": campaign_id,
                    "reason": "gift selection failed",
                }
            key = (
                "giftSubscriptionId" if gift["kind"] == "subscription" else "giftIapId"
            )
            camp.update({key: gift["id"], "giftProduct": gift.get("productId")})
            d[key] = gift["id"]
            state.ledger(
                "decision",
                game,
                action="gift_selection",
                reason=gift.get("reason", ""),
                product=gift.get("productId"),
            )

        # Wrap the whole mint+import in once(): a retry after a completed mint must
        # NOT mint a second batch of 50 or double the availableCodes counter.
        def _mint():
            try:
                if d.get("giftSubscriptionId"):
                    rows, expiration = asc.mint_subscription_offer_codes(
                        d["giftSubscriptionId"]
                    )
                else:
                    rows, expiration = asc.mint_iap_offer_codes(d["giftIapId"])
            except (
                Exception
            ) as e:  # symmetry with the Google path: escalate, don't die silently
                state.critical(
                    f"Apple mint failed for {game}/apple: {e}. {RUNBOOK}",
                    game=game,
                    campaign=campaign_id,
                )
                return {
                    "escalated": True,
                    "campaign": campaign_id,
                    "reason": f"ASC mint failed: {e}",
                }
            batch = tc.batch()
            for code, url in rows[:50]:
                cid = "".join(
                    random.choices(string.ascii_lowercase + string.digits, k=9)
                )
                batch.set(
                    camp.collection("codes").document(cid),
                    {
                        "id": cid,
                        "isTorn": False,
                        "codeType": "one_time_code",
                        "createdAt": int(_time.time() * 1000),
                        "mintedBy": "stagenator",
                        "promotionEnd": expiration,
                        "offerType": "iap_offer_code",
                    },
                )
                batch.set(
                    camp.collection("secrets").document(cid),
                    {"code": code, "redeemUrl": url},
                )
            n_minted = len(rows[:50])
            batch.commit()
            camp.update({"availableCodes": firestore.Increment(n_minted)})
            return {
                "autonomous": True,
                "store": "app-store",
                "imported": n_minted,
                "minted": len(rows),
                "valid_until": expiration,
            }

        return state.once(task.get("id"), "apple_mint", _mint)

    # Google Play (no mint API): email the owner a precise restock request.
    from agent.tools import mailbox

    play_app_id = d.get("playAppId")
    try:
        mailbox.send_restock_request(game, campaign_id, play_app_id)
        return {"escalated": True, "via": "email", "campaign": campaign_id}
    except Exception as e:
        state.critical(
            f"Replenish needed for {game} campaign {campaign_id} "
            f"(email failed: {e}). {RUNBOOK}",
            game=game,
        )
        return {"escalated": True, "via": "critical-log", "campaign": campaign_id}


def _select_gift(game: str, campaign: dict) -> dict | None:
    """Bounded LLM choice: pick the gift product from the app's REAL catalog.

    Guardrails: the choice must exist in the catalog (validated in code);
    playbook philosophy + any pending CEO directives steer the taste."""
    from agent.tools import asc, genai_client

    app_id = config.GAMES.get(game, {}).get("app_store_id")
    if not app_id:
        return None
    catalog = asc.list_products(app_id)
    if not catalog:
        return None
    playbook = state.get_playbook()
    directives = [d.get("text", "") for d in state.pending_directives()]
    reply = genai_client.generate_json(
        f"You choose which product an engagement agent gifts to players of {game!r} "
        f"via free App Store offer codes.\n"
        f"Catalog (choose EXACTLY one by its 'id'): {catalog}\n"
        f"Playbook philosophy: {playbook.get('philosophy', '')}\n"
        f"Owner directives (highest priority if relevant): {directives}\n"
        "Guidance: prefer a meaningful mid-tier gift (quality-of-life like ads removal, "
        "or a content pack) over the full-unlock bundle (protects future revenue) — "
        "unless a directive says otherwise.\n"
        'Reply JSON: {"id": "...", "productId": "...", "reason": "one sentence"}'
    )
    if not reply or not reply.get("id"):
        return None
    valid = {p["id"]: p for p in catalog}
    if reply["id"] not in valid:
        return None
    # both kinds are mintable now (IAP offers and subscription free-trial offers)
    return {**valid[reply["id"]], "reason": reply.get("reason", "")}


def poll_restock_inbox(task: dict) -> dict:
    """Import any Play code CSVs the owner replied with by email."""
    if config.DRY_RUN:
        return {"dry_run": True}
    from agent.tools import mailbox

    return mailbox.poll_and_import()


def housekeeping(task: dict) -> dict:
    """Bounded-growth janitor. Keeps the system from leaving trash or growing without
    need: prunes stale ledger/task/brief docs, releases abandoned code reservations,
    deletes expired claim tokens. Every deletion is guarded by an age/expiry/status
    check well beyond any live read window, so it can only ever remove genuinely-dead
    data. Idempotent — safe to run daily."""
    import datetime as dt

    if config.DRY_RUN:
        return {"dry_run": True}

    db = state.db()
    now = state.now()
    out: dict = {}

    def _batch_delete(refs: list) -> int:
        done = 0
        while refs:
            chunk, refs = refs[:400], refs[400:]
            b = db.batch()
            for r in chunk:
                b.delete(r)
            b.commit()
            done += len(chunk)
        return done

    # 1) LEDGER — routine rows > 30d; keep 'outcome' rows (the learning signal) to 90d.
    led_cut = now - dt.timedelta(days=config.LEDGER_RETENTION_DAYS)
    out_cut = now - dt.timedelta(days=config.LEDGER_OUTCOME_RETENTION_DAYS)
    refs = []
    for d in (
        db.collection(config.COL_LEDGER)
        .where(filter=firestore.FieldFilter("ts", "<", led_cut))
        .limit(3000)
        .stream()
    ):
        x = d.to_dict() or {}
        if x.get("kind") == "outcome":
            if x.get("ts") and x["ts"] < out_cut:
                refs.append(d.reference)
        else:
            refs.append(d.reference)
    out["ledger_pruned"] = _batch_delete(refs)

    # 2) TASKS — only terminal (done/dead) docs older than 14d. Never pending/running.
    task_cut = now - dt.timedelta(days=config.TASK_RETENTION_DAYS)
    refs = []
    for st in ("done", "dead"):
        for d in (
            db.collection(config.COL_TASKS)
            .where(filter=firestore.FieldFilter("status", "==", st))
            .limit(1000)
            .stream()
        ):
            x = d.to_dict() or {}
            if x.get("updated") and x["updated"] < task_cut:
                refs.append(d.reference)
    out["tasks_pruned"] = _batch_delete(refs)

    # 3) BRIEFS — older than 30d (dashboard shows only the latest few).
    brief_cut = now - dt.timedelta(days=config.BRIEF_RETENTION_DAYS)
    refs = [
        d.reference
        for d in (
            db.collection(config.COL_BRIEFS)
            .where(filter=firestore.FieldFilter("ts", "<", brief_cut))
            .limit(1000)
            .stream()
        )
    ]
    out["briefs_pruned"] = _batch_delete(refs)

    # 4) TAKE-CODES — release abandoned reservations + delete expired tokens.
    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    released = tokens_deleted = 0

    def _release(campaign_id: str, code_id: str) -> bool:
        """Release a code ONLY if it's still stagenator-reserved and un-torn."""
        cref = (
            tc.collection("campaigns")
            .document(campaign_id)
            .collection("codes")
            .document(code_id)
        )
        cs = cref.get().to_dict() or {}
        if cs and not cs.get("isTorn") and cs.get("reservedBy") == "stagenator":
            cref.update(
                {
                    "reservedBy": firestore.DELETE_FIELD,
                    "reservedAt": firestore.DELETE_FIELD,
                }
            )
            return True
        return False

    # 4a) EXPIRED stagenator tokens: an expired token can never be claimed, so its
    #     un-torn reserved codes are abandoned — return them to the pool, drop the token.
    for cb in ("stagenator", "stagenator-test"):
        for tdoc in (
            tc.collection("claimTokens")
            .where(filter=firestore.FieldFilter("createdBy", "==", cb))
            .limit(2000)
            .stream()
        ):
            t = tdoc.to_dict() or {}
            exp = t.get("expiresAt")
            if not exp or exp >= now:
                continue  # keep live tokens untouched
            entries = []
            if t.get("codeIds"):
                entries.append((t.get("campaignId"), t.get("codeIds")))
            for pool in (t.get("pools") or {}).values():
                entries.append((pool.get("campaignId"), pool.get("codeIds") or []))
            for cid, ids in entries:
                for code_id in ids or []:
                    if cid and code_id and _release(cid, code_id):
                        released += 1
            tdoc.reference.delete()
            tokens_deleted += 1

    # 4b) ORPHAN sweep: reservations with NO live token (e.g. a pre-fix crash mid-reserve).
    #     Only touch codes held longer than any token TTL, so a live reservation is safe.
    res_cut = now - dt.timedelta(days=config.RESERVATION_MAX_AGE_DAYS)
    from agent import rules

    seen_campaigns = set()
    for game in config.ACTIVE_GAMES:
        for camp in rules.campaign_inventory(game).get("campaigns", {}).values():
            cid = camp.get("campaign_id")
            if not cid or cid in seen_campaigns:
                continue
            seen_campaigns.add(cid)
            codes = tc.collection("campaigns").document(cid).collection("codes")
            for c in (
                codes.where(
                    filter=firestore.FieldFilter("reservedBy", "==", "stagenator")
                )
                .limit(1000)
                .stream()
            ):
                cx = c.to_dict() or {}
                ra = cx.get("reservedAt")
                if not cx.get("isTorn") and ra and ra < res_cut:
                    c.reference.update(
                        {
                            "reservedBy": firestore.DELETE_FIELD,
                            "reservedAt": firestore.DELETE_FIELD,
                        }
                    )
                    released += 1

    out["reservations_released"] = released
    out["expired_tokens_deleted"] = tokens_deleted
    return out


def cleanup_storage(task: dict) -> dict:
    """Keep storage light: delete Mission Control preview media > 14 days and any
    leftover AMQ temp_uploads > 1 day."""
    import datetime as dt

    from google.cloud import storage

    if config.DRY_RUN:
        return {"dry_run": True}
    removed = 0
    home = storage.Client(project=config.HOME_PROJECT).bucket(
        f"{config.HOME_PROJECT}.firebasestorage.app"
    )
    cutoff_prev = state.now() - dt.timedelta(days=14)
    for blob in home.list_blobs(prefix="stagenator_previews/"):
        if blob.time_created and blob.time_created < cutoff_prev:
            blob.delete()
            removed += 1
    cutoff_tmp = state.now() - dt.timedelta(days=1)
    for prefix in ("temp_uploads/", "stagenator_veo_cache/"):
        for blob in home.list_blobs(prefix=prefix):
            if blob.time_created and blob.time_created < cutoff_tmp:
                blob.delete()
                removed += 1
    return {"removed_blobs": removed}


def check_balances(task: dict) -> dict:
    if config.DRY_RUN:
        return {"dry_run": True, "check": "runpod"}
    from agent.tools import runpod

    balance = runpod.account_balance()
    if balance is not None and balance < 5.0:
        state.critical(
            f"Runpod balance low: ${balance:.2f} — top up soon", balance=balance
        )
    return {"runpod_balance": balance}
