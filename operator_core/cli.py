"""Command line interface.

    python -m operator_core.cli status
    python -m operator_core.cli daily
    python -m operator_core.cli screen
    python -m operator_core.cli listing SEED-BAMBOO-ORG-01
    python -m operator_core.cli suppliers SEED-BAMBOO-ORG-01
    python -m operator_core.cli price SEED-BAMBOO-ORG-01
    python -m operator_core.cli capital
    python -m operator_core.cli approvals
    python -m operator_core.cli approve <action_id> --by "Name"
    python -m operator_core.cli outcome <action_id> --met true --note "..."

Amazon SP-API (requires live credentials — see `status`):

    python -m operator_core.cli amazon-verify
    python -m operator_core.cli amazon-search "bamboo drawer organizer"
    python -m operator_core.cli amazon-fees B08EXAMPLE1 34.99
    python -m operator_core.cli amazon-inventory
    python -m operator_core.cli amazon-orders --since 2026-07-01
    python -m operator_core.cli amazon-offers B08EXAMPLE1

TikTok Shop (requires live credentials — see `status`):

    python -m operator_core.cli tiktok-verify
    python -m operator_core.cli tiktok-products
    python -m operator_core.cli tiktok-orders --since 2026-07-01
    python -m operator_core.cli tiktok-settlements --days 30
    python -m operator_core.cli tiktok-trends
    python -m operator_core.cli tiktok-report
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors import (  # noqa: E402
    ConnectorNotConfigured,
    WriteNotPermitted,
    all_status,
    get_connector,
)
from connectors.amazon import SPAPIError  # noqa: E402
from connectors.tiktok import TikTokAPIError  # noqa: E402

from .config import PolicyError, load_policy  # noqa: E402
from .economics import economics_for_candidate  # noqa: E402
from .listings import generate_listing  # noqa: E402
from .models import CompetitorOffer  # noqa: E402
from .pipeline import load_candidates, load_operations, run_daily  # noqa: E402
from .pricing import recommend_price  # noqa: E402
from .screening import screen_all, screen_candidate  # noqa: E402
from .store import Store, learning_summary  # noqa: E402
from .suppliers import build_negotiation_brief, score_suppliers, select_supplier  # noqa: E402
from .pipeline import _supplier_from  # noqa: E402


def _hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def cmd_status(args, policy, store) -> int:
    _hr("OPERATOR STATUS")
    print(f"Policy:        {policy.source_path}")
    print(f"Business:      {policy.meta.get('business_name')}")
    print(f"Mode:          {'LIVE EXECUTION' if policy.live_trading_enabled else 'ADVISORY ONLY (no writes)'}")
    print(f"Marketplaces:  {', '.join(policy.marketplaces)}")

    _hr("MARKETPLACE CONNECTIONS")
    verified_any = False
    configured_any = False

    for entry in all_status():
        name = entry["marketplace"]
        configured = entry["configured"]
        configured_any = configured_any or configured

        if not configured:
            print(f"  {name:<10} NOT CONFIGURED")
            print(f"             missing: {', '.join(entry['missing_env'])}")
            print(f"             docs:    {entry['docs']}")
            continue

        if args.no_verify:
            print(f"  {name:<10} CREDENTIALS PRESENT (not verified)")
            continue

        # Credentials exist, so prove they work rather than assuming.
        conn = get_connector(name)
        try:
            result = conn.verify_connection()
        except Exception as exc:  # a broken connector must not kill the sweep
            print(f"  {name:<10} ERROR — {type(exc).__name__}: {exc}")
            continue

        if result.get("ok"):
            verified_any = True
            print(f"  {name:<10} CONNECTED — {result.get('detail', '')}")
            if result.get("seller_marketplaces"):
                print(f"             seller marketplaces: "
                      f"{', '.join(result['seller_marketplaces'])}")
            if result.get("endpoint"):
                print(f"             endpoint: {result['endpoint']}")
        else:
            print(f"  {name:<10} FAILED VERIFICATION")
            for line in str(result.get("detail", "")).splitlines():
                print(f"             {line}")
            if result.get("status_code"):
                print(f"             HTTP {result['status_code']}")

    if not configured_any:
        print(
            "\n  No marketplace is connected. The operator runs in advisory mode on\n"
            "  seed data only. It cannot read your real sales, inventory, or ads, and\n"
            "  it will not present simulated numbers as if they were real."
        )
    elif not verified_any and not args.no_verify:
        print(
            "\n  Credentials are present but none verified. Until a connection\n"
            "  succeeds the operator stays on seed data — it will not guess at\n"
            "  numbers it could not read."
        )

    _hr("GATES IN FORCE")
    sel = policy.selection
    print(f"  Min ROI:              {sel['min_roi_pct']}%")
    print(f"  Min margin:           {sel['min_margin_pct']}%")
    print(f"  Min supplier rating:  {sel['min_supplier_rating']}")
    print(f"  Max shipping:         {sel['max_shipping_days']} days (waived for domestic)")
    print(f"  Max single PO:        ${policy.risk['max_single_po_usd']:,.2f}")
    print(f"  Max daily spend:      ${policy.risk['max_daily_spend_usd']:,.2f}")
    print(f"  Approval above:       ${policy.approval_threshold('purchase_order_usd'):,.2f}")

    ls = learning_summary(store)
    _hr("DECISION JOURNAL")
    print(f"  Recorded: {ls['total_decisions']}  |  resolved: {ls['resolved_decisions']}"
          f"  |  open: {ls['unresolved_decisions']}")
    pending = store.pending_approvals()
    if pending:
        print(f"  {len(pending)} action(s) awaiting approval — run `approvals` to review.")
    return 0


def cmd_daily(args, policy, store) -> int:
    content, path = run_daily(policy, store, report_date=args.date)
    print(content)
    print(f"\n[report written to {path}]")
    return 0


def cmd_screen(args, policy, store) -> int:
    results = screen_all(policy, load_candidates())
    _hr("PRODUCT SCREENING")
    for r in results:
        e = r.economics
        mark = {"APPROVE": "PASS", "NEEDS_HUMAN_APPROVAL": "PASS (approval needed)",
                "REJECT": "REJECT", "HOLD": "HOLD"}[r.decision.value]
        print(f"\n{r.sku}  [{mark}]  score {r.score:.0f}")
        print(f"  price ${e.sale_price:.2f} | landed ${e.landed_cost:.2f} | "
              f"profit ${e.net_profit:.2f} | ROI {e.roi_pct:.1f}% | margin {e.margin_pct:.1f}%")
        for g in r.gates:
            if not g.passed:
                print(f"  FAIL  {g.name}: {g.detail}")
        if args.verbose:
            for g in r.gates:
                if g.passed:
                    print(f"  ok    {g.name}: {g.detail}")
        for n in r.notes:
            print(f"  note: {n}")
    approved = [r for r in results if r.decision.value != "REJECT"]
    print(f"\n{len(approved)}/{len(results)} candidates cleared every gate.")
    return 0


def cmd_listing(args, policy, store) -> int:
    cand = next((c for c in load_candidates() if c.sku == args.sku), None)
    if not cand:
        print(f"Unknown SKU {args.sku}", file=sys.stderr)
        return 1

    screen = screen_candidate(policy, cand)
    if screen.decision.value == "REJECT":
        print(f"Refusing to draft a listing for {args.sku}: it failed screening.")
        print(f"  {screen.reason_summary()}")
        print("\nDrafting content for a product that cannot be sold compliantly wastes "
              "effort and invites someone to publish it anyway.")
        return 1

    d = generate_listing(cand)
    _hr(f"LISTING DRAFT — {d.sku} ({d.marketplace})")
    print(f"\nTITLE ({len(d.title)} chars)\n  {d.title}")
    print("\nBULLETS")
    for b in d.bullets:
        print(f"  • {b}")
    print(f"\nDESCRIPTION\n{d.description}")
    print(f"\nBACKEND KEYWORDS ({len(d.backend_keywords.encode())} bytes)\n  {d.backend_keywords}")
    print("\nIMAGE BRIEFS")
    for img in d.image_briefs:
        print(f"  [{img['slot']}] {img['concept']}")
        print(f"      spec: {img['spec']}")
        print(f"      why:  {img['why']}")
    print("\nCOMPARISON CHART")
    print(f"  {' | '.join(d.comparison_chart['columns'])}")
    for row in d.comparison_chart["rows"]:
        print(f"  {row}")
    print(f"  ! {d.comparison_chart['note']}")
    print("\nFAQ")
    for f in d.faq:
        print(f"  Q: {f['q']}\n  A: {f['a']}")
    print("\nA+ CONTENT MODULES")
    for m in d.aplus_modules:
        print(f"  - {m['module']}: {m['content']}")
    if d.warnings:
        print("\nWARNINGS")
        for w in d.warnings:
            print(f"  ! {w}")
    print("\nNOTE: image briefs are production specs, not generated images. The operator "
          "does not fabricate product photography — a rendered image of a product you "
          "have not received is a returns and policy problem waiting to happen.")
    return 0


def cmd_suppliers(args, policy, store) -> int:
    ops, _ = load_operations()
    quotes = ops.get("supplier_quotes", {}).get(args.sku)
    if not quotes:
        print(f"No supplier quotes on file for {args.sku}.", file=sys.stderr)
        return 1

    sups = [_supplier_from(q) for q in quotes]
    scored = score_suppliers(policy, sups)
    _hr(f"SUPPLIER SCORECARD — {args.sku}")
    print(f"{'Supplier':<28}{'Total':>7}{'Cost':>7}{'Ship':>7}{'Qual':>7}{'Comm':>7}{'Stab':>7}  Status")
    for s in scored:
        status = "DISQUALIFIED" if s.disqualified else "eligible"
        print(f"{s.name[:27]:<28}{s.total:>7.1f}{s.landed_cost_score:>7.1f}"
              f"{s.shipping_score:>7.1f}{s.quality_score:>7.1f}"
              f"{s.communication_score:>7.1f}{s.stability_score:>7.1f}  {status}")
        for r in s.disqualification_reasons:
            print(f"    - {r}")
        store.record_supplier_score(
            supplier_id=s.supplier.supplier_id, supplier_name=s.name, score=s.total,
            on_time_rate_pct=s.supplier.on_time_rate_pct,
            defect_rate_pct=s.supplier.defect_rate_pct,
        )

    winner, notes = select_supplier(policy, sups)
    print()
    for n in notes:
        print(f"  {n}")

    if winner:
        cand = next((c for c in load_candidates() if c.sku == args.sku), None)
        max_cost = winner.supplier.unit_cost * 1.15
        if cand:
            e = economics_for_candidate(policy, cand)
            headroom = e.net_profit - (cand.target_price * float(policy.selection["min_margin_pct"]) / 100)
            max_cost = winner.supplier.unit_cost + max(headroom, 0)
        brief = build_negotiation_brief(
            winner=winner, alternatives=scored,
            annual_volume_units=args.volume, max_acceptable_cost=max_cost,
        )
        _hr("NEGOTIATION BRIEF")
        print(f"  Current:    ${brief.current_unit_cost:.2f}/unit")
        print(f"  Target:     ${brief.target_unit_cost:.2f}/unit")
        print(f"  Walk away above: ${brief.walk_away_cost:.2f}/unit")
        if brief.walk_away_cost <= brief.current_unit_cost:
            print("    ^ The walk-away price is at or below what you already pay: at the")
            print("      current sale price this SKU does not clear the margin gate. A cost")
            print("      reduction is a precondition for sourcing it, not an optimisation.")
        print("\n  Leverage:")
        for l in brief.leverage_points:
            print(f"    - {l}")
        print("\n  Asks:")
        for a in brief.asks:
            print(f"    - {a}")
        print(f"\n  DRAFT MESSAGE (review before sending — the operator does not send\n"
              f"  commercial commitments on your behalf):\n")
        for line in brief.draft_message.splitlines():
            print(f"    {line}")
    return 0


def cmd_price(args, policy, store) -> int:
    cand = next((c for c in load_candidates() if c.sku == args.sku), None)
    if not cand:
        print(f"Unknown SKU {args.sku}", file=sys.stderr)
        return 1
    ops, _ = load_operations()
    offers = ops.get("competitors", {}).get(args.sku, [])
    if not offers:
        print(f"No competitor data on file for {args.sku}.", file=sys.stderr)
        return 1

    comps = [
        CompetitorOffer(
            seller=o["seller"], price=float(o["price"]), rating=float(o.get("rating", 0)),
            review_count=int(o.get("review_count", 0)),
            in_stock=bool(o.get("in_stock", True)),
        )
        for o in offers
    ]
    rec = recommend_price(
        policy=policy, sku=args.sku, marketplace=cand.marketplace,
        current_price=args.current or cand.target_price, supplier=cand.supplier,
        competitors=comps, duty_pct=cand.duty_pct,
        ad_cost_per_unit=(args.current or cand.target_price) * 0.10,
        our_rating=args.rating, our_review_count=args.reviews,
    )
    _hr(f"PRICE RECOMMENDATION — {rec.sku}")
    print(f"  Current:     ${rec.current_price:.2f}")
    print(f"  Recommended: ${rec.recommended_price:.2f}  ({rec.change_pct:+.1f}%)  [{rec.action}]")
    print(f"  Profit floor:${rec.floor_price:.2f}   Target: ${rec.target_price:.2f}")
    print(f"  Projected:   {rec.projected_margin_pct:.1f}% margin, "
          f"${rec.projected_unit_profit:.2f}/unit")
    print(f"  Approval:    {'REQUIRED' if rec.requires_approval else 'within autonomous limits'}")
    print("\n  Reasoning:")
    for r in rec.rationale:
        print(f"    - {r}")
    return 0


# ---------------------------------------------------------------------------
# Amazon SP-API commands. Each requires live credentials and says so plainly
# rather than falling back to seed data — a silent fallback here would mix
# simulated and real numbers in the same output.
# ---------------------------------------------------------------------------
def _amazon(policy, *, writes: bool = False):
    """Build the Amazon connector, honouring the policy's execution mode."""
    from connectors.amazon import AmazonConnector
    allow = writes and policy.live_trading_enabled
    if writes and not policy.live_trading_enabled:
        print("Note: meta.live_trading_enabled is false — writes stay blocked.\n")
    return AmazonConnector(allow_writes=allow)


