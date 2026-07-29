"""Market signals and opportunity confidence scoring.

The charter asks for continuous monitoring of Amazon, Google Trends, TikTok,
Reddit, Pinterest, YouTube, Meta, news, and economic indicators. This module
defines the contract for all of them and scores whatever is genuinely available.

The design constraint that shapes everything here: **an unconnected source
contributes nothing and says so.** It does not contribute a neutral 50, and it
is never filled in from a model's impression of what is trending. A fabricated
demand signal is the most expensive kind of fabrication in this system, because
unlike a bad report it survives into a purchase order and becomes inventory.

So `confidence_score` returns both a score and the share of signal weight that
was actually observable. A 90 built from 30% coverage is not a 90, and the
report says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from .config import Policy


class SignalDirection(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class SourceState(str, Enum):
    LIVE = "LIVE"                 # connector exists and returned data
    UNAVAILABLE = "UNAVAILABLE"   # no connector wired
    ERROR = "ERROR"               # connector exists but failed
    STALE = "STALE"               # data older than the policy window


# Which sources have a real connector today. Flip a value here only when the
# connector actually exists and has returned live data at least once.
SOURCE_CONNECTORS = {
    # Live: our own TikTok shop analytics tell us how OUR products are moving.
    "tiktok_product_velocity": "connectors.tiktok (Analytics 202405)",
    "amazon_sales_rank": "connectors.amazon (SP-API Catalog Items)",
    # Everything below needs a feed that does not exist yet.
    "tiktok_hashtag_momentum": None,
    "tiktok_sound_trend": None,
    "tiktok_creator_adoption": None,
    "amazon_search_volume": None,
    "google_trends": None,
    "social_reddit": None,
    "social_pinterest": None,
    "social_youtube": None,
    "news_regulatory": None,
    "seasonality": None,
}

# What it would take to wire each one, so the gap is actionable rather than
# just reported.
SOURCE_REQUIREMENTS = {
    "tiktok_hashtag_momentum": (
        "TikTok Creative Center has no public API. Options: the TikTok Research "
        "API (academic/approved use only), a commercial social-listening "
        "provider with a TikTok data agreement, or manual weekly logging "
        "through the signals interface. Scraping the Creative Center breaches "
        "TikTok's ToS and risks the shop."
    ),
    "tiktok_sound_trend": (
        "Same as hashtag momentum — no public API. Trending sounds are visible "
        "in-app and can be logged manually; they cannot be pulled."
    ),
    "tiktok_creator_adoption": (
        "TikTok Shop's Creator Marketplace API requires separate approval and "
        "covers creators you have a relationship with, not the open market."
    ),
    "amazon_search_volume": "Amazon Brand Analytics (requires Brand Registry).",
    "google_trends": "Google Trends has no official API; a licensed data "
                     "provider or an approved scraping agreement is needed.",
    "social_tiktok": "TikTok Research API (application required) or a "
                     "commercial social-listening provider.",
    "social_reddit": "Reddit API (OAuth app, rate-limited, paid above free tier).",
    "social_pinterest": "Pinterest Trends API (business account + app review).",
    "social_youtube": "YouTube Data API v3 (quota-limited, free tier available).",
    "news_regulatory": "A news/regulatory feed — CPSC recall RSS is free and "
                       "is the highest-value one for product safety.",
    "seasonality": "Twelve months of the operator's own sales history; until "
                   "then there is nothing to derive a seasonal index from.",
}


@dataclass
class Signal:
    """One observation about demand, competition, or risk.

    `strength` is 0-100 and means "how strongly does this point in `direction`",
    not "how much do we like this product".
    """

    source: str
    direction: SignalDirection
    strength: float
    observed_at: str
    detail: str
    raw: dict[str, Any] = field(default_factory=dict)

    def age_days(self, *, today: date | None = None) -> int:
        try:
            seen = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            return 10**6  # unparseable timestamp is treated as ancient, never fresh
        reference = today or date.today()
        return reference.toordinal() - seen.toordinal()

    def is_fresh(self, max_age_days: int, *, today: date | None = None) -> bool:
        return 0 <= self.age_days(today=today) <= max_age_days


@dataclass
class SourceStatus:
    source: str
    state: SourceState
    weight: float
    detail: str = ""

    @property
    def counts(self) -> bool:
        return self.state is SourceState.LIVE


@dataclass
class ConfidenceAssessment:
    """The result of scoring an opportunity's signals."""

    sku: str
    score: float                       # 0-100, over observed sources only
    coverage_pct: float                # share of total signal weight observed
    positive_signals: int
    negative_signals: int
    sources: list[SourceStatus]
    signals: list[Signal]
    meets_signal_minimum: bool
    meets_confidence_minimum: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def sufficient(self) -> bool:
        """Whether this opportunity may be recommended at all."""
        return self.meets_signal_minimum and self.meets_confidence_minimum

    @property
    def effective_score(self) -> float:
        """Score discounted by how much of the signal set we could actually see.

        This is the number to rank on. An unmoderated score rewards products we
        happen to know little about, which is exactly backwards.
        """
        return round(self.score * (self.coverage_pct / 100.0), 1)

    def summary(self) -> str:
        return (
            f"confidence {self.score:.0f}/100 at {self.coverage_pct:.0f}% source "
            f"coverage (effective {self.effective_score:.0f}); "
            f"{self.positive_signals} positive / {self.negative_signals} negative"
        )


