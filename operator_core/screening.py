"""Product screening: every gate from the operating policy, as executable code.

Design rule: gates are *conjunctive and blocking by default*. A candidate that
trips any blocking gate cannot be approved by any other part of the system —
there is no override path in code, only a human editing policy.toml.

The IP/hazmat screens are keyword heuristics. They are deliberately tuned to
over-flag: a false positive costs one human review, a false negative costs a
suspended selling account. That trade is not close.
"""

from __future__ import annotations

import re

from .config import Policy
from .economics import economics_for_candidate
from .models import (
    Decision,
    GateResult,
    ProductCandidate,
    ScreeningResult,
    Severity,
    UnitEconomics,
)


def _matches(text: str, terms: list[str]) -> list[str]:
    """Whole-word-ish matching so 'cure' does not fire inside 'manicure'."""
    hits = []
    for term in terms:
        t = term.strip().lower()
        if not t:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            hits.append(term)
    return hits


def competition_score(candidate: ProductCandidate) -> float:
    """0-100 crowding estimate. Higher is worse.

    Blends three signals that each independently predict a hard launch:
      - how many sellers are already there
      - how much review moat the leader has (the real barrier)
      - how good the incumbents' ratings are (a weak leader is an opening)
    """
    rivals = candidate.competitor_count
    seller_component = min(rivals / 50.0, 1.0) * 40.0

    reviews = candidate.top_rival_review_count
    review_component = min(reviews / 5000.0, 1.0) * 45.0

    # A 4.8-rated incumbent is far harder to displace than a 3.9-rated one.
    rating = candidate.avg_rival_rating or 4.0
    rating_component = max(0.0, min((rating - 3.5) / 1.5, 1.0)) * 15.0

    return round(min(seller_component + review_component + rating_component, 100.0), 1)


def _compliance_gates(policy: Policy, candidate: ProductCandidate) -> list[GateResult]:
    text = candidate.searchable_text()
    prohibited = policy.prohibited
    gates: list[GateResult] = []

    checks = [
        ("hazmat", "hazmat_terms",
         "Hazmat/restricted-goods term(s) present: {hits}. Requires dangerous-goods "
         "review and marketplace approval before any listing."),
        ("medical_claims", "medical_claim_terms",
         "Medical/health claim term(s) present: {hits}. Unsubstantiated claims draw "
         "FTC and marketplace enforcement."),
        ("trademark", "trademark_terms",
         "Possible trademark reference(s): {hits}. Requires documented brand "
         "authorisation or removal before listing."),
        ("copyright", "copyright_terms",
         "Possible copyrighted-content reference(s): {hits}. Requires licence proof."),
        ("patent_risk", "patent_risk_terms",
         "Product sits in a litigated utility-patent niche: {hits}. Requires "
         "freedom-to-operate check."),
    ]

    for name, policy_key, template in checks:
        hits = _matches(text, prohibited.get(policy_key, []))
        gates.append(
            GateResult(
                name=name,
                passed=not hits,
                detail=template.format(hits=", ".join(hits)) if hits else "No matches.",
                severity=Severity.CRITICAL if hits else Severity.INFO,
                blocking=True,
            )
        )

    # Gated categories are not a permanent block — they are a block *until
    # approval is on file*, which is exactly what the policy says.
    gated = policy.selection.get("gated_categories", [])
    is_gated = candidate.is_gated_category or any(
        g.lower() in candidate.category.lower() for g in gated
    )
    if is_gated and not candidate.gated_approval_on_file:
        gates.append(
            GateResult(
                name="restricted_category",
                passed=False,
                detail=(
                    f"Category {candidate.category!r} is gated and no approval is on "
                    "file. Selling ungated risks listing removal and account review."
                ),
                severity=Severity.CRITICAL,
                blocking=True,
            )
        )
    else:
        gates.append(
            GateResult(
                name="restricted_category",
                passed=True,
                detail="Category ungated or approval on file.",
            )
        )

    return gates


def _supplier_gates(policy: Policy, candidate: ProductCandidate) -> list[GateResult]:
    sel = policy.selection
    s = candidate.supplier
    gates: list[GateResult] = []

    min_rating = float(sel["min_supplier_rating"])
    gates.append(
        GateResult(
            name="supplier_rating",
            passed=s.rating >= min_rating,
            detail=f"Supplier rating {s.rating:.2f} vs required {min_rating:.2f}.",
            severity=Severity.WARN if s.rating < min_rating else Severity.INFO,
        )
    )

    max_days = int(sel["max_shipping_days"])
    ship_ok = s.domestic_stock or s.shipping_days < max_days
    gates.append(
        GateResult(
            name="shipping_time",
            passed=ship_ok,
            detail=(
                f"{s.shipping_days}d transit"
                + (" (domestic stock, limit waived)" if s.domestic_stock else f" vs limit {max_days}d")
            ),
        )
    )
    return gates


