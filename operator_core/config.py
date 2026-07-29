"""Policy loading and validation.

The policy file is the single source of truth for every threshold in the
system. Engines read it; they never hardcode a limit. Validation is strict and
fails loudly at startup, because a silently-misread threshold is how an
automated system spends money it should not.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.toml"


class PolicyError(ValueError):
    """Raised when the policy file is missing, malformed, or self-contradictory."""


@dataclass(frozen=True)
class Policy:
    """Immutable view over the policy file."""

    raw: dict[str, Any]
    source_path: Path

    # -- section accessors ------------------------------------------------
    @property
    def meta(self) -> dict[str, Any]:
        return self.raw["meta"]

    @property
    def selection(self) -> dict[str, Any]:
        return self.raw["selection"]

    @property
    def prohibited(self) -> dict[str, list[str]]:
        return self.raw["selection"]["prohibited"]

    @property
    def suppliers(self) -> dict[str, Any]:
        return self.raw["suppliers"]

    @property
    def pricing(self) -> dict[str, Any]:
        return self.raw["pricing"]

    @property
    def inventory(self) -> dict[str, Any]:
        return self.raw["inventory"]

    @property
    def advertising(self) -> dict[str, Any]:
        return self.raw["advertising"]

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw["risk"]

    @property
    def reporting(self) -> dict[str, Any]:
        return self.raw["reporting"]

    @property
    def tax(self) -> dict[str, Any]:
        return self.raw["tax"]

    @property
    def capital(self) -> dict[str, Any]:
        return self.raw["capital"]

    @property
    def signals(self) -> dict[str, Any]:
        return self.raw["signals"]

    @property
    def account_health(self) -> dict[str, Any]:
        return self.raw["account_health"]

    @property
    def tiktok(self) -> dict[str, Any]:
        return self.raw["tiktok"]

    @property
    def tiktok_health(self) -> dict[str, Any]:
        return self.raw["tiktok_health"]

    @property
    def live_trading_enabled(self) -> bool:
        return bool(self.meta.get("live_trading_enabled", False))

    def fees_for(self, marketplace: str) -> dict[str, float]:
        """Fee schedule for a marketplace key (amazon/shopify/walmart/ebay/tiktok)."""
        fees = self.raw.get("fees", {})
        key = marketplace.strip().lower()
        if key not in fees:
            raise PolicyError(
                f"No fee schedule for marketplace {marketplace!r}. "
                f"Known: {sorted(fees)}. Add one to [fees.{key}] before pricing anything."
            )
        return fees[key]

    @property
    def marketplaces(self) -> list[str]:
        return sorted(self.raw.get("fees", {}))

    def approval_threshold(self, key: str) -> Any:
        return self.risk["approval_thresholds"].get(key)


REQUIRED_SECTIONS = (
    "meta",
    "selection",
    "suppliers",
    "fees",
    "pricing",
    "inventory",
    "advertising",
    "risk",
    "reporting",
    "tax",
    "capital",
    "signals",
    "account_health",
    "tiktok",
    "tiktok_health",
)


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and validate the policy file."""
    p = Path(path) if path else DEFAULT_POLICY_PATH
    if not p.exists():
        raise PolicyError(f"Policy file not found at {p}. Refusing to run without policy.")

    with p.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - passthrough
            raise PolicyError(f"Policy file {p} is not valid TOML: {exc}") from exc

    _validate(raw, p)
    return Policy(raw=raw, source_path=p)


