"""Amazon Selling Partner API integration."""
from .auth import LWACredentials, LWAError, TokenProvider
from .client import FeeBreakdown, SPAPIClient
from .connector import AmazonConnector
from .regions import MARKETPLACES, UnknownMarketplace, resolve
from .transport import RateLimited, SPAPIError, TokenBucket, Transport

__all__ = [
    "AmazonConnector", "SPAPIClient", "Transport", "TokenProvider",
    "LWACredentials", "LWAError", "SPAPIError", "RateLimited", "TokenBucket",
    "FeeBreakdown", "MARKETPLACES", "UnknownMarketplace", "resolve",
]