def _market_gates(policy: Policy, candidate: ProductCandidate) -> list[GateResult]:
    sel = policy.selection
    gates: list[GateResult] = []

    comp = competition_score(candidate)
    max_comp = float(sel.get("max_competition_score", 70.0))
    gates.append(
        GateResult(
            name="competition",
            passed=comp <= max_comp,
            detail=(
                f"Competition score {comp} vs max {max_comp} "
                f"({candidate.competitor_count} sellers, top rival "
                f"{candidate.top_rival_review_count} reviews)."
            ),
        )
    )

    min_demand = int(sel.get("min_monthly_demand_units", 0))
    gates.append(
        GateResult(
            name="demand",
            passed=candidate.est_monthly_demand_units >= min_demand,
            detail=(
                f"Est. {candidate.est_monthly_demand_units} units/mo vs "
                f"minimum {min_demand}."
            ),
        )
    )

    max_reviews = int(sel.get("max_review_count_of_top_rival", 10**9))
    gates.append(
        GateResult(
            name="review_moat",
            passed=candidate.top_rival_review_count <= max_reviews,
            detail=(
                f"Top rival has {candidate.top_rival_review_count} reviews vs "
                f"tolerance {max_reviews}."
            ),
        )
    )

    lo = float(sel.get("min_price", 0.0))
    hi = float(sel.get("max_price", 10**9))
    gates.append(
        GateResult(
            name="price_band",
            passed=lo <= candidate.target_price <= hi,
            detail=f"Target price ${candidate.target_price:.2f} vs band ${lo:.2f}-${hi:.2f}.",
        )
    )
    return gates


def _economic_gates(policy: Policy, econ: UnitEconomics) -> list[GateResult]:
    sel = policy.selection
    min_roi = float(sel["min_roi_pct"])
    min_margin = float(sel["min_margin_pct"])

    return [
        GateResult(
            name="roi",
            passed=econ.roi_pct >= min_roi,
            detail=f"ROI {econ.roi_pct:.1f}% vs required {min_roi:.1f}%.",
        ),
        GateResult(
            name="margin",
            passed=econ.margin_pct >= min_margin,
            detail=f"Net margin {econ.margin_pct:.1f}% vs required {min_margin:.1f}%.",
        ),
        GateResult(
            name="positive_profit",
            passed=econ.net_profit > 0,
            detail=f"Net profit per unit ${econ.net_profit:.2f}.",
            severity=Severity.CRITICAL if econ.net_profit <= 0 else Severity.INFO,
        ),
    ]


def opportunity_score(candidate: ProductCandidate, econ: UnitEconomics) -> float:
    """0-100 ranking score for candidates that already passed the gates.

    Gates answer "may we?"; this answers "which first?". Weighted toward
    monthly profit pool and away from crowding, because a 60%-margin product
    selling 5 units a month is not a business.
    """
    monthly_profit = econ.net_profit * candidate.est_monthly_demand_units
    profit_pool = min(monthly_profit / 5000.0, 1.0) * 35.0
    margin = min(econ.margin_pct / 50.0, 1.0) * 25.0
    roi = min(econ.roi_pct / 100.0, 1.0) * 20.0
    headroom = (1.0 - competition_score(candidate) / 100.0) * 20.0
    return round(profit_pool + margin + roi + headroom, 1)


def screen_candidate(policy: Policy, candidate: ProductCandidate,
                     *, sale_price: float | None = None) -> ScreeningResult:
    """Run every gate. Returns a full audit trail, not just a verdict."""
    econ = economics_for_candidate(policy, candidate, sale_price=sale_price)

    gates: list[GateResult] = []
    gates += _compliance_gates(policy, candidate)
    gates += _supplier_gates(policy, candidate)
    gates += _market_gates(policy, candidate)
    gates += _economic_gates(policy, econ)

    blocking = [g for g in gates if not g.passed and g.blocking]
    compliance_names = {
        "hazmat", "medical_claims", "trademark", "copyright",
        "patent_risk", "restricted_category",
    }
    compliance_failed = [g for g in blocking if g.name in compliance_names]

    notes: list[str] = []
    if compliance_failed:
        # Compliance failures are never "close calls" to be scored around.
        decision = Decision.REJECT
        notes.append(
            "Rejected on compliance grounds. Account safety outranks any projected "
            "profit; this is not overridable by economics."
        )
    elif blocking:
        decision = Decision.REJECT
        notes.append("Failed one or more economic/market gates.")
    else:
        decision = Decision.NEEDS_HUMAN_APPROVAL
        notes.append(
            "All gates passed. Sourcing a new SKU commits capital, so it routes to "
            "human approval per risk.approval_thresholds.listing_publish."
        )

    score = opportunity_score(candidate, econ) if decision != Decision.REJECT else 0.0

    return ScreeningResult(
        sku=candidate.sku,
        decision=decision,
        gates=gates,
        economics=econ,
        score=score,
        notes=notes,
    )


def screen_all(policy: Policy, candidates: list[ProductCandidate]) -> list[ScreeningResult]:
    """Screen and rank. Approved candidates sort first, by opportunity score."""
    results = [screen_candidate(policy, c) for c in candidates]
    results.sort(key=lambda r: (r.decision == Decision.REJECT, -r.score))
    return results
