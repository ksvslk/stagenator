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
        os.environ.get("ASC_KEY_CONTENT") or Path(os.environ["ASC_KEY_PATH"]).read_text(),
        algorithm="ES256",
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


def find_or_create_iap_offer(iap_id: str) -> str:
    """Reuse or create the 'Stagenator Gift' free offer on a one-time IAP."""
    offers = requests.get(f"{API.replace('/v1','')}/v2/inAppPurchases/{iap_id}/offerCodes",
                          headers={"Authorization": f"Bearer {_token()}"}, timeout=60).json().get("data", [])
    for o in offers:
        if o["attributes"].get("name") == OFFER_NAME and o["attributes"].get("active", True):
            return o["id"]

    pts = requests.get(
        f"{API.replace('/v1','')}/v2/inAppPurchases/{iap_id}/pricePoints"
        "?filter[territory]=USA&limit=200&fields[inAppPurchasePricePoints]=customerPrice",
        headers={"Authorization": f"Bearer {_token()}"}, timeout=60).json()["data"]
    free_pp = next(p["id"] for p in pts if float(p["attributes"]["customerPrice"]) == 0.0)

    created = _req("POST", "/inAppPurchaseOfferCodes", json={
        "data": {"type": "inAppPurchaseOfferCodes",
                 "attributes": {"name": OFFER_NAME,
                                "customerEligibilities": ["NON_SPENDER", "ACTIVE_SPENDER", "CHURNED_SPENDER"]},
                 "relationships": {
                     "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                     "prices": {"data": [{"type": "inAppPurchaseOfferPrices", "id": "${price-1}"}]}}},
        "included": [{"type": "inAppPurchaseOfferPrices", "id": "${price-1}",
                      "relationships": {
                          "territory": {"data": {"type": "territories", "id": "USA"}},
                          "pricePoint": {"data": {"type": "inAppPurchasePricePoints", "id": free_pp}}}}]})
    return created["data"]["id"]


def mint_iap_offer_codes(iap_id: str, expiry_days: int = 180) -> tuple[list[tuple[str, str]], str]:
    """Mint a one-time-use batch (Apple minimum granularity: 500) on the gift offer.

    Returns ([(code, redeem_url), ...], expiration_date). The values CSV from
    Apple already carries per-code redeem URLs (ctx=offercodes deep links).
    """
    offer_id = find_or_create_iap_offer(iap_id)
    expiration = (dt.date.today() + dt.timedelta(days=expiry_days)).isoformat()
    batch = _req("POST", "/inAppPurchaseOfferCodeOneTimeUseCodes", json={
        "data": {"type": "inAppPurchaseOfferCodeOneTimeUseCodes",
                 "attributes": {"numberOfCodes": 500, "expirationDate": expiration},
                 "relationships": {"offerCode": {"data": {"type": "inAppPurchaseOfferCodes", "id": offer_id}}}}})
    batch_id = batch["data"]["id"]
    for _ in range(12):
        time.sleep(6)
        r = requests.get(f"{API}/inAppPurchaseOfferCodeOneTimeUseCodes/{batch_id}/values",
                         headers={"Authorization": f"Bearer {_token()}", "Accept": "text/csv"}, timeout=120)
        if r.status_code == 200 and r.text.strip():
            rows = [tuple(x.strip() for x in line.split(",", 1))
                    for line in r.text.strip().splitlines()
                    if "," in line and "code" not in line.lower()[:20]]
            if rows:
                log.info("minted %d IAP offer codes for iap %s", len(rows), iap_id)
                return rows, expiration
    raise RuntimeError(f"IAP offer batch {batch_id} values not ready")
