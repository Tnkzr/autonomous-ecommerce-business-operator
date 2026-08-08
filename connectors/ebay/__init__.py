"""eBay Sell and Buy API integration.

Layering matches the other connectors: credentials -> auth -> transport ->
client -> connector. `client.py` mirrors the API; `connector.py` maps into
domain models and owns the safety rules.

Two things here are unlike the rest of the repo. eBay needs **two token kinds**
from one keyset (application for Browse, user for the Sell APIs), and its rate
limit is a **daily quota** rather than a refilling bucket — so the limiter
refuses instead of waiting. Both are documented where they live.
"""

from .auth import APPLICATION, USER, EbayAuthError, Token, TokenProvider
from .client import EbayClient, SellerProfile
from .connector import EbayConnector, EbayOrderSummary
from .credentials import (
    DEFAULT_ENVIRONMENT,
    DEFAULT_MARKETPLACE,
    ENVIRONMENTS,
    KNOWN_MARKETPLACES,
    REQUIRED_SCOPES,
    SETUP_REMEDY,
    ChainedCredentials,
    CredentialSource,
    CredentialsUnavailable,
    CredentialStatus,
    EbayCredentials,
    EnvCredentials,
    FileCredentials,
    StaticCredentials,
    UnavailableCredentials,
    default_source,
)
from .transport import (
    DailyQuota,
    EbayAPIError,
    EbayQuotaExhausted,
    QuotaState,
    Response,
    Transport,
)

__all__ = [
    "EbayConnector", "EbayClient", "SellerProfile", "EbayOrderSummary",
    "Transport", "Response", "DailyQuota", "QuotaState",
    "EbayAPIError", "EbayQuotaExhausted",
    "TokenProvider", "Token", "EbayAuthError", "APPLICATION", "USER",
    "EbayCredentials", "ENVIRONMENTS", "DEFAULT_ENVIRONMENT",
    "DEFAULT_MARKETPLACE", "KNOWN_MARKETPLACES", "REQUIRED_SCOPES",
    "SETUP_REMEDY", "CredentialSource", "CredentialStatus",
    "CredentialsUnavailable", "EnvCredentials", "FileCredentials",
    "StaticCredentials", "UnavailableCredentials", "ChainedCredentials",
    "default_source",
]