def cmd_amazon_verify(args, policy, store) -> int:
    result = _amazon(policy).verify_connection()
    _hr("AMAZON CONNECTION")
    print(f"  Result:  {'CONNECTED' if result['ok'] else 'NOT CONNECTED'}")
    print(f"  Detail:  {result.get('detail', '')}")
    for key in ("endpoint", "country", "currency", "sandbox", "status_code"):
        if result.get(key) not in (None, ""):
            print(f"  {key.replace('_', ' ').title():<9}{result[key]}")
    if result.get("seller_marketplaces"):
        print(f"  Markets: {', '.join(result['seller_marketplaces'])}")
    return 0 if result["ok"] else 1


def cmd_amazon_search(args, policy, store) -> int:
    env = _amazon(policy).search_products(args.keywords, max_pages=args.pages)
    _hr(f"CATALOG SEARCH — {' '.join(args.keywords)}")
    print(f"  source: {env.source} · fetched {env.fetched_at} · {len(env.payload)} results\n")
    for row in env.payload:
        rank = row.get("best_sales_rank")
        print(f"  {row['asin']}  {(row.get('title') or '')[:62]}")
        print(f"            brand={row.get('brand') or '-'}  "
              f"type={row.get('product_type') or '-'}  "
              f"rank={rank if rank else '-'}")
    print("\n  Sales rank is a demand proxy, not a demand figure. Screen these with "
          "`screen` before treating any of them as an opportunity.")
    return 0


