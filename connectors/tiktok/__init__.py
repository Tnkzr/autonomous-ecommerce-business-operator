"""TikTok Shop Partner API integration."""
from .auth import TikTokAuthError, TikTokCredentials, TokenProvider
from .client import ShopInfo, TikTokShopClient
from .connector import TikTokShopConnector
from .credentials import (
    ChainedCredentials,
    CredentialSource,
    CredentialsUnavailable,
    CredentialStatus,
    EnvCredentials,
    FileCredentials,
    StaticCredentials,
    UnavailableCredentials,
    default_source,
)
from .importers import (
    ImportResult,
    daily_performance_from_orders,
    detect_export_kind,
    import_orders,
    import_products,
    import_settlements,
    parse_int,
    parse_money,
)
from .regions import REGIONS, UnknownRegion, resolve
from .signing import canonical_string, sign_request
from .transport import TikTokAPIError, TikTokRateLimited, TokenBucket, Transport

__all__ = [
    "TikTokShopConnector", "TikTokShopClient", "Transport", "TokenProvider",
    "TikTokCredentials", "TikTokAuthError", "TikTokAPIError", "TikTokRateLimited",
    "TokenBucket", "ShopInfo", "REGIONS", "UnknownRegion", "resolve",
    "sign_request", "canonical_string",
    "CredentialSource", "CredentialStatus", "CredentialsUnavailable",
    "EnvCredentials", "FileCredentials", "StaticCredentials",
    "UnavailableCredentials", "ChainedCredentials", "default_source",
    "ImportResult", "import_orders", "import_settlements", "import_products",
    "detect_export_kind", "daily_performance_from_orders", "parse_money",
    "parse_int",
]
