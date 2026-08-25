"""Reply-to-restock: the agent's own Gmail as a control channel.

When a Google Play campaign runs low (no mint API exists — Console-only), the
agent EMAILS the owner exact instructions from stagenator.agent@gmail.com. The
owner creates the promotion in Play Console and REPLIES with the CSV attached.
Each replenish run polls the inbox (IMAP), matches the reply to a campaign by a
tag in the subject, imports the codes (same seedCampaign schema), and archives
the mail. No OAuth — a Gmail app password in Secret Manager.
"""

import datetime as dt
import email
import imaplib
import logging
import os
import re
import smtplib
from email.message import EmailMessage

from google.cloud import firestore

from agent import config, state

log = logging.getLogger("stagenator.mailbox")

USER = "stagenator.agent@gmail.com"
OWNER = os.getenv("OWNER_EMAIL", "indrekl@gmail.com")
TAG_RE = re.compile(r"\[stagenator-restock:([A-Za-z0-9]+)\]")

# per-campaign Play Console gift (product to gift + which promo type to create)
PLAY_GIFT = {
    "subliminal-words": ("No ad breaks", "one-time product"),
    "ai-movie-quiz": ("Premium (1 month)", "subscription"),
}


def _pw() -> str:
    pw = os.getenv("GMAIL_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GMAIL_APP_PASSWORD not set")
    return pw


def send_restock_request(game: str, campaign_id: str, play_app_id: str | None) -> None:
    """Email the owner exactly what Play promo codes to create + how to reply."""
    product, ptype = PLAY_GIFT.get(game, ("your standard gift product", "one-time product"))
    end = (dt.date.today() + dt.timedelta(days=364)).isoformat()
    create_url = (
        f"https://play.google.com/console/u/0/developers/{mint_play_dev()}/app/"
        f"{play_app_id}/promotions/create" if play_app_id else "your Play Console → Promo codes → Create"
    )
    body = (
        f"Stagenator needs a Google Play code restock for {game}.\n\n"
        f"Google Play has no code-minting API (Console-only), so this one step is yours —\n"
        f"the agent handles everything else.\n\n"
        f"1) Create a promo code batch:\n"
        f"   • Product:   {product}\n"
        f"   • Type:      {ptype}\n"
        f"   • Count:     50\n"
        f"   • End date:  {end}\n"
        f"   • {create_url}\n\n"
        f"2) Download the CSV and REPLY to this email with it attached.\n"
        f"   (Keep the subject line — it routes the codes to the right campaign.)\n\n"
        f"The agent will import them automatically on its next run and confirm.\n\n"
        f"— Stagenator\nMission Control: https://stagenator-mission.web.app\n"
    )
    msg = EmailMessage()
    msg["Subject"] = f"[stagenator-restock:{campaign_id}] {game} Google Play codes low — reply with CSV"
    msg["From"] = f"Stagenator <{USER}>"
    msg["To"] = OWNER
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(USER, _pw())
        s.send_message(msg)
    state.ledger("action", game, action="restock_email", status="sent",
                 result={"campaign": campaign_id, "product": product})
    log.info("restock request emailed for %s / %s", game, campaign_id)


# Google Play developer-account id — used only to build a Console deep-link in
# the restock email (Play has no minting API; the owner creates codes there).
PLAY_DEV_ACCOUNT = "7030085917427251773"


def mint_play_dev() -> str:
    return PLAY_DEV_ACCOUNT


def poll_and_import() -> dict:
    """Read unseen replies carrying CSVs; import each to its campaign."""
    imported: dict = {}
    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(USER, _pw())
        m.select("INBOX")
        _typ, data = m.search(None, '(UNSEEN SUBJECT "stagenator-restock")')
        for num in data[0].split():
            _t, raw = m.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
            tag = TAG_RE.search(msg.get("Subject", ""))
            if not tag:
                continue
            campaign_id = tag.group(1)
            codes = _extract_codes(msg)
            if not codes:
                continue
            n = _import_codes(campaign_id, codes)
            imported[campaign_id] = n
            m.store(num, "+FLAGS", "\\Seen")
            state.ledger("action", None, action="restock_import", status="done",
                         result={"campaign": campaign_id, "codes": n, "via": "email reply"})
            _reply_confirm(m, msg, campaign_id, n)
    return {"imported": imported}


def _extract_codes(msg) -> list[str]:
    codes: list[str] = []
    for part in msg.walk():
        fn = (part.get_filename() or "").lower()
        if fn.endswith(".csv") or part.get_content_type() == "text/csv":
            text = part.get_payload(decode=True).decode("utf-8", "ignore")
            for line in text.splitlines()[1:]:
                c = line.split(",")[0].strip()
                if c and c.lower() != "promotion code":
                    codes.append(c)
    return codes


def _import_codes(campaign_id: str, codes: list[str]) -> int:
    import random
    import string
    import time

    tc = firestore.Client(project=config.TAKECODES_PROJECT)
    camp = tc.collection("campaigns").document(campaign_id)
    if not camp.get().exists:
        state.critical(f"restock reply for unknown campaign {campaign_id}")
        return 0
    end = (dt.date.today() + dt.timedelta(days=364)).isoformat()
    batch = tc.batch()
    for code in codes:
        cid = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        batch.set(camp.collection("codes").document(cid),
                  {"id": cid, "isTorn": False, "codeType": "one_time_code",
                   "createdAt": int(time.time() * 1000), "mintedBy": "stagenator",
                   "promotionEnd": end})
        batch.set(camp.collection("secrets").document(cid), {"code": code})
    batch.commit()
    camp.update({"availableCodes": firestore.Increment(len(codes))})
    return len(codes)


def _reply_confirm(m, original, campaign_id: str, n: int) -> None:
    reply = EmailMessage()
    reply["Subject"] = "Re: " + original.get("Subject", "")
    reply["From"] = f"Stagenator <{USER}>"
    reply["To"] = original.get("From", OWNER)
    reply.set_content(
        f"Imported {n} codes into campaign {campaign_id}. It's live again — "
        f"thanks!\n\nMission Control: https://stagenator-mission.web.app\n— Stagenator"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(USER, _pw())
        s.send_message(reply)