def cmd_amazon_fees(args, policy, store) -> int:
    """Reconcile policy fee estimates against Amazon's own calculation."""
    env = _amazon(policy).fetch_fee_breakdown(args.asin, args.price, is_fba=not args.fbm)
    p = env.payload
    _hr(f"FEE RECONCILIATION — {args.asin} @ ${args.price:.2f}")
    print(f"  Referral fee:   ${p['referral_fee']:.2f}  ({p['referral_pct']:.2f}%)")
    print(f"  Fulfilment fee: ${p['fba_fee']:.2f}")
    if p["variable_closing_fee"]:
        print(f"  Closing fee:    ${p['variable_closing_fee']:.2f}")
    print(f"  TOTAL:          ${p['total_fees']:.2f}")

    policy_fees = policy.fees_for("amazon")
    est_referral_pct = float(policy_fees.get("referral_pct", 0.0))
    est_fulfilment = float(policy_fees.get("fulfillment_flat", 0.0))

    print("\n  Against config/policy.toml [fees.amazon]:")
    print(f"    referral_pct       policy {est_referral_pct:.2f}%  "
          f"actual {p['referral_pct']:.2f}%  "
          f"delta {p['referral_pct'] - est_referral_pct:+.2f}pp")
    print(f"    fulfillment_flat   policy ${est_fulfilment:.2f}  "
          f"actual ${p['fba_fee']:.2f}  "
          f"delta ${p['fba_fee'] - est_fulfilment:+.2f}")

    drift = abs(p["referral_pct"] - est_referral_pct) > 1.0 or \
        abs(p["fba_fee"] - est_fulfilment) > 0.50
    if drift:
        print("\n  These differ materially. Every profit figure, price floor, and "
              "\n  break-even ACOS in the system is computed from the policy values, "
              "\n  so update [fees.amazon] before trusting any of them. Note in the "
              "\n  commit which ASIN and price the numbers came from.")
    else:
        print("\n  Policy estimates match Amazon's calculation for this item.")
    return 0


