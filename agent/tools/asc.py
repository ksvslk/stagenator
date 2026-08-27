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
        {
            "iss": os.environ["ASC_ISSUER_ID"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,
            "aud": "appstoreconnect-v1",
        },
        os.environ.get("ASC_KEY_CONTENT")
        or Path(os.environ["ASC_KEY_PATH"]).read_text(),
        algorithm="ES256",
        headers={"kid": os.environ["ASC_KEY_ID"]},
    )


def _req(method: str, path: str, **kw) -> dict:
    r = requests.request(
        method,
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        timeout=60,
        **kw,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"ASC {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


def list_products(app_store_id: str) -> list[dict]:
    """The app's reward catalog: approved one-time IAPs + subscriptions."""
    products = []
    iaps = _req("GET", f"/apps/{app_store_id}/inAppPurchasesV2?limit=50").get(
        "data", []
    )
    for i in iaps:
        a = i["attributes"]
        if a.get("state") == "APPROVED":
            products.append(
                {
                    "id": i["id"],
                    "productId": a.get("productId"),
                    "name": a.get("name"),
                    "kind": "iap",
                    "type": a.get("inAppPurchaseType"),
                }
            )
    for g in _req("GET", f"/apps/{app_store_id}/subscriptionGroups").get("data", []):
        for s_ in _req("GET", f"/subscriptionGroups/{g['id']}/subscriptions").get(
            "data", []
        ):
            a = s_["attributes"]
            if a.get("state") == "APPROVED":
                products.append(
                    {
                        "id": s_["id"],
                        "productId": a.get("productId"),
                        "name": a.get("name"),
                        "kind": "subscription",
                        "period": a.get("subscriptionPeriod"),
                    }
                )
    return products


def find_or_create_subscription_offer(subscription_id: str) -> str:
    """Free-trial (1 month) offer on a subscription. The prices ceremony that
    Apple actually accepts for FREE_TRIAL: inline entries with territory only,
    subscriptionPricePoint null."""
    offers = _req("GET", f"/subscriptions/{subscription_id}/offerCodes").get("data", [])
    for o in offers:
        if o["attributes"].get("name") == OFFER_NAME and o["attributes"].get(
            "active", True
        ):
            return o["id"]
    created = _req(
        "POST",
        "/subscriptionOfferCodes",
        json={
            "data": {
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
                    "subscription": {
                        "data": {"type": "subscriptions", "id": subscription_id}
                    },
                    "prices": {
                        "data": [{"type": "subscriptionOfferCodePrices", "id": "${p1}"}]
                    },
                },
            },
            "included": [
                {
                    "type": "subscriptionOfferCodePrices",
                    "id": "${p1}",
                    "relationships": {
                        "territory": {"data": {"type": "territories", "id": "USA"}}
                    },
                }
            ],
        },
    )
    return created["data"]["id"]


def mint_subscription_offer_codes(
    subscription_id: str, expiry_days: int = 180
) -> tuple[list[tuple[str, str]], str]:
    """One-time-use batch (500) on the subscription gift offer; returns rows+expiry."""
    offer_id = find_or_create_subscription_offer(subscription_id)
    expiration = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=expiry_days)).isoformat()
    batch = _req(
        "POST",
        "/subscriptionOfferCodeOneTimeUseCodes",
        json={
            "data": {
                "type": "subscriptionOfferCodeOneTimeUseCodes",
                "attributes": {"numberOfCodes": 500, "expirationDate": expiration},
                "relationships": {
                    "offerCode": {
                        "data": {"type": "subscriptionOfferCodes", "id": offer_id}
                    }
                },
            }
        },
    )
    batch_id = batch["data"]["id"]
    for _ in range(12):
        time.sleep(6)
        r = requests.get(
            f"{API}/subscriptionOfferCodeOneTimeUseCodes/{batch_id}/values",
            headers={"Authorization": f"Bearer {_token()}", "Accept": "text/csv"},
            timeout=120,
        )
        if r.status_code == 200 and r.text.strip():
            rows = [
                tuple(x.strip() for x in line.split(",", 1))
                for line in r.text.strip().splitlines()
                if "," in line and "code" not in line.lower()[:20]
            ]
            if rows:
                return rows, expiration
    raise RuntimeError(f"subscription offer batch {batch_id} values not ready")