def _validate(raw: dict[str, Any], path: Path) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s not in raw]
    if missing:
        raise PolicyError(f"Policy {path} is missing required sections: {missing}")

    # Supplier weights must be a true weighting, or the scorecard is meaningless.
    weights = raw["suppliers"].get("weights", {})
    if not weights:
        raise PolicyError("Policy [suppliers.weights] is empty; supplier scoring cannot run.")
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise PolicyError(
            f"Policy [suppliers.weights] must sum to 1.0, got {total:.4f}. "
            "An unnormalised scorecard silently biases supplier choice."
        )

    sel = raw["selection"]
    for key in ("min_roi_pct", "min_margin_pct", "min_supplier_rating", "max_shipping_days"):
        if key not in sel:
            raise PolicyError(f"Policy [selection] missing required gate {key!r}.")

    if sel["min_supplier_rating"] > 5.0:
        raise PolicyError(
            f"min_supplier_rating={sel['min_supplier_rating']} exceeds the 5.0 scale ceiling; "
            "no supplier could ever qualify."
        )

    pr = raw["pricing"]
    if pr["absolute_floor_margin_pct"] >= pr["target_margin_pct"]:
        raise PolicyError(
            "pricing.absolute_floor_margin_pct must sit below target_margin_pct, "
            f"got floor={pr['absolute_floor_margin_pct']} target={pr['target_margin_pct']}."
        )

    adv = raw["advertising"]
    if adv["target_acos_pct"] > adv["max_acos_pct"]:
        raise PolicyError("advertising.target_acos_pct cannot exceed max_acos_pct.")

    risk = raw["risk"]
    if risk["max_single_po_usd"] > risk["max_daily_spend_usd"]:
        raise PolicyError(
            "risk.max_single_po_usd exceeds max_daily_spend_usd; a single PO could "
            "breach the daily cap in one action."
        )
    if "approval_thresholds" not in risk:
        raise PolicyError("Policy [risk.approval_thresholds] is required.")
    if "never_autonomous" not in risk:
        raise PolicyError("Policy risk.never_autonomous list is required.")

    inv = raw["inventory"]
    if inv["critical_days_of_cover"] >= inv["target_days_of_cover"]:
        raise PolicyError("inventory.critical_days_of_cover must be below target_days_of_cover.")

    tax = raw["tax"]
    rate = float(tax["income_tax_rate_pct"])
    if not 0.0 <= rate < 100.0:
        raise PolicyError(
            f"tax.income_tax_rate_pct={rate} is outside 0-100. A rate at or above "
            "100% would make every profitable product look like a loss."
        )

    cap = raw["capital"]
    reserve_total = (
        float(cap["reserve_advertising_pct"])
        + float(cap["reserve_refunds_pct"])
        + float(cap["reserve_contingency_pct"])
    )
    if reserve_total >= 100.0:
        raise PolicyError(
            f"[capital] reserves total {reserve_total:.1f}% of capital, leaving "
            "nothing to deploy. The operator could never buy inventory."
        )
    for key in ("max_single_sku_share_pct", "max_single_supplier_share_pct",
                "max_single_category_share_pct"):
        share = float(cap[key])
        if not 0.0 < share <= 100.0:
            raise PolicyError(f"capital.{key}={share} must be between 0 and 100.")
    if float(cap["min_inventory_turns_per_year"]) > float(cap["target_inventory_turns_per_year"]):
        raise PolicyError(
            "capital.min_inventory_turns_per_year cannot exceed the target."
        )

    sig = raw["signals"]
    weights = sig.get("weights", {})
    if not weights:
        raise PolicyError("[signals.weights] is empty; confidence scoring cannot run.")
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise PolicyError(
            f"[signals.weights] must sum to 1.0, got {total:.4f}. An unnormalised "
            "weighting silently biases which sources decide an opportunity."
        )
    if int(sig["min_positive_signals"]) < 1:
        raise PolicyError(
            "signals.min_positive_signals must be at least 1 — the charter requires "
            "corroboration before capital is committed."
        )

    tt = raw["tiktok"]
    if float(tt["max_autonomous_price_change_pct"]) > float(
            raw["pricing"]["max_daily_price_change_pct"]):
        raise PolicyError(
            "tiktok.max_autonomous_price_change_pct exceeds the global "
            "pricing.max_daily_price_change_pct, which would let a TikTok write "
            "bypass the portfolio-wide limit."
        )
    if int(tt["min_trend_history_days"]) < 7:
        raise PolicyError(
            f"tiktok.min_trend_history_days={tt['min_trend_history_days']} is too "
            "short to tell growth from a decaying spike, and those call for "
            "opposite inventory decisions."
        )

    tth = raw["tiktok_health"]
    if float(tth["warn_violation_points"]) >= float(tth["max_seller_violation_points"]):
        raise PolicyError(
            "tiktok_health.warn_violation_points must sit below the suspension "
            "threshold, or the warning fires at the same moment as the suspension."
        )

    health = raw["account_health"]
    warn_at = float(health.get("warn_at_pct_of_limit", 75.0))
    if not 0.0 < warn_at <= 100.0:
        raise PolicyError(
            f"account_health.warn_at_pct_of_limit={warn_at} must be in (0, 100]. "
            "Warning only at 100% of a limit gives no time to react."
        )
