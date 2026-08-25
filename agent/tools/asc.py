"""App Store Connect API — autonomous subscription offer-code minting.

Apple's classic promo codes are UI-only, but subscription OFFER codes are fully
API-mintable: create/reuse a subscriptionOfferCode (e.g. 1 month free) on the
app's subscription, then batch one-time-use codes and read their values back.
This makes iOS replenish zero-human. One-time-use offer codes expire at the
batch's expirationDate (we set ~6 months; Apple caps offer-code redemption
windows), tracked as promotionEnd in the campaign import.
"""

import datetime as dt
import logging
import os
import time
from pathlib import Path

import jwt
import requests

log = logging.getLogger("stagenator.asc")

API = "https://api.appstoreconnect.apple.com/v1"
OFFER_NAME = "Stagenator Gift"


def _token() -> str:
    return jwt.encode(
        {"iss": os.environ["ASC_ISSUER_ID"], "iat": int(time.time()),
         "exp": int(time.time()) + 900, "aud": "appstoreconnect-v1"},
        Path(os.environ["ASC_KEY_PATH"]).read_text(), algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"]},
    )


def _req(method: str, path: str, **kw) -> dict:
    r = requests.request(method, f"{API}{path}",
                         headers={"Authorization": f"Bearer {_token()}",
                                  "Content-Type": "application/json"},
                         timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"ASC {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


def monthly_subscription(app_store_id: str) -> dict:
    """The app's monthly (shortest-period) approved subscription."""
    groups = _req("GET", f"/apps/{app_store_id}/subscriptionGroups")["data"]
    subs = []
    for g in groups:
        subs += _req("GET", f"/subscriptionGroups/{g['id']}/subscriptions")["data"]
    monthly = [s for s in subs if s["attributes"].get("subscriptionPeriod") == "ONE_MONTH"] or subs
    if not monthly:
        raise RuntimeError(f"no subscriptions on app {app_store_id}")
    return monthly[0]


def _find_or_create_offer(subscription_id: str) -> str:
    offers = _req("GET", f"/subscriptions/{subscription_id}/offerCodes").get("data", [])
    for o in offers:
        if o["attributes"].get("name") == OFFER_NAME and o["attributes"].get("active"):
            return o["id"]
    created = _req("POST", "/subscriptionOfferCodes", json={"data": {
        "type": "subscriptionOfferCodes",
        "attributes": {
            "name": OFFER_NAME,
            "customerEligibilities": ["NEW", "EXISTING", "EXPIRED"],
            "offerEligibility": "STACK_WITH_INTRO_OFFERS",
            "duration": "ONE_MONTH",
            "offerMode": "FREE_TRIAL",
            "numberOfPeriods": 1,
        },
        "relationships": {
            "subscription": {"data": {"type": "subscriptions", "id": subscription_id}},
            # FREE_TRIAL offers carry no price points, but the relationship is mandatory
            "prices": {"data": []},
        },
    }})
    return created["data"]["id"]


def mint_offer_codes(app_store_id: str, n: int = 25, expiry_days: int = 180) -> tuple[list[str], str]:
    """Create a one-time-use code batch; returns (codes, expiration_date)."""
    sub = monthly_subscription(app_store_id)
    offer_id = _find_or_create_offer(sub["id"])
    expiration = (dt.date.today() + dt.timedelta(days=expiry_days)).isoformat()
    batch = _req("POST", "/subscriptionOfferCodeOneTimeUseCodes", json={"data": {
        "type": "subscriptionOfferCodeOneTimeUseCodes",
        "attributes": {"numberOfCodes": n, "expirationDate": expiration},
        "relationships": {"offerCode": {"data": {"type": "subscriptionOfferCodes", "id": offer_id}}},
    }})
    batch_id = batch["data"]["id"]

    # values endpoint returns CSV; brief propagation delay is normal
    for attempt in range(6):
        time.sleep(5)
        r = requests.get(f"{API}/subscriptionOfferCodeOneTimeUseCodes/{batch_id}/values",
                         headers={"Authorization": f"Bearer {_token()}", "Accept": "text/csv"},
                         timeout=60)
        if r.status_code == 200 and r.text.strip():
            codes = [line.strip() for line in r.text.strip().splitlines()
                     if line.strip() and "code" not in line.lower()]
            if codes:
                log.info("minted %d offer codes for app %s (sub %s)", len(codes),
                         app_store_id, sub["attributes"].get("productId"))
                return codes, expiration
    raise RuntimeError(f"offer code batch {batch_id} values not ready after retries")