def source_statuses(policy: Policy) -> list[SourceStatus]:
    """Report every declared source and whether it can actually be read."""
    weights = policy.raw["signals"]["weights"]
    out: list[SourceStatus] = []
    for source, weight in weights.items():
        connector = SOURCE_CONNECTORS.get(source)
        if connector:
            out.append(SourceStatus(source, SourceState.LIVE, float(weight), connector))
        else:
            out.append(SourceStatus(
                source, SourceState.UNAVAILABLE, float(weight),
                SOURCE_REQUIREMENTS.get(source, "No connector implemented."),
            ))
    out.sort(key=lambda s: -s.weight)
    return out


def available_weight(policy: Policy) -> float:
    """Fraction of total signal weight that is currently observable."""
    statuses = source_statuses(policy)
    total = sum(s.weight for s in statuses) or 1.0
    live = sum(s.weight for s in statuses if s.counts)
    return round(live / total * 100, 1)


def assess_confidence(
    policy: Policy,
    sku: str,
    signals: list[Signal],
    *,
    today: date | None = None,
) -> ConfidenceAssessment:
    """Score an opportunity from the signals actually collected for it.

    Scoring is weighted by source importance and computed *only over sources
    that reported*. Missing sources reduce coverage rather than dragging the
    score toward the middle, because "we did not look" and "we looked and it
    was mediocre" are different facts and must not be blended.
    """
    cfg = policy.raw["signals"]
    weights = cfg["weights"]
    max_age = int(cfg["max_signal_age_days"])
    min_signals = int(cfg["min_positive_signals"])
    min_confidence = float(cfg["min_confidence_score"])

    warnings: list[str] = []
    statuses = source_statuses(policy)
    by_source = {s.source: s for s in statuses}

    fresh: list[Signal] = []
    for sig in signals:
        if sig.source not in weights:
            warnings.append(
                f"Signal from unknown source {sig.source!r} ignored — it carries no "
                "policy weight, so counting it would be an unauditable thumb on "
                "the scale."
            )
            continue
        if not sig.is_fresh(max_age, today=today):
            warnings.append(
                f"{sig.source} signal is {sig.age_days(today=today)}d old "
                f"(limit {max_age}d) and was dropped. Trend data decays fast; "
                "a stale spike is not current demand."
            )
            if sig.source in by_source:
                by_source[sig.source].state = SourceState.STALE
            continue
        fresh.append(sig)

    # Score across sources that actually reported something fresh.
    reported = {s.source for s in fresh}
    scored_weight = sum(float(weights[src]) for src in reported) or 0.0

    if scored_weight <= 0:
        total_weight = sum(float(w) for w in weights.values()) or 1.0
        return ConfidenceAssessment(
            sku=sku, score=0.0, coverage_pct=0.0, positive_signals=0,
            negative_signals=0, sources=statuses, signals=[],
            meets_signal_minimum=False, meets_confidence_minimum=False,
            warnings=warnings + [
                "No fresh signals from any weighted source. Confidence is 0 — not "
                "because the product is bad, but because nothing is known about it. "
                "Do not treat an unknown as a negative or as a neutral."
            ],
        )

    weighted_total = 0.0
    for sig in fresh:
        w = float(weights[sig.source])
        if sig.direction is SignalDirection.POSITIVE:
            contribution = sig.strength
        elif sig.direction is SignalDirection.NEGATIVE:
            contribution = -sig.strength
        else:
            contribution = 0.0
        weighted_total += w * contribution

    # Map [-100, +100] onto [0, 100]: 50 is genuinely neutral evidence.
    raw = weighted_total / scored_weight
    score = round(max(0.0, min(100.0, 50.0 + raw / 2.0)), 1)

    total_weight = sum(float(w) for w in weights.values()) or 1.0
    coverage = round(scored_weight / total_weight * 100, 1)

    positives = sum(1 for s in fresh if s.direction is SignalDirection.POSITIVE)
    negatives = sum(1 for s in fresh if s.direction is SignalDirection.NEGATIVE)

    meets_signals = positives >= min_signals
    meets_confidence = score >= min_confidence

    if not meets_signals:
        warnings.append(
            f"{positives} positive signal(s) against a {min_signals} minimum. "
            "One or two signals is an anecdote — the charter requires "
            "corroboration before capital is committed."
        )
    if coverage < 50:
        warnings.append(
            f"Only {coverage:.0f}% of the weighted signal set is observable. "
            "Rank on effective score, not raw score, and treat every ranking as "
            "provisional until more sources are connected."
        )
    if negatives:
        warnings.append(
            f"{negatives} negative signal(s) present. Read them before acting — "
            "a high score with a live negative usually means one source is "
            "outvoting a warning."
        )

    return ConfidenceAssessment(
        sku=sku, score=score, coverage_pct=coverage,
        positive_signals=positives, negative_signals=negatives,
        sources=statuses, signals=fresh,
        meets_signal_minimum=meets_signals,
        meets_confidence_minimum=meets_confidence,
        warnings=warnings,
    )


