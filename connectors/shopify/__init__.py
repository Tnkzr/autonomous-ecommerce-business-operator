"""Shopify Admin API integration (GraphQL).

Layering matches the other connectors: credentials -> transport -> client ->
connector. `client.py` mirrors the API; `connector.py` maps into domain models
and owns the safety rules. Keep new endpoints in the layer they belong to.
"""

from .client import ShopifyClient, ShopInfo
from .connector import (
    ShopifyConnector,
    ShopifyOrderSummary,
    TrafficAttribution,
    classify_traffic_source,
    handleize,
)
from .credentials import (
    DEFAULT_API_VERSION,
    REQUIRED_SCOPES,
    SETUP_REMEDY,
    ChainedCredentials,
    CredentialSource,
    CredentialsUnavailable,
    CredentialStatus,
    EnvCredentials,
    FileCredentials,
    ShopifyCredentials,
    StaticCredentials,
    UnavailableCredentials,
    default_source,
)
from .transport import (
    CostLimiter,
    Response,
    ShopifyAPIError,
    ShopifyThrottled,
    ShopifyUserError,
    ThrottleStatus,
    Transport,
    normalise_domain,
)

__all__ = [
    "ShopifyConnector", "ShopifyClient", "ShopInfo", "Transport", "Response",
    "ShopifyAPIError", "ShopifyThrottled", "ShopifyUserError", "CostLimiter",
    "ThrottleStatus", "normalise_domain", "ShopifyCredentials",
    "DEFAULT_API_VERSION", "REQUIRED_SCOPES", "SETUP_REMEDY",
    "CredentialSource", "CredentialStatus", "CredentialsUnavailable",
    "EnvCredentials", "FileCredentials", "StaticCredentials",
    "UnavailableCredentials", "ChainedCredentials", "default_source",
    "ShopifyOrderSummary", "TrafficAttribution", "classify_traffic_source",
    "handleize",
]
