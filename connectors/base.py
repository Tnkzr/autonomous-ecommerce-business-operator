"""Marketplace connector interface.

Every connector obeys three rules:

1. **Fail loud on missing credentials.** Never return empty lists or zeros that
   look like real data. A silent zero propagates into a report that says
   "revenue $0" when the truth is "we could not authenticate", and those two
   statements lead to completely different decisions.

2. **Reads are free, writes are gated.** Any mutating call checks
   `live_trading_enabled` and the risk module before touching an account.

3. **Every response is tagged with its provenance.** `DataEnvelope.source` is
   either `live`, `cached`, or `seed`. The reporting layer refuses to present
   seed data as if it were real.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

DataSource = Literal["live", "cached", "seed"]


class ConnectorNotConfigured(RuntimeError):
    """Raised when credentials for a marketplace are absent or incomplete."""


class WriteNotPermitted(RuntimeError):
    """Raised when a mutating call is attempted outside live mode / approval."""


class MarketplaceNotImplemented(NotImplementedError):
    """This marketplace has no live implementation yet.

    Distinct from a missing credential (`ConnectorNotConfigured`) and from an
    unfinished function. It says the adapter for this marketplace has not been
    written, names what would be needed, and subclasses NotImplementedError so
    existing handlers keep working.
    """

    def __init__(self, marketplace: str, operation: str, *,
                 endpoints: str = "", docs: str = "") -> None:
        message = (
            f"{marketplace}.{operation} has no live implementation. The connector "
            f"declares its credentials and fails loudly rather than returning "
            f"fabricated data."
        )
        if endpoints:
            message += f" Endpoints to implement: {endpoints}."
        if docs:
            message += f" Docs: {docs}"
        super().__init__(message)
        self.marketplace = marketplace
        self.operation = operation


@dataclass
class DataEnvelope:
    """Data plus provenance. Never hand raw data around without this."""

    source: DataSource
    marketplace: str
    fetched_at: str
    payload: Any
    warnings: list[str] = field(default_factory=list)

    @property
    def is_real(self) -> bool:
        return self.source == "live"


class MarketplaceConnector(ABC):
    """Base connector. Subclasses declare the env vars they need."""

    name: str = "base"
    required_env: tuple[str, ...] = ()
    docs_url: str = ""

    def __init__(self, *, allow_writes: bool = False) -> None:
        self.allow_writes = allow_writes

    # -- credential handling ----------------------------------------------
    def missing_credentials(self) -> list[str]:
        return [k for k in self.required_env if not os.environ.get(k)]

    @property
    def configured(self) -> bool:
        return not self.missing_credentials()

    def require_credentials(self) -> None:
        missing = self.missing_credentials()
        if missing:
            raise ConnectorNotConfigured(
                f"{self.name} connector is not configured. Missing environment "
                f"variable(s): {', '.join(missing)}. "
                + (f"Setup: {self.docs_url}" if self.docs_url else "")
                + " Refusing to return placeholder data — a fabricated zero is worse "
                "than a clear failure."
            )

    def require_write_permission(self, action: str) -> None:
        if not self.allow_writes:
            raise WriteNotPermitted(
                f"Refusing to '{action}' on {self.name}: connector is in read-only mode. "
                "Writes require meta.live_trading_enabled=true in policy.toml, valid "
                "credentials, and an approved action record."
            )
        self.require_credentials()

    # -- read interface (implement per marketplace) ------------------------
    @abstractmethod
    def fetch_orders(self, *, since: str) -> DataEnvelope: ...

    @abstractmethod
    def fetch_inventory(self) -> DataEnvelope: ...

    @abstractmethod
    def fetch_listings(self) -> DataEnvelope: ...

    @abstractmethod
    def fetch_competitor_offers(self, identifier: str) -> DataEnvelope: ...

    @abstractmethod
    def fetch_reviews(self, sku: str) -> DataEnvelope: ...

    @abstractmethod
    def fetch_ad_performance(self, *, since: str) -> DataEnvelope: ...

    # -- write interface ---------------------------------------------------
    def update_price(self, sku: str, price: float) -> DataEnvelope:
        self.require_write_permission("update_price")
        raise MarketplaceNotImplemented(self.name, "update_price", docs=self.docs_url)

    def publish_listing(self, listing: dict[str, Any]) -> DataEnvelope:
        self.require_write_permission("publish_listing")
        raise MarketplaceNotImplemented(self.name, "publish_listing", docs=self.docs_url)

    def update_ad_budget(self, campaign_id: str, budget: float) -> DataEnvelope:
        self.require_write_permission("update_ad_budget")
        raise MarketplaceNotImplemented(self.name, "update_ad_budget", docs=self.docs_url)

    def verify_connection(self) -> dict[str, Any]:
        """Prove credentials actually work against the live API.

        Returns a structured result instead of raising, so a status sweep can
        diagnose every marketplace rather than dying on the first failure.
        Connectors without a live implementation report why.
        """
        missing = self.missing_credentials()
        return {
            "marketplace": self.name,
            "ok": False,
            "detail": (
                f"Missing credentials: {', '.join(missing)}" if missing
                else "Credentials present, but this connector has no live "
                     "implementation yet — nothing to verify against."
            ),
        }

    def status(self) -> dict[str, Any]:
        return {
            "marketplace": self.name,
            "configured": self.configured,
            "missing_env": self.missing_credentials(),
            "writes_allowed": self.allow_writes,
            "docs": self.docs_url,
        }