def cmd_amazon_inventory(args, policy, store) -> int:
    env = _amazon(policy).fetch_inventory()
    _hr("AMAZON FBA INVENTORY")
    print(f"  source: {env.source} · fetched {env.fetched_at}\n")
    print(f"  {'SKU':<24}{'ASIN':<13}{'On hand':>8}{'Inbound':>9}"
          f"{'Reserved':>10}{'Unfulfil':>10}")
    for r in env.payload:
        print(f"  {(r['sku'] or '')[:23]:<24}{(r['asin'] or '')[:12]:<13}"
              f"{r['on_hand_units']:>8}{r['inbound_units']:>9}"
              f"{r['reserved_units']:>10}{r['unfulfillable_units']:>10}")
    for w in env.warnings:
        print(f"\n  ! {w}")
    print("\n  Feed these into the reorder engine with `daily` once velocity history "
          "exists — reorder points need trailing sales, not a single snapshot.")
    return 0


def cmd_amazon_orders(args, policy, store) -> int:
    env = _amazon(policy).fetch_orders(since=args.since)
    _hr(f"AMAZON ORDERS SINCE {args.since}")
    print(f"  source: {env.source} · {len(env.payload)} orders\n")
    total = sum(r["order_total"] for r in env.payload)
    for r in env.payload[:args.limit]:
        print(f"  {r['order_id']}  {r['purchase_date'][:10]}  "
              f"{r['status']:<12}{r['channel']:<5}${r['order_total']:>8.2f}")
    print(f"\n  Gross order value: ${total:,.2f} across {len(env.payload)} orders")
    for w in env.warnings:
        print(f"  ! {w}")
    print("\n  Gross order value is not revenue and not profit — fees, refunds, and "
          "ad spend all come out of it.")
    return 0


def cmd_amazon_offers(args, policy, store) -> int:
    env = _amazon(policy).fetch_competitor_offers(args.asin)
    _hr(f"COMPETITIVE OFFERS — {args.asin}")
    print(f"  {env.payload['offer_count']} total offers\n")
    print(f"  {'Seller':<14}{'Landed':>9}{'List':>9}{'Ship':>7}"
          f"{'BuyBox':>8}{'FBA':>6}{'Rating':>8}{'Revs':>8}")
    for o in sorted(env.payload["offers"], key=lambda x: x["price"]):
        print(f"  {o['seller'][:13]:<14}${o['price']:>8.2f}${o['listing_price']:>8.2f}"
              f"${o['shipping']:>6.2f}{'yes' if o['is_buybox'] else '-':>8}"
              f"{'yes' if o['is_fba'] else '-':>6}{o['rating']:>8.1f}{o['review_count']:>8}")
    for w in env.warnings:
        print(f"\n  ! {w}")
    print("\n  Run `price <sku>` to turn this into a recommendation — it applies the "
          "profit floor these raw offers know nothing about.")
    return 0


