"""Shopify's credential spec.

Shopify has two auth models and only one of them is right here. A **public app**
does the OAuth dance and receives a token per install; a **custom app** created
inside the merchant's own admin issues a permanent Admin API access token. This
operator runs one store owned by the operator, so a custom app is the correct
shape: no callback URL to host, no OAuth round trip, and no token expiry.

The token is store-specific and is revoked the moment the app is uninstalled,
which is worth knowing because a revoked token produces a 401 that looks exactly
like a typo.

Unlike TikTok, nothing here rotates — so `rotating_field` is None and the
environment is a perfectly good source.
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

# Pinned deliberately. Shopify supports a version for roughly 12 months and then
# removes it; an unpinned client silently moves to new behaviour on someone
# else's schedule, which is how a working listing pipeline breaks on a Tuesday.
# Bump this on purpose, after reading the changelog for breaking changes.
DEFAULT_API_VERSION = "2025-07"

REQUIRED_FIELDS = ("shop_domain", "access_token")
OPTIONAL_FIELDS = ("api_version",)

ENV_KEYS = {
    "shop_domain": "SHOPIFY_SHOP_DOMAIN",
    "access_token": "SHOPIFY_ADMIN_ACCESS_TOKEN",
    "api_version": "SHOPIFY_API_VERSION",
}

SETUP_REMEDY = (
    "Create a custom app in your own store — no developer-programme approval "
    "and no eligibility review, unlike a marketplace API:\n"
    "  1. Shopify admin > Settings > Apps and sales channels > Develop apps.\n"
    "  2. 'Allow custom app development' (once per store), then 'Create an app'.\n"
    "  3. Configuration > Admin API integration > select these scopes:\n"
    "     read_products, write_products, read_inventory, write_inventory,\n"
    "     read_orders, read_customers, read_price_rules, write_price_rules,\n"
    "     read_publications, write_publications, read_locations.\n"
    "  4. Install app, then reveal the Admin API access token once (it starts\n"
    "     'shpat_' and is shown a single time).\n"
    "Set SHOPIFY_SHOP_DOMAIN to your myshopify domain (e.g. my-store.myshopify.com) "
    "and SHOPIFY_ADMIN_ACCESS_TOKEN to that token."
)

# The scopes the connector's operations actually need. Listed so a 403 can be
# explained as a missing scope rather than a bad token — they are different
# problems with different fixes, and scopes can only be changed by reinstalling.
REQUIRED_SCOPES = (
    "read_products", "write_products",
    "read_inventory", "write_inventory",
    "read_orders", "read_customers",
    "read_price_rules", "write_price_rules",
    "read_publications", "write_publications",
    "read_locations",
)


@dataclass
class ShopifyCredentials:
    shop_domain: str
    access_token: str
    api_version: str = DEFAULT_API_VERSION

    def __post_init__(self) -> None:
        if not self.api_version:
            self.api_version = DEFAULT_API_VERSION

    def __repr__(self) -> str:
        # Never interpolate the token: this object appears in tracebacks.
        return (f"ShopifyCredentials(shop_domain={self.shop_domain!r}, "
                f"access_token=<redacted>, api_version={self.api_version!r})")


SPEC = CredentialSpec(
    provider="shopify",
    required=REQUIRED_FIELDS,
    optional=OPTIONAL_FIELDS,
    env_keys=ENV_KEYS,
    build=lambda v: ShopifyCredentials(
        shop_domain=v["shop_domain"],
        access_token=v["access_token"],
        api_version=v.get("api_version") or DEFAULT_API_VERSION,
    ),
    remedy=SETUP_REMEDY,
)


class EnvCredentials(_EnvCredentials):
    def __init__(self) -> None:
        super().__init__(SPEC)


class FileCredentials(_FileCredentials):
    def __init__(self, path: str | Path) -> None:
        super().__init__(SPEC, path)


class StaticCredentials(_StaticCredentials):
    def __init__(self, credentials: ShopifyCredentials) -> None:
        super().__init__(SPEC, credentials)


class UnavailableCredentials(_UnavailableCredentials):
    def __init__(self, reason: str = "Shopify Admin API access is not configured.",
                 *, remedy: str = SETUP_REMEDY) -> None:
        super().__init__(SPEC, reason, remedy=remedy)


def default_source(*, credential_file: str | Path | None = None) -> CredentialSource:
    sources: list[CredentialSource] = []
    path = credential_file or os.environ.get("SHOPIFY_CREDENTIAL_FILE")
    if path:
        sources.append(FileCredentials(path))
    sources.append(EnvCredentials())
    sources.append(UnavailableCredentials())
    return ChainedCredentials(*sources)


__all__ = [
    "SPEC", "ShopifyCredentials", "DEFAULT_API_VERSION", "REQUIRED_FIELDS",
    "OPTIONAL_FIELDS", "ENV_KEYS", "SETUP_REMEDY", "REQUIRED_SCOPES",
    "CredentialSource", "CredentialStatus", "CredentialsUnavailable",
    "EnvCredentials", "FileCredentials", "StaticCredentials",
    "UnavailableCredentials", "ChainedCredentials", "default_source",
]