def signal_from_sales_rank(rank: int, *, category_size: int = 1_000_000,
                           observed_at: str | None = None) -> Signal:
    """Convert an Amazon sales rank into a demand signal.

    Rank is ordinal, not cardinal — rank 1000 is not "ten times" rank 10000 —
    so this maps onto a log scale. It is a demand *proxy*: it says other people
    are buying in this category, not how many will buy from us.
    """
    import math

    observed_at = observed_at or date.today().isoformat()
    if rank <= 0:
        return Signal(
            source="amazon_sales_rank", direction=SignalDirection.NEUTRAL,
            strength=0.0, observed_at=observed_at,
            detail="No sales rank available — the ASIN may be new or unranked.",
        )

    # Rank 100 -> strong, rank 100k -> weak, log-scaled between.
    span = math.log10(max(category_size, 10))
    position = math.log10(max(rank, 1))
    strength = max(0.0, min(100.0, (1 - position / span) * 100))
    direction = SignalDirection.POSITIVE if strength >= 50 else SignalDirection.NEGATIVE

    return Signal(
        source="amazon_sales_rank",
        direction=direction,
        strength=round(strength, 1),
        observed_at=observed_at,
        detail=(
            f"Sales rank {rank:,} in a category of ~{category_size:,}. "
            "Rank is a demand proxy, not a unit forecast."
        ),
        raw={"rank": rank, "category_size": category_size},
    )