def cmd_capital(args, policy, store) -> int:
    """Capital position, concentration, turns, and signal coverage."""
    from .capital import CapitalState, Position, concentration_report, portfolio_turns, reserves
    from .signals import available_weight, unavailable_sources_report

    ops, source = load_operations()
    positions = [
        Position(
            sku=x["sku"], category=x.get("category", "uncategorised"),
            supplier_id=x.get("supplier_id", "unknown"),
            units_on_hand=int(x["on_hand_units"]),
            units_inbound=int(x.get("inbound_units", 0)),
            unit_cost=float(x.get("unit_cost", 0.0)),
            annual_units_sold=float(x.get("daily_velocity", 0.0)) * 365,
        )
        for x in ops.get("inventory", [])
    ]
    state = CapitalState(
        total_capital_usd=float(policy.capital["total_capital_usd"]),
        cash_available_usd=float(ops.get("spend_state", {}).get("cash_available_usd", 0.0)),
        positions=positions,
    )
    res = reserves(policy)

    _hr("CAPITAL POSITION")
    if source != "live":
        print(f"  ! Positions are {source.upper()} data, not your real inventory.\n")
    print(f"  Total capital:  ${state.total_capital_usd:,.2f}")
    print(f"  Deployed:       ${state.deployed_usd:,.2f}")
    print(f"  Cash on hand:   ${state.cash_available_usd:,.2f}")
    print(f"  Reserves:       ${res['total']:,.2f}  "
          f"(ads ${res['advertising']:,.2f} / refunds ${res['refunds']:,.2f} / "
          f"contingency ${res['contingency']:,.2f})")
    print(f"  Deployable:     ${res['deployable']:,.2f}")

    turns = portfolio_turns(policy, state)
    _hr("INVENTORY TURNS")
    print(f"  Portfolio: {turns['portfolio_turns']:.2f}/yr  "
          f"(target {turns['target_turns']:.1f}, floor {turns['min_turns']:.1f})")
    for sku, t in sorted(turns["per_sku"].items(), key=lambda kv: kv[1]):
        flag = "DEAD" if t <= 0 else ("SLOW" if t < turns["min_turns"] else "")
        print(f"    {sku:<24}{t:>7.2f}/yr  {flag}")
    if turns["capital_in_dead"]:
        print(f"\n  ${turns['capital_in_dead']:,.2f} in stock that is not moving at all.")

    conc = concentration_report(policy, state)
    _hr("CONCENTRATION")
    for dim in ("sku", "category", "supplier_id"):
        shares = conc["shares"].get(dim, {})
        top = sorted(shares.items(), key=lambda kv: -kv[1])[:3]
        print(f"  {dim}:")
        for key, pct in top:
            print(f"    {key:<28}{pct:>6.1f}%")
    if conc["breaches"]:
        print("\n  BREACHES:")
        for b in conc["breaches"]:
            print(f"    {b['dimension']} '{b['value']}' at {b['share_pct']:.0f}% "
                  f"(limit {b['limit_pct']:.0f}%)")
        print("\n  Concentration is the risk that ends businesses. A good margin does")
        print("  not offset it — one suspension or supplier failure at this weighting")
        print("  is fatal, regardless of how profitable the SKU is.")
    for u in conc.get("unmapped", []):
        print(f"\n  ! {u['detail']}")

    _hr("MARKET SIGNAL COVERAGE")
    print(f"  {available_weight(policy):.0f}% of the weighted signal set is observable.\n")
    for line in unavailable_sources_report(policy):
        print(f"    - {line}")
    print("\n  These are reported as unavailable, never estimated. A guessed trend")
    print("  signal survives into a purchase order and becomes inventory.")
    return 0


# ---------------------------------------------------------------------------
# TikTok Shop commands.
# ---------------------------------------------------------------------------
def _tiktok(policy, *, writes: bool = False):
    from connectors.tiktok import TikTokShopConnector
    allow = writes and policy.live_trading_enabled
    if writes and not policy.live_trading_enabled:
        print("Note: meta.live_trading_enabled is false — writes stay blocked.\n")
    return TikTokShopConnector(allow_writes=allow)


def cmd_tiktok_verify(args, policy, store) -> int:
    result = _tiktok(policy).verify_connection()
    _hr("TIKTOK SHOP CONNECTION")
    print(f"  Result:  {'CONNECTED' if result['ok'] else 'NOT CONNECTED'}")
    print(f"  Detail:  {result.get('detail', '')}")
    for key in ("region", "currency", "settlement_lag_days", "shop_name",
                "shop_region", "sandbox", "code", "status_code"):
        if result.get(key) not in (None, ""):
            print(f"  {key.replace('_', ' ').title():<20}{result[key]}")
    for shop in result.get("authorised_shops", []):
        print(f"  Authorised shop:    {shop}")
    for w in result.get("warnings", []):
        print(f"  ! {w}")
    if result["ok"]:
        print(f"\n  Settlement lag is {result.get('settlement_lag_days', '?')} days —")
        print("  that is the gap between a sale and the cash arriving, and it is")
        print("  longer than Amazon's. Plan reorder cash around it.")
    return 0 if result["ok"] else 1


