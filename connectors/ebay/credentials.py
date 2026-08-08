"""eBay's credential spec.

eBay is the one provider here where **two different tokens** are needed, and
conflating them is the mistake that costs an afternoon:

- An **application token** (client-credentials grant) authenticates the *app*.
  It is what the Browse API wants for public catalogue and competitor data.
- A **user token** (refresh-token grant) authenticates the *seller*. Every Sell
  API — orders, inventory, finances — needs this one.

They come from the same endpoint with the same client credentials and are not
interchangeable: calling Browse with a user token mostly works, calling
Fulfillment with an application token returns a 403 that reads like a missing
scope. `auth.py` keeps them apart; this module just declares what is needed.

The other trap is expiry asymmetry. A user access token lasts about two hours,
so it must be refreshed constantly and that is routine. The **refresh token
lasts about 18 months and does not rotate** — unlike TikTok, nothing writes a
new one back, and when it finally expires the only fix is a human re-consenting
through the browser. That failure lands with no deploy to correlate against, so
`auth.py` reports the age of the grant rather than waiting to be surprised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..credentials import (
    ChainedCredentials,
    CredentialSource,
    CredentialSpec,
    CredentialStatus,
    CredentialsUnavailable,
)
from ..credentials import EnvCredentials as _EnvCredentials
from ..credentials import FileCredentials as _FileCredentials
from ..credentials import StaticCredentials as _StaticCredentials
from ..credentials import UnavailableCredentials as _UnavailableCredentials

# eBay runs a separate sandbox with its own hosts, its own credentials, and its
# own catalogue. A sandbox key against a production host authenticates fine and
# returns nothing, which reads as "no sales" rather than as a wrong environment.
ENVIRONMENTS = {
    "production": {
        "auth": "https://api.ebay.com/identity/v1/oauth2/token",
        "api": "https://api.ebay.com",
        "consent": "https://auth.ebay.com/oauth2/authorize",
    },
    "sandbox": {
        "auth": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "api": "https://api.sandbox.ebay.com",
        "consent": "https://auth.sandbox.ebay.com/oauth2/authorize",
    },
}

DEFAULT_ENVIRONMENT = "production"

# The site the seller trades on. Sent as X-EBAY-C-MARKETPLACE-ID on nearly
# every call; the wrong value returns another country's listings and prices,
# which silently corrupts every competitor comparison built on them.
DEFAULT_MARKETPLACE = "EBAY_US"

KNOWN_MARKETPLACES = (
    "EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_AU", "EBAY_CA", "EBAY_FR",
    "EBAY_IT", "EBAY_ES", "EBAY_IE", "EBAY_NL", "EBAY_PL", "EBAY_BE",
    "EBAY_AT", "EBAY_CH", "EBAY_HK", "EBAY_SG", "EBAY_MY", "EBAY_PH",
)

# Scopes the connector's operations need. Requested at consent time; a token
# minted without one fails at the point of use with a 403 that looks like a
# credential problem rather than a scope problem.
REQUIRED_SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
)

REQUIRED_FIELDS = ("client_id", "client_secret", "refresh_token")
OPTIONAL_FIELDS = ("environment", "marketplace_id", "granted_at")

ENV_KEYS = {
    "client_id": "EBAY_CLIENT_ID",
    "client_secret": "EBAY_CLIENT_SECRET",
    "refresh_token": "EBAY_REFRESH_TOKEN",
    "environment": "EBAY_ENVIRONMENT",
    "marketplace_id": "EBAY_MARKETPLACE_ID",
    "granted_at": "EBAY_REFRESH_TOKEN_GRANTED_AT",
}

SETUP_REMEDY = (
    "eBay's developer programme is free and open — no eligibility review:\n"
    "  1. developer.ebay.com > Register, then Application Keys.\n"
    "  2. Create a keyset. The 'App ID (Client ID)' is EBAY_CLIENT_ID and the\n"
    "     'Cert ID (Client Secret)' is EBAY_CLIENT_SECRET. Take the PRODUCTION\n"
    "     keyset, not sandbox, unless you mean to test against sandbox.\n"
    "  3. Under the keyset, 'User Tokens' > 'Get a Token from eBay via Your\n"
    "     Application'. Set an RuName (redirect), then run the consent flow and\n"
    "     accept as your selling account.\n"
    "  4. Exchange the authorisation code for a refresh token; that long string\n"
    "     is EBAY_REFRESH_TOKEN.\n"
    "Set EBAY_MARKETPLACE_ID to the site you sell on (default EBAY_US).\n"
    "Record EBAY_REFRESH_TOKEN_GRANTED_AT as today's date (YYYY-MM-DD): the\n"
    "refresh token expires about 18 months later and cannot be renewed without\n"
    "a human repeating step 3, so the operator warns before it lapses."
)


@dataclass
class EbayCredentials:
    client_id: str
    client_secret: str
    refresh_token: str
    environment: str = DEFAULT_ENVIRONMENT
    marketplace_id: str = DEFAULT_MARKETPLACE
    granted_at: str = ""

    def __post_init__(self) -> None:
        self.environment = (self.environment or DEFAULT_ENVIRONMENT).strip().lower()
        if self.environment not in ENVIRONMENTS:
            raise ValueError(
                f"Unknown eBay environment {self.environment!r}. "
                f"Expected one of {', '.join(ENVIRONMENTS)}.")
        self.marketplace_id = (self.marketplace_id or DEFAULT_MARKETPLACE).strip().upper()

    @property
    def auth_url(self) -> str:
        return ENVIRONMENTS[self.environment]["auth"]

    @property
    def api_base(self) -> str:
        return ENVIRONMENTS[self.environment]["api"]

    @property
    def is_sandbox(self) -> bool:
        return self.environment == "sandbox"

    def __repr__(self) -> str:
        # Both the secret and the refresh token are credentials. This object
        # ends up in tracebacks; neither may be interpolated.
        return (f"EbayCredentials(client_id={self.client_id!r}, "
                "client_secret=<redacted>, refresh_token=<redacted>, "
                f"environment={self.environment!r}, "
                f"marketplace_id={self.marketplace_id!r})")


SPEC = CredentialSpec(
    provider="ebay",
    required=REQUIRED_FIELDS,
    optional=OPTIONAL_FIELDS,
    env_keys=ENV_KEYS,
    build=lambda v: EbayCredentials(
        client_id=v["client_id"],
        client_secret=v["client_secret"],
        refresh_token=v["refresh_token"],
        environment=v.get("environment") or DEFAULT_ENVIRONMENT,
        marketplace_id=v.get("marketplace_id") or DEFAULT_MARKETPLACE,
        granted_at=v.get("granted_at", ""),
    ),
    # Deliberately None. eBay does not rotate the refresh token on use, so
    # there is nothing to write back — the renewal is a human re-consent, not a
    # value the operator can persist.
    rotating_field=None,
    remedy=SETUP_REMEDY,
)


class EnvCredentials(_EnvCredentials):
    def __init__(self) -> None:
        super().__init__(SPEC)


class FileCredentials(_FileCredentials):
    def __init__(self, path: str | Path) -> None:
        super().__init__(SPEC, path)


class StaticCredentials(_StaticCredentials):
    def __init__(self, credentials: EbayCredentials) -> None:
        super().__init__(SPEC, credentials)


class UnavailableCredentials(_UnavailableCredentials):
    def __init__(self, reason: str = "eBay API access is not configured.",
                 *, remedy: str = SETUP_REMEDY) -> None:
        super().__init__(SPEC, reason, remedy=remedy)


def default_source(*, credential_file: str | Path | None = None) -> CredentialSource:
    sources: list[CredentialSource] = []
    path = credential_file or os.environ.get("EBAY_CREDENTIAL_FILE")
    if path:
        sources.append(FileCredentials(path))
    sources.append(EnvCredentials())
    sources.append(UnavailableCredentials())
    return ChainedCredentials(*sources)


__all__ = [
    "SPEC", "EbayCredentials", "ENVIRONMENTS", "DEFAULT_ENVIRONMENT",
    "DEFAULT_MARKETPLACE", "KNOWN_MARKETPLACES", "REQUIRED_SCOPES",
    "REQUIRED_FIELDS", "OPTIONAL_FIELDS", "ENV_KEYS", "SETUP_REMEDY",
    "CredentialSource", "CredentialStatus", "CredentialsUnavailable",
    "EnvCredentials", "FileCredentials", "StaticCredentials",
    "UnavailableCredentials", "ChainedCredentials", "default_source",
]