def find_or_create_iap_offer(iap_id: str) -> str:
    """Reuse or create the 'Stagenator Gift' free offer on a one-time IAP."""
    offers = (
        requests.get(
            f"{API.replace('/v1', '')}/v2/inAppPurchases/{iap_id}/offerCodes",
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=60,
        )
        .json()
        .get("data", [])
    )
    for o in offers:
        if o["attributes"].get("name") == OFFER_NAME and o["attributes"].get(
            "active", True
        ):
            return o["id"]

    pts = requests.get(
        f"{API.replace('/v1', '')}/v2/inAppPurchases/{iap_id}/pricePoints"
        "?filter[territory]=USA&limit=200&fields[inAppPurchasePricePoints]=customerPrice",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=60,
    ).json()["data"]
    free_pp = next(
        p["id"] for p in pts if float(p["attributes"]["customerPrice"]) == 0.0
    )

    created = _req(
        "POST",
        "/inAppPurchaseOfferCodes",
        json={
            "data": {
                "type": "inAppPurchaseOfferCodes",
                "attributes": {
                    "name": OFFER_NAME,
                    "customerEligibilities": [
                        "NON_SPENDER",
                        "ACTIVE_SPENDER",
                        "CHURNED_SPENDER",
                    ],
                },
                "relationships": {
                    "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                    "prices": {
                        "data": [
                            {"type": "inAppPurchaseOfferPrices", "id": "${price-1}"}
                        ]
                    },
                },
            },
            "included": [
                {
                    "type": "inAppPurchaseOfferPrices",
                    "id": "${price-1}",
                    "relationships": {
                        "territory": {"data": {"type": "territories", "id": "USA"}},
                        "pricePoint": {
                            "data": {"type": "inAppPurchasePricePoints", "id": free_pp}
                        },
                    },
                }
            ],
        },
    )
    return created["data"]["id"]


def mint_iap_offer_codes(
    iap_id: str, expiry_days: int = 180
) -> tuple[list[tuple[str, str]], str]:
    """Mint a one-time-use batch (Apple minimum granularity: 500) on the gift offer.

    Returns ([(code, redeem_url), ...], expiration_date). The values CSV from
    Apple already carries per-code redeem URLs (ctx=offercodes deep links).
    """
    offer_id = find_or_create_iap_offer(iap_id)
    expiration = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=expiry_days)).isoformat()
    batch = _req(
        "POST",
        "/inAppPurchaseOfferCodeOneTimeUseCodes",
        json={
            "data": {
                "type": "inAppPurchaseOfferCodeOneTimeUseCodes",
                "attributes": {"numberOfCodes": 500, "expirationDate": expiration},
                "relationships": {
                    "offerCode": {
                        "data": {"type": "inAppPurchaseOfferCodes", "id": offer_id}
                    }
                },
            }
        },
    )
    batch_id = batch["data"]["id"]
    for _ in range(12):
        time.sleep(6)
        r = requests.get(
            f"{API}/inAppPurchaseOfferCodeOneTimeUseCodes/{batch_id}/values",
            headers={"Authorization": f"Bearer {_token()}", "Accept": "text/csv"},
            timeout=120,
        )
        if r.status_code == 200 and r.text.strip():
            rows = [
                tuple(x.strip() for x in line.split(",", 1))
                for line in r.text.strip().splitlines()
                if "," in line and "code" not in line.lower()[:20]
            ]
            if rows:
                log.info("minted %d IAP offer codes for iap %s", len(rows), iap_id)
                return rows, expiration
    raise RuntimeError(f"IAP offer batch {batch_id} values not ready")