def cmd_tiktok_products(args, policy, store) -> int:
    env = _tiktok(policy).fetch_inventory()
    _hr("TIKTOK SHOP INVENTORY")
    print(f"  source: {env.source} · {len(env.payload)} SKUs\n")
    print(f"  {'SKU':<20}{'Product':<28}{'Stock':>7}{'Price':>10}  Status")
    for r in env.payload:
        print(f"  {str(r['sku'])[:19]:<20}{str(r['title'])[:27]:<28}"
              f"{r['on_hand_units']:>7}{r['price']:>10.2f}  {r['status']}")
    for w in env.warnings:
        print(f"\n  ! {w}")
    return 0


def cmd_tiktok_orders(args, policy, store) -> int:
    env = _tiktok(policy).fetch_orders(since=args.since)
    _hr(f"TIKTOK ORDERS SINCE {args.since}")
    print(f"  source: {env.source} · {len(env.payload)} orders\n")
    total = sum(r["buyer_total"] for r in env.payload)
    for r in env.payload[:args.limit]:
        print(f"  {r['order_id']}  {r['created_at'][:10]}  "
              f"{str(r['status']):<12}{r['buyer_total']:>9.2f} {r['currency']}")
    print(f"\n  Buyer-paid total: {total:,.2f}")
    for w in env.warnings:
        print(f"  ! {w}")
    return 0


def cmd_tiktok_settlements(args, policy, store) -> int:
    """Reconcile the real platform take rate against policy estimates."""
    env = _tiktok(policy).fetch_settlements(days=args.days)
    p = env.payload
    _hr(f"TIKTOK SETTLEMENTS — LAST {args.days} DAYS")
    print(f"  Statements:     {len(p['statements'])}")
    print(f"  Revenue:        {p['total_revenue']:,.2f}")
    print(f"  Platform fees:  {p['total_fees']:,.2f}")
    print(f"  Take rate:      {p['take_rate_pct']:.2f}%")

    fees = policy.fees_for("tiktok")
    estimated = float(fees.get("referral_pct", 0)) + float(fees.get("payment_pct", 0))
    estimated += float(fees.get("affiliate_commission_pct", 0))
    print(f"\n  Policy [fees.tiktok] estimates {estimated:.2f}% "
          f"(commission + payment + affiliate)")
    drift = p["take_rate_pct"] - estimated
    print(f"  Drift: {drift:+.2f} percentage points")
    if abs(drift) > 3.0:
        print("\n  These differ materially. Every TikTok margin, price floor, and")
        print("  break-even in the system is computed from the policy estimate, so")
        print("  update [fees.tiktok] before trusting any of them. Note in the commit")
        print("  which date range the number came from.")
    else:
        print("\n  Policy estimates match the settled take rate for this window.")
    return 0


def cmd_tiktok_trends(args, policy, store) -> int:
    from .tiktok import DailyPoint, analyse_trend

    ops, source = load_operations()
    history = ops.get("tiktok_daily_performance", {})
    if not history:
        print("No TikTok daily performance history available.", file=sys.stderr)
        print("Trend analysis needs per-day history; a 30-day total cannot "
              "distinguish growth from a decaying spike.", file=sys.stderr)
        return 1

    _hr("TIKTOK TREND ANALYSIS")
    if source != "live":
        print(f"  ! This is {source.upper()} data, not your real performance.\n")

    min_days = int(policy.tiktok["min_trend_history_days"])
    for pid, rows in history.items():
        if pid.startswith("_"):
            continue
        points = [DailyPoint(day=r["day"], units=int(r["units"]),
                             gmv=float(r.get("gmv", 0)),
                             page_views=int(r.get("page_views", 0)),
                             orders=int(r.get("orders", 0)))
                  for r in rows]
        t = analyse_trend(product_id=pid, title=rows[0].get("title", pid),
                          history=points, min_days=min_days)
        print(f"\n  {t.title}  [{t.shape}]")
        print(f"    {t.prior_daily_units:.1f} -> {t.recent_daily_units:.1f} units/day "
              f"({t.change_pct:+.0f}%)")
        if t.peak_day:
            print(f"    peak {t.peak_day} ({t.days_since_peak}d ago), "
                  f"volatility {t.volatility:.2f}, conversion {t.conversion_rate_pct:.2f}%")
        print(f"    {t.interpretation}")
        print(f"    -> {t.inventory_guidance}")
        print(f"    ({t.confidence_note})")
    return 0