def signal_from_tiktok_velocity(
    *,
    recent_daily_units: float,
    prior_daily_units: float,
    trend_shape: str,
    conversion_rate_pct: float = 0.0,
    observed_at: str | None = None,
) -> Signal:
    """Turn our own TikTok product performance into a demand signal.

    This is the one TikTok source that is genuinely live, because it describes
    our own shop rather than the market. It is a real signal — units moving is
    the least ambiguous evidence there is — but it is *our* demand, not
    category demand, and it cannot tell us whether an unlisted product would
    sell. The detail line says so.

    Spike-decay is scored NEGATIVE even when recent volume is high: the curve
    has rolled over, and treating the window average as demand is precisely
    how a warehouse fills up.
    """
    observed_at = observed_at or date.today().isoformat()

    if trend_shape in ("INSUFFICIENT_DATA", ""):
        return Signal(
            source="tiktok_product_velocity", direction=SignalDirection.NEUTRAL,
            strength=0.0, observed_at=observed_at,
            detail="Not enough daily history to establish a velocity trend.",
        )

    if trend_shape == "SPIKE_DECAY":
        return Signal(
            source="tiktok_product_velocity", direction=SignalDirection.NEGATIVE,
            strength=70.0, observed_at=observed_at,
            detail=(
                f"Spike already decayed to {recent_daily_units:.1f} u/day. High "
                "window volume, but the curve has rolled over — this is evidence "
                "against reordering, not for it."
            ),
            raw={"recent": recent_daily_units, "prior": prior_daily_units,
                 "shape": trend_shape},
        )

    if trend_shape == "DEAD":
        return Signal(
            source="tiktok_product_velocity", direction=SignalDirection.NEGATIVE,
            strength=90.0, observed_at=observed_at,
            detail="No units moving at all in the window.",
            raw={"shape": trend_shape},
        )

    change = ((recent_daily_units - prior_daily_units) / prior_daily_units * 100
              if prior_daily_units else 0.0)

    if trend_shape == "GROWING":
        strength = max(0.0, min(100.0, 55 + change / 4))
        direction = SignalDirection.POSITIVE
    elif trend_shape == "DECAYING":
        strength = max(0.0, min(100.0, 40 + abs(change) / 4))
        direction = SignalDirection.NEGATIVE
    else:  # STEADY
        strength = 45.0
        direction = SignalDirection.POSITIVE if recent_daily_units > 0 \
            else SignalDirection.NEUTRAL

    return Signal(
        source="tiktok_product_velocity", direction=direction,
        strength=round(strength, 1), observed_at=observed_at,
        detail=(
            f"{trend_shape}: {prior_daily_units:.1f} -> {recent_daily_units:.1f} "
            f"u/day ({change:+.0f}%), {conversion_rate_pct:.2f}% conversion. "
            "This is our own shop's demand, not category demand."
        ),
        raw={"recent": recent_daily_units, "prior": prior_daily_units,
             "shape": trend_shape, "conversion_pct": conversion_rate_pct},
    )


# Sources a human can legitimately observe in-app and log by hand. These have
# no API, and manual entry is the only route that does not breach TikTok's ToS.
MANUALLY_OBSERVABLE = frozenset({
    "tiktok_hashtag_momentum",
    "tiktok_sound_trend",
    "tiktok_creator_adoption",
    "google_trends",
    "social_reddit",
    "social_pinterest",
    "social_youtube",
    "news_regulatory",
})


def validate_manual_signal(policy: Policy, source: str, strength: float) -> None:
    """Reject manual entries that would corrupt the confidence model.

    A human logging what they saw in the app is legitimate evidence. A human
    logging a source that has a real API, or inventing a strength for one they
    did not check, is not — and the whole value of the confidence score is that
    it distinguishes the two.
    """
    weights = policy.raw["signals"]["weights"]
    if source not in weights:
        raise ValueError(
            f"Unknown signal source {source!r}. Known: {', '.join(sorted(weights))}."
        )
    if source not in MANUALLY_OBSERVABLE:
        connector = SOURCE_CONNECTORS.get(source)
        raise ValueError(
            f"{source!r} is served by {connector or 'a connector'} and must not be "
            "entered by hand. Manual entry for an automatable source means the "
            "number is a recollection rather than a reading, and nothing "
            "downstream can tell the difference."
        )
    if not 0.0 <= strength <= 100.0:
        raise ValueError(f"Strength must be 0-100, got {strength}.")


def unavailable_sources_report(policy: Policy) -> list[str]:
    """Human-readable list of what is missing and how to get it."""
    lines: list[str] = []
    for s in source_statuses(policy):
        if s.counts:
            continue
        lines.append(f"{s.source} (weight {s.weight:.0%}) — {s.detail}")
    return lines
