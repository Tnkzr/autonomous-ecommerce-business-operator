"""Command line interface.

    python -m operator_core.cli status
    python -m operator_core.cli daily
    python -m operator_core.cli screen
    python -m operator_core.cli listing SEED-BAMBOO-ORG-01
    python -m operator_core.cli suppliers SEED-BAMBOO-ORG-01
    python -m operator_core.cli price SEED-BAMBOO-ORG-01
    python -m operator_core.cli approvals
    python -m operator_core.cli approve <action_id> --by "Name"
    python -m operator_core.cli outcome <action_id> --met true --note "..."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors import all_status  # noqa: E402

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
    any_live = False
    for s in all_status():
        state = "READY" if s["configured"] else "NOT CONFIGURED"
        any_live = any_live or s["configured"]
        print(f"  {s['marketplace']:<10} {state}")
        if s["missing_env"]:
            print(f"             missing: {', '.join(s['missing_env'])}")
            print(f"             docs:    {s['docs']}")

    if not any_live:
        print(
            "\n  No marketplace is connected. The operator runs in advisory mode on\n"
            "  seed data only. It cannot read your real sales, inventory, or ads, and\n"
            "  it will not present simulated numbers as if they were real."
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

    sub.add_parser("status", help="connections, gates, and journal state")

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
        "journal": cmd_journal,
    }
    return handlers[args.cmd](args, policy, store)


if __name__ == "__main__":
    raise SystemExit(main())
