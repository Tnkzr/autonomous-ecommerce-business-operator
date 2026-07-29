"""TikTok Shop Partner API integration."""
from .auth import TikTokAuthError, TikTokCredentials, TokenProvider
from .client import ShopInfo, TikTokShopClient
from .connector import TikTokShopConnector
from .regions import REGIONS, UnknownRegion, resolve
from .signing import canonical_string, sign_request
from .transport import TikTokAPIError, TikTokRateLimited, TokenBucket, Transport

__all__ = [
    "TikTokShopConnector", "TikTokShopClient", "Transport", "TokenProvider",
    "TikTokCredentials", "TikTokAuthError", "TikTokAPIError", "TikTokRateLimited",
    "TokenBucket", "ShopInfo", "REGIONS", "UnknownRegion", "resolve",
    "sign_request", "canonical_string",
]
