"""TikTok Shop endpoints, regions, and API versions.

TikTok versions endpoints by date in the path (`/product/202309/products`)
rather than by header. Versions are pinned here as named constants so an
upgrade is a deliberate one-line change with a commit message, not a string
edited in passing somewhere in the client.
"""

from __future__ import annotations

# Single global gateway; the shop's region is determined by the shop_cipher,
# not by the host.
API_BASE = "https://open-api.tiktokglobalshop.com"
SANDBOX_BASE = "https://open-api-sandbox.tiktokglobalshop.com"

AUTH_BASE = "https://auth.tiktok-shops.com"
TOKEN_PATH = "/api/v2/token/get"
REFRESH_PATH = "/api/v2/token/refresh"

# Pinned API versions. Bump deliberately — TikTok changes response shapes
# between versions without deprecating the old one immediately.
V_AUTH = "202309"
V_PRODUCT = "202309"
V_PRODUCT_CATEGORY = "202309"
V_ORDER = "202309"
V_FINANCE = "202309"
V_ANALYTICS = "202405"
V_FULFILMENT = "202309"

# region -> (currency, typical settlement lag in days)
# Settlement lag matters for cash-flow modelling: TikTok holds funds until
# after the return window closes, which is materially longer than Amazon.
REGIONS = {
    "US": ("USD", 15),
    "GB": ("GBP", 14),
    "ID": ("IDR", 14),
    "MY": ("MYR", 14),
    "TH": ("THB", 14),
    "VN": ("VND", 14),
    "PH": ("PHP", 14),
    "SG": ("SGD", 14),
    "JP": ("JPY", 14),
    "DE": ("EUR", 14),
    "FR": ("EUR", 14),
    "IT": ("EUR", 14),
    "ES": ("EUR", 14),
    "IE": ("EUR", 14),
    "MX": ("MXN", 14),
    "BR": ("BRL", 14),
}

DEFAULT_REGION = "US"


class UnknownRegion(ValueError):
    """Raised for a region code we cannot map to a currency."""


def resolve(region: str | None = None, *, sandbox: bool = False) -> tuple[str, str, int]:
    """Return (base_url, currency, settlement_lag_days)."""
    code = (region or DEFAULT_REGION).strip().upper()
    if code not in REGIONS:
        raise UnknownRegion(
            f"Region {code!r} is not a known TikTok Shop market. "
            f"Known: {', '.join(sorted(REGIONS))}. Set TIKTOK_REGION to the market "
            "your shop sells in — the currency and settlement lag both depend on it, "
            "and a wrong currency silently corrupts every profit calculation."
        )
    currency, lag = REGIONS[code]
    return (SANDBOX_BASE if sandbox else API_BASE), currency, lag