def cmd_tiktok_report(args, policy, store) -> int:
    """Daily TikTok optimisation pass over seed or live performance data."""
    from .tiktok import DailyPoint, analyse_trend, build_optimisation_report, compute_profit

    ops, source = load_operations()
    history = ops.get("tiktok_daily_performance", {})
    costs = ops.get("tiktok_costs", {})
    if not history:
        print("No TikTok performance history available.", file=sys.stderr)
        return 1

    _hr("TIKTOK DAILY OPTIMISATION REPORT")
    if source != "live":
        print("  ! NOT REAL DATA — this is seed input and exists to show the "
              "pipeline runs.\n")

    min_days = int(policy.tiktok["min_trend_history_days"])
    trends, profits, inventory = [], {}, []
    for pid, rows in history.items():
        if pid.startswith("_"):
            continue
        points = [DailyPoint(day=r["day"], units=int(r["units"]),
                             gmv=float(r.get("gmv", 0)),
                             page_views=int(r.get("page_views", 0)),
                             orders=int(r.get("orders", 0)))
                  for r in rows]
        trends.append(analyse_trend(product_id=pid, title=rows[0].get("title", pid),
                                    history=points, min_days=min_days))
        c = costs.get(pid, {})
        profits[pid] = compute_profit(
            policy=policy, product_id=pid,
            units=sum(p.units for p in points),
            gross_revenue=sum(p.gmv for p in points),
            cogs_per_unit=float(c.get("cogs_per_unit", 0.0)),
            shipping_per_unit=float(c.get("shipping_per_unit", 0.0)),
            ad_cost=float(c.get("ad_cost", 0.0)),
            affiliate_rate_pct=c.get("affiliate_rate_pct"),
        )
        inventory.append({"product_id": pid,
                          "on_hand_units": int(c.get("on_hand_units", 0))})

    print("  PROFIT BY PRODUCT")
    print(f"  {'Product':<28}{'Units':>7}{'GMV':>11}{'Net':>11}"
          f"{'Margin':>9}{'Take':>8}  Basis")
    for t in trends:
        p = profits[t.product_id]
        print(f"  {t.title[:27]:<28}{p.units:>7}{p.gross_revenue:>11,.2f}"
              f"{p.pre_tax_profit:>11,.2f}{p.margin_pct:>8.1f}%"
              f"{p.take_rate_pct:>7.1f}%  {p.revenue_basis}")

    total_pre = sum(p.pre_tax_profit for p in profits.values())
    total_post = sum(p.after_tax_profit for p in profits.values())
    print(f"\n  Pre-tax: {total_pre:,.2f}   After-tax: {total_post:,.2f}")

    actions, notes = build_optimisation_report(
        policy=policy, trends=trends, profits=profits, inventory=inventory,
        take_rate_pct=args.take_rate,
    )

    _hr("RECOMMENDED ACTIONS")
    if not actions:
        print("  Nothing requiring action today.")
    for i, a in enumerate(actions, 1):
        flag = " [NEEDS APPROVAL]" if a.requires_approval else ""
        print(f"\n  {i}. [{a.priority}] {a.title} — {a.action}{flag}")
        print(f"     {a.rationale}")
        if a.estimated_impact_usd:
            print(f"     Estimated impact: ${a.estimated_impact_usd:,.2f}")
    if notes:
        print()
        for n in notes:
            print(f"  ! {n}")
    return 0


def cmd_approvals(args, policy, store) -> int:
    pending = store.pending_approvals()
    _hr(f"PENDING APPROVALS ({len(pending)})")
    if not pending:
        print("  Nothing awaiting approval.")
        return 0
    for d in pending:
        print(f"\n  [{d['action_id']}] {d['domain'].upper()} — {d['sku']}")
        print(f"    Action:   {d['action']}")
        print(f"    Why:      {d['rationale'][:400]}")
        print(f"    Expected: {d['expected_outcome']}")
        print(f"    When:     {d['created_at']}")
    print(f"\n  Approve: python -m operator_core.cli approve <action_id> --by \"Your Name\"")
    return 0


def cmd_approve(args, policy, store) -> int:
    if store.approve(args.action_id, args.by):
        print(f"Approved {args.action_id} (by {args.by}).")
        if not policy.live_trading_enabled:
            print("Note: live_trading_enabled is false, so this is recorded but not "
                  "executed against any marketplace.")
        return 0
    print(f"No such action {args.action_id}.", file=sys.stderr)
    return 1


def cmd_outcome(args, policy, store) -> int:
    met = args.met.lower() in ("true", "yes", "1", "y")
    ok = store.record_outcome(args.action_id, {
        "met_expectation": met,
        "note": args.note,
        "actual_value": args.value,
    })
    if ok:
        print(f"Outcome recorded for {args.action_id}. This feeds the hit-rate stats "
              "that calibrate future recommendations.")
        return 0
    print(f"No such action {args.action_id}.", file=sys.stderr)
    return 1


