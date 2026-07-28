# Autonomous Ecommerce Business Operator

A decision engine for running a multi-marketplace ecommerce operation across
Amazon, Shopify, Walmart Marketplace, eBay, and TikTok Shop.

It screens products, scores suppliers, prepares negotiations, calculates unit
economics, drafts listings, reprices against competitors, plans inventory,
optimises advertising, monitors reviews, and produces a daily report — with
every rule from the operating policy enforced in code and every decision
journaled so the system can be measured over time.

---

## Read this first

**No marketplace is connected.** This repository contains the operator; it does
not contain your business. Until credentials exist it runs in **advisory mode**
on seed data, and:

- it cannot read your real sales, inventory, ads, or reviews;
- it will not write to, publish on, or spend money through any account;
- every report built from seed data carries an unmissable banner saying so.

That last point is deliberate. The most dangerous output this system could
produce is a clean, confident report full of invented revenue. A number that
looks real and isn't will be acted on eventually. So provenance is tracked from
the loader to the printed page, and there is no code path that lets seed data
reach a report unlabelled.

```
python3 -m operator_core.cli status     # shows exactly what is missing
```

## Quick start

No dependencies — Python 3.11+ standard library only.

```bash
python3 -m operator_core.cli status                   # connections, gates, journal
python3 -m operator_core.cli daily                    # full cycle → reports/
python3 -m operator_core.cli screen -v                # screen candidates
python3 -m operator_core.cli listing SEED-PETBRUSH-03 # full listing draft
python3 -m operator_core.cli suppliers SEED-BAMBOO-ORG-01 --volume 8000
python3 -m operator_core.cli price SEED-BAMBOO-ORG-01
python3 -m operator_core.cli approvals                # what needs your sign-off
python3 -m operator_core.cli approve <id> --by "Your Name"
python3 -m operator_core.cli outcome <id> --met true --note "sold through in 38d"
python3 -m unittest tests.test_operator                # 75 tests
```

## How it is organised

| Module | Responsibility |
|---|---|
| `config.py` | Loads and validates `config/policy.toml`. Fails loudly on contradictions. |
| `economics.py` | Unit P&L, target-margin solver, break-even price, cash-cycle ROI. |
| `screening.py` | The product gates: ROI, margin, supplier, shipping, competition, IP/hazmat/medical. |
| `suppliers.py` | Weighted scorecard, selection, negotiation brief. |
| `listings.py` | SEO title, bullets, description, backend keywords, image briefs, chart, FAQ, A+. |
| `pricing.py` | Competitor-aware repricer with a hard profit floor. |
| `inventory.py` | Safety stock, reorder point, stockout prediction, overstock detection. |
| `advertising.py` | Per-SKU break-even ACOS, campaign verdicts, keyword harvest/negate. |
| `reviews.py` | Defect-theme clustering and account-risk escalation. |
| `risk.py` | The spend gate. Every money-touching action passes through `authorise()`. |
| `store.py` | SQLite decision journal, metrics, price and supplier history. |
| `reporting.py` | The daily report, including the provenance banner. |
| `pipeline.py` | The daily run that wires it all together. |
| `connectors/` | Per-marketplace adapters. Fail loudly when unconfigured. |

## The policy file is the constitution

`config/policy.toml` holds every threshold. Engines read it; they never hardcode
a limit. Change a number there and the whole system's behaviour changes.

Currently enforced:

- ROI ≥ 40%, net margin ≥ 30%, supplier rating ≥ 4.8, shipping < 12 days
  (waived for domestic stock)
- Hazmat, medical-claim, trademark, copyright, and patent-risk keyword screens
- Gated categories blocked until approval is on file
- Max single PO $2,000 · max daily spend $5,000 · max open exposure $15,000
- $3,000 cash reserve that is never spendable
- Human approval required above $500, above a 10% price change, for any first
  order with a new supplier, and for publishing any listing
