"""SP-API regional endpoints and marketplace identifiers.

Getting the region wrong is a silent class of bug: the call authenticates fine
and returns an empty result set, because the seller simply has no data in that
region. So region is derived from the marketplace ID rather than configured
separately, and an unknown marketplace ID raises instead of defaulting to NA.
"""

from __future__ import annotations

# Regional API hosts.
ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

# Amazon's sandbox mirrors the production paths but returns canned data.
SANDBOX_ENDPOINTS = {
    "na": "https://sandbox.sellingpartnerapi-na.amazon.com",
    "eu": "https://sandbox.sellingpartnerapi-eu.amazon.com",
    "fe": "https://sandbox.sellingpartnerapi-fe.amazon.com",
}

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# marketplace_id -> (country_code, region, currency)
MARKETPLACES = {
    # North America
    "ATVPDKIKX0DER": ("US", "na", "USD"),
    "A2EUQ1WTGCTBG2": ("CA", "na", "CAD"),
    "A1AM78C64UM0Y8": ("MX", "na", "MXN"),
    "A2Q3Y263D00KWC": ("BR", "na", "BRL"),
    # Europe
    "A1F83G8C2ARO7P": ("UK", "eu", "GBP"),
    "A1PA6795UKMFR9": ("DE", "eu", "EUR"),
    "A13V1IB3VIYZZH": ("FR", "eu", "EUR"),
    "APJ6JRA9NG5V4": ("IT", "eu", "EUR"),
    "A1RKKUPIHCS9HS": ("ES", "eu", "EUR"),
    "A1805IZSGTT6HS": ("NL", "eu", "EUR"),
    "A2NODRKZP88ZB9": ("SE", "eu", "SEK"),
    "A1C3SOZRARQ6R3": ("PL", "eu", "PLN"),
    "AMEN7PMS3EDWL": ("BE", "eu", "EUR"),
    "A28R8C7NBKEWEA": ("IE", "eu", "EUR"),
    "A33AVAJ2PDY3EV": ("TR", "eu", "TRY"),
    "A2VIGQ35RCS4UG": ("AE", "eu", "AED"),
    "A17E79C6D8DWNP": ("SA", "eu", "SAR"),
    "ARBP9OOSHTCHU": ("EG", "eu", "EGP"),
    "A21TJRUUN4KGV": ("IN", "eu", "INR"),
    # Far East
    "A1VC38T7YXB528": ("JP", "fe", "JPY"),
    "A39IBJ37TRP1C6": ("AU", "fe", "AUD"),
    "A19VAU5U5O7RUS": ("SG", "fe", "SGD"),
}

COUNTRY_TO_MARKETPLACE = {country: mid for mid, (country, _, _) in MARKETPLACES.items()}


class UnknownMarketplace(ValueError):
    """Raised for a marketplace ID we cannot map to a region."""


def resolve(marketplace_id: str, *, sandbox: bool = False) -> tuple[str, str, str]:
    """Return (endpoint_url, country_code, currency) for a marketplace ID."""
    mid = (marketplace_id or "").strip()
    if mid not in MARKETPLACES:
        raise UnknownMarketplace(
            f"Marketplace ID {mid!r} is not recognised. "
            f"Known IDs: {', '.join(sorted(MARKETPLACES))}. "
            "Set AMZ_MARKETPLACE_ID to the ID for the country you sell in "
            "(US is ATVPDKIKX0DER). Guessing the region would return empty "
            "results that look like 'no sales' rather than a configuration error."
        )
    country, region, currency = MARKETPLACES[mid]
    table = SANDBOX_ENDPOINTS if sandbox else ENDPOINTS
    return table[region], country, currency