def cmd_journal(args, policy, store) -> int:
    _hr("RECENT DECISIONS")
    for d in store.recent_decisions(limit=args.limit):
        flag = "*" if d["requires_approval"] and not d["approved_by"] else " "
        print(f"{flag} [{d['action_id']}] {d['created_at'][:16]}  {d['domain']:<12} "
              f"{d['sku']:<24} {d['action'][:60]}")
    print(f"\n{json.dumps(learning_summary(store), indent=2)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="operator", description="Autonomous ecommerce operator")
    ap.add_argument("--policy", help="path to policy.toml")
    ap.add_argument("--db", help="path to sqlite db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="connections, gates, and journal state")
    p_status.add_argument(
        "--no-verify", action="store_true",
        help="skip live API verification (offline / avoid burning rate limit)",
    )

    p_daily = sub.add_parser("daily", help="run the full daily cycle and write the report")
    p_daily.add_argument("--date", help="report date (YYYY-MM-DD)")

    p_screen = sub.add_parser("screen", help="screen product candidates")
    p_screen.add_argument("-v", "--verbose", action="store_true")

    p_list = sub.add_parser("listing", help="generate a full listing draft")
    p_list.add_argument("sku")

    p_sup = sub.add_parser("suppliers", help="score suppliers and build a negotiation brief")
    p_sup.add_argument("sku")
    p_sup.add_argument("--volume", type=int, default=5000, help="projected annual units")

    p_price = sub.add_parser("price", help="recommend a price against live competitors")
    p_price.add_argument("sku")
    p_price.add_argument("--current", type=float)
    p_price.add_argument("--rating", type=float, default=4.4)
    p_price.add_argument("--reviews", type=int, default=120)

    p_av = sub.add_parser("amazon-verify", help="verify the Amazon SP-API connection")

    p_as = sub.add_parser("amazon-search", help="search the Amazon catalog")
    p_as.add_argument("keywords", nargs="+")
    p_as.add_argument("--pages", type=int, default=2)

    p_af = sub.add_parser("amazon-fees",
                          help="reconcile policy fees against Amazon's own calculation")
    p_af.add_argument("asin")
    p_af.add_argument("price", type=float)
    p_af.add_argument("--fbm", action="store_true", help="merchant-fulfilled, not FBA")

    sub.add_parser("amazon-inventory", help="live FBA inventory")

    p_ao = sub.add_parser("amazon-orders", help="recent orders")
    p_ao.add_argument("--since", default="")
    p_ao.add_argument("--limit", type=int, default=25)

    p_aof = sub.add_parser("amazon-offers", help="competitor offers for an ASIN")
    p_aof.add_argument("asin")

    sub.add_parser("capital", help="capital position, concentration, turns, signals")

    sub.add_parser("tiktok-verify", help="verify the TikTok Shop connection")
    sub.add_parser("tiktok-products", help="live TikTok inventory")

    p_tto = sub.add_parser("tiktok-orders", help="recent TikTok orders")
    p_tto.add_argument("--since", default="")
    p_tto.add_argument("--limit", type=int, default=25)

    p_tts = sub.add_parser("tiktok-settlements",
                           help="reconcile the real take rate against policy")
    p_tts.add_argument("--days", type=int, default=30)

    sub.add_parser("tiktok-trends", help="classify demand curves per product")

    p_ttr = sub.add_parser("tiktok-report", help="daily TikTok optimisation report")
    p_ttr.add_argument("--take-rate", type=float, default=None,
                       dest="take_rate",
                       help="settled take rate %% for fee-drift comparison")

    sub.add_parser("approvals", help="list actions awaiting human approval")

    p_ap = sub.add_parser("approve", help="approve a pending action")
    p_ap.add_argument("action_id")
    p_ap.add_argument("--by", required=True)

    p_out = sub.add_parser("outcome", help="record what actually happened")
    p_out.add_argument("action_id")
    p_out.add_argument("--met", required=True, help="did it meet expectation: true/false")
    p_out.add_argument("--note", default="")
    p_out.add_argument("--value", default="")

    p_j = sub.add_parser("journal", help="show the decision journal")
    p_j.add_argument("--limit", type=int, default=25)

    args = ap.parse_args(argv)

    if getattr(args, "since", None) == "":
        from datetime import datetime, timedelta, timezone
        args.since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()

    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"POLICY ERROR: {exc}", file=sys.stderr)
        return 2

    store = Store(args.db)
    handlers = {
        "status": cmd_status, "daily": cmd_daily, "screen": cmd_screen,
        "listing": cmd_listing, "suppliers": cmd_suppliers, "price": cmd_price,
        "approvals": cmd_approvals, "approve": cmd_approve, "outcome": cmd_outcome,
        "journal": cmd_journal, "capital": cmd_capital,
        "amazon-verify": cmd_amazon_verify, "amazon-search": cmd_amazon_search,
        "amazon-fees": cmd_amazon_fees, "amazon-inventory": cmd_amazon_inventory,
        "amazon-orders": cmd_amazon_orders, "amazon-offers": cmd_amazon_offers,
        "tiktok-verify": cmd_tiktok_verify, "tiktok-products": cmd_tiktok_products,
        "tiktok-orders": cmd_tiktok_orders,
        "tiktok-settlements": cmd_tiktok_settlements,
        "tiktok-trends": cmd_tiktok_trends, "tiktok-report": cmd_tiktok_report,
    }
    try:
        return handlers[args.cmd](args, policy, store)
    except ConnectorNotConfigured as exc:
        # Correct refusal, but a traceback is the wrong way to say it.
        print(f"\nNOT CONFIGURED\n  {exc}", file=sys.stderr)
        print("\n  Run `status` to see every missing variable.", file=sys.stderr)
        return 2
    except SPAPIError as exc:
        print(f"\nAMAZON API ERROR\n  {exc}", file=sys.stderr)
        if exc.request_id:
            print(f"\n  Amazon request id: {exc.request_id} "
                  "(quote this in a Selling Partner support case).", file=sys.stderr)
        return 3
    except TikTokAPIError as exc:
        print(f"\nTIKTOK API ERROR\n  {exc}", file=sys.stderr)
        if exc.request_id:
            print(f"\n  TikTok request id: {exc.request_id} "
                  "(quote this in a Partner Center ticket).", file=sys.stderr)
        return 3
    except WriteNotPermitted as exc:
        print(f"\nBLOCKED\n  {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
