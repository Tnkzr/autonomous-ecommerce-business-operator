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

**Amazon SP-API is fully implemented** (`connectors/amazon/`). The other four
marketplaces are still declarations.

**No marketplace is connected in this checkout.** This repository contains the
operator; it does not contain your business. Until credentials exist it runs in
**advisory mode** on seed data, and:

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
python3 -m operator_core.cli capital                  # allocation, concentration, turns
python3 -m operator_core.cli approvals                # what needs your sign-off
python3 -m operator_core.cli approve <id> --by "Your Name"
python3 -m operator_core.cli outcome <id> --met true --note "sold through in 38d"
python3 -m unittest tests.test_operator tests.test_amazon tests.test_charter  # 184 tests
```

### Amazon commands (require live credentials)

```bash
python3 -m operator_core.cli amazon-verify                 # prove auth works
python3 -m operator_core.cli amazon-search "drawer organizer"
python3 -m operator_core.cli amazon-fees B08XXXXXXX 34.99  # reconcile fees
python3 -m operator_core.cli amazon-inventory
python3 -m operator_core.cli amazon-orders --since 2026-07-01
python3 -m operator_core.cli amazon-offers B08XXXXXXX
```

`status` verifies every configured connection by calling the API. Use
`status --no-verify` to skip the network round trip.

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
| `signals.py` | Market signals, confidence scoring, source availability. |
| `capital.py` | Allocation, concentration limits, inventory turns. |
| `account_health.py` | Marketplace performance metrics and scaling brake. |
| `risk.py` | The spend gate. Every money-touching action passes through `authorise()`. |
| `store.py` | SQLite decision journal, metrics, price and supplier history. |
| `reporting.py` | The daily report, including the provenance banner. |
| `pipeline.py` | The daily run that wires it all together. |
| `connectors/amazon/` | SP-API: auth → transport → client → connector. |
| `connectors/` | Other marketplace adapters. Fail loudly when unconfigured. |

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
- 21% planning tax rate applied to profit — the KPI is after-tax
- No SKU above 30% of deployed capital, no supplier above 45%, no category
  above 50%; 35% of capital held in reserve
- 3 corroborating signals and 60/100 confidence before a product is recommended
- Account health limits mirroring Amazon's published targets, warning at 75%

## Amazon SP-API

Implemented across four layers, each independently testable:

| Layer | Responsibility |
|---|---|
| `auth.py` | LWA token exchange and caching. Refreshes at 80% of lifetime so no request goes out on a nearly-dead token. Secrets are redacted from `__repr__`. |
| `transport.py` | Per-operation token-bucket rate limiting, retry with backoff, `Retry-After` handling, error translation. |
| `client.py` | One method per Amazon operation, with pagination and page caps. |
| `connector.py` | Maps responses into the operator's domain models, tagged `live`. |

Operations wired: Sellers (`getMarketplaceParticipations`), Catalog Items
2022-04-01 (search + get), Product Pricing v0 (competitive pricing, item
offers), Product Fees v0 (fee estimates), FBA Inventory v1, Listings Items
2021-08-01 (get/put/patch, price, quantity), Orders v0 (+ order items),
Reports 2021-06-30 (create → poll → download → parse), Tokens 2021-03-01 (RDT).

Things that took deliberate care:

- **Region is derived from the marketplace ID, not configured separately.** A
  wrong region authenticates fine and returns empty results, which reads as
  "no sales" rather than "misconfigured". `verify_connection` also checks the
  configured marketplace is one the seller actually sells in.
- **Price patches target `purchasable_offer`, not `list_price`.** The latter is
  the manufacturer's list price; patching it does not change what customers pay.
- **`productType` is read from the listing, never guessed.** A patch with the
  wrong product type is rejected.
- **Landed price includes shipping.** Comparing an FBA listing price against an
  MFN listing price without shipping makes the MFN rival look cheaper than it is.
- **Report documents are fetched without auth headers.** The URL is a pre-signed
  S3 link; sending the SP-API token there would leak a live credential to a
  third-party host.
- **`ACCEPTED` is not `applied`.** Amazon accepting a submission means the
  payload was well-formed. The connector says so rather than reporting success.
- **Reviews and ads raise instead of returning empty.** SP-API has no review
  endpoint and scraping breaches the Conditions of Use; ads live on a separate
  API. An empty list would report a badly-reviewed product as clean.

### Fee reconciliation

`amazon-fees <ASIN> <price>` calls Product Fees v0 and diffs Amazon's own
calculation against `[fees.amazon]` in the policy. This is the highest-value
thing to run first: those estimates drive every profit figure, price floor, and
break-even ACOS in the system.

## The charter engines

The operating charter lives in `CLAUDE.md`. Clauses that are only prose are
aspirations, so each one is enforced somewhere:

**After-tax profit is the KPI.** `economics.after_tax_profit` applies the
planning rate from `[tax]`. A 40% pre-tax ROI is roughly 32% after a 21% rate,
and that gap decides marginal products. Losses are not assumed refundable — that
would flatter a bad product.

**Cash flow is modelled separately from profit.** `cash_flow_projection` returns
days to first cash and days to full recovery. A profitable product on a 120-day
cash cycle can still bankrupt a business that reorders on schedule.

**Capital is allocated on risk-adjusted return, not headline ROI.** ROI is
discounted by confidence and normalised to a 90-day cycle, because uncertainty
and optimism look identical in a spreadsheet, and slow money is not the same as
fast money.

**Concentration limits bind even on the best product.** They are trimmed to the
limit rather than refused where headroom exists. A SKU that is already the whole
portfolio cannot be fixed by buying more of it — the fix is buying something else,
and the system says so.

**Confidence requires corroboration.** A candidate that clears every hard gate
but has thin evidence is HELD, not approved — "we do not know yet" is a distinct
answer from "no". Rankings use the score discounted by source coverage, so
poorly-understood opportunities cannot outrank well-understood ones on
optimistic arithmetic alone.

**Account health brakes growth.** A breach or near-breach strips
`INCREASE_BUDGET` actions from the day's recommendations. More volume through a
failing process produces more defects, not more profit.

**Suppliers are watched for drift.** Suppliers rarely fail suddenly — defect
rates creep and ship dates slip a few days at a time. `detect_deterioration`
compares recent performance against earlier, and every SKU gets a named backup
or an explicit single-source warning.

### Market signals: what is actually connected

The charter asks for continuous monitoring of Amazon, Google Trends, TikTok,
Reddit, Pinterest, YouTube, Meta, news, and economic indicators.

**Only Amazon sales rank is connected — 30% of the weighted signal set.**

`signals.py` declares all of them and reports each unconnected source with what
it would take to wire it. Nothing is estimated to fill the gap: an unavailable
source contributes zero and reduces coverage, rather than contributing a neutral
50. `ConfidenceAssessment.effective_score` is the raw score times coverage, and
that is what rankings use.

This matters more than it looks. A guessed trend signal does not stay a guess —
it survives into a purchase order and becomes inventory sitting in a warehouse.
Run `capital` to see the current coverage and the gap list.

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
2. Run `amazon-verify`. It exercises the full chain — refresh token, LWA
   exchange, app authorisation, region routing — and diagnoses what is wrong
   rather than throwing. For the other four marketplaces, implement the read
   methods in `connectors/marketplaces.py`; each class documents its endpoints
   and main gotcha.
3. **Reconcile the fee models in `[fees.*]`.** Run `amazon-fees` on several
   representative ASINs and update the policy. The shipped values are reasonable
   estimates, not your actual fees. Wrong fees mean confidently wrong profit on
   every downstream decision — this is the single highest-value calibration step.
4. Run in advisory mode for a few weeks. Record outcomes with the `outcome`
   command. Compare recommendations against what you would have done.
5. Only then consider `live_trading_enabled = true`, and even then the approval
   thresholds still apply.

## Limitations

- **The Amazon integration has never executed against a live account.** Every
  path is covered by offline tests against scripted responses shaped like
  Amazon's, but scripted responses are not the real API. Run reads first,
  compare a day's orders against Seller Central by hand, and only then consider
  enabling writes.
- Advertising is not implemented. It requires the separate Amazon Ads API.
- Seven of nine market signal sources have no connector. Confidence scores are
  computed from 30% of the intended evidence base and should be read as
  provisional.
- The tax model is a flat planning rate, not a tax engine. It does not handle
  nexus, quarterly estimates, depreciation, or entity structure. It exists so
  decisions are made on after-tax numbers, not to file anything.
- Seasonality needs twelve months of the operator's own sales history before it
  can contribute anything.
- Reviews are not retrievable via SP-API at all.
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