- Eight actions that are never autonomous at any value (transferring funds,
  changing payout details, responding to IP claims, and similar)

## Design decisions worth knowing

**Compliance outranks profit, structurally.** A candidate that trips a
compliance gate is rejected before scoring, and no economic result can override
it. The seed data includes a posture corrector at 249% ROI that gets rejected
for medical claims — that is the system working. A suspended account ends the
business; one skipped product does not.

**Break-even ACOS is per-product.** A 45%-margin product can profitably run at
40% ACOS while a 22%-margin product bleeds at 30%. One portfolio-wide "target
ACOS" is the most expensive common mistake in ecommerce advertising, so the
break-even is derived from each SKU's own margin.

**The repricer has a floor it cannot be argued below.** When a competitor
prices under our cost, the operator holds and says why rather than following.
Competitors have different cost bases; a race to the bottom against someone who
bought cheaper is a race you lose.

**Statistical patience.** Nothing is paused, scaled, or negatived until minimum
click and spend thresholds are met. Acting on three clicks is noise-chasing
dressed up as optimisation.

**Returns are charged against every unit.** At a 5% return rate you lose the
outbound fulfilment fee, refund admin, and roughly half the goods value on that
5%. Omitting this is how a "40% ROI" product turns out to make nothing.

**Lead-time variance drives safety stock.** A 30-day supplier that occasionally
takes 45 will empty the shelf even when demand was forecast perfectly, so the
reorder point uses both demand and lead-time variance.

**The daily run is idempotent.** Re-running the same date returns existing
decision IDs rather than duplicating them; otherwise the journal inflates and
the hit-rate statistics that drive learning become meaningless.

**Only resolved decisions count as evidence.** An open recommendation is not a
win. `learning_summary` counts only decisions with recorded outcomes, and says
so when the sample is too small to mean anything.

## What it deliberately will not do

- **Generate product images.** It writes detailed production briefs — slot,
  concept, spec, and the conversion reason for each of seven images. A rendered
  picture of a product you have not physically received creates a
  not-as-described return problem and a listing-accuracy violation. The brief is
  the useful artefact; the photo needs the actual product.
- **Send supplier messages.** It drafts the negotiation, you send it. A
  commercial commitment sent under your business name should have a human
  behind it.
- **Publish listings or place orders autonomously.** Both are approval-gated
  regardless of value.
- **Invent market data.** No fabricated search volumes, competitor prices, or
  demand estimates. Where a number is unknown, the connector raises instead of
  returning a plausible-looking zero.

## Going live

1. Provision credentials for each marketplace (`status` lists the exact
   variables and links the API docs).
2. Implement the read methods in `connectors/marketplaces.py`. Each class
   documents its endpoints and its main gotcha. Start with reads only.
3. **Reconcile the fee models in `[fees.*]` against real settlement reports.**
   The shipped values are reasonable estimates, not your actual fees. Wrong fees
   mean confidently wrong profit on every downstream decision — this is the
   single highest-value calibration step.
4. Run in advisory mode for a few weeks. Record outcomes with the `outcome`
   command. Compare recommendations against what you would have done.
5. Only then consider `live_trading_enabled = true`, and even then the approval
   thresholds still apply.

## Limitations

- Fee schedules are estimates until reconciled (see above).
- The IP and hazmat screens are keyword heuristics tuned to over-flag. They are
  a triage layer, not a legal opinion, and they will not catch a design patent
  or an unregistered trademark. Anything commercially significant needs counsel.
- Competition scoring uses seller count, review moat, and incumbent rating. It
  does not model brand strength, seasonality, or PPC bid density.
- The learning loop measures whether outcomes matched expectations. It does not
  yet auto-tune thresholds from that history — deliberately, since automatic
  threshold drift on a system that spends money deserves its own review.
- Money is float dollars. Fine for per-unit decision math; migrate to integer
  cents before doing settlement reconciliation.
