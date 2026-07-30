# Autonomous Ecommerce Business Operator

A decision engine for an ecommerce content business: **organic TikTok video
drives traffic to a Shopify store**. TikTok Shop, Amazon, Walmart and eBay plug
into the same marketplace interfaces.

The loop it runs:

```
research → score → select → list on Shopify → make videos → publish
        → measure → learn → repeat
```

It screens products, scores suppliers, prepares negotiations, calculates unit
economics, drafts listings and Shopify products, generates ten creative angles
with shootable production packages, schedules publishing, diagnoses where the
funnel leaks, runs product tests that are allowed to fail, and reports what the
history actually supports concluding — with every rule from the operating
policy enforced in code and every decision journaled so the system can be
measured over time.

Because acquisition is organic, the cost of a customer is a shoot day rather
than a bid. There is no CAC to optimise down, so profit per order comes from
order value, repeat purchase and refund rate — and reach comes from hit rate
across many angles, not from spend.

---

## Read this first

**Amazon SP-API** (`connectors/amazon/`), **TikTok Shop**
(`connectors/tiktok/`), and **Shopify Admin GraphQL** (`connectors/shopify/`)
are fully implemented. Walmart and eBay are still declarations.

Shopify is the one that needs no eligibility review: a custom app in your own
store issues a permanent Admin API token in about two minutes. TikTok Shop Open
API registration is gated on seller eligibility; while it is blocked, the
operator runs on Seller Center CSV exports (`tiktok-import`), which are real
data moved by hand and are tracked with provenance `import` — distinct from both
`live` and `seed`.

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
python3 -m unittest tests.test_operator tests.test_amazon \
    tests.test_charter tests.test_tiktok tests.test_growth \
    tests.test_tiktok_import tests.test_shopify tests.test_creative \
    tests.test_growth_engine tests.test_intelligence \
    tests.test_growth_pipeline                        # 611 tests
```

### The growth loop

```bash
python3 -m operator_core.cli growth                   # run the whole loop once
python3 -m operator_core.cli research                 # which signal feeds exist
python3 -m operator_core.cli creative SEED-PETBRUSH-03    # 10 ideas/hooks/captions/CTAs
python3 -m operator_core.cli produce SEED-PETBRUSH-03     # call sheet, SRT, thumbnails
python3 -m operator_core.cli shopify-verify           # token AND scope check

# after posting a video, and after reading its numbers in the app
python3 -m operator_core.cli publish-log <package-id> --sku SEED-PETBRUSH-03 \
    --at 2026-08-01T19:00:00+00:00 --angle problem_solution --cta passive
python3 -m operator_core.cli video-metrics <package-id> \
    --views 41000 --likes 3100 --shares 480 --clicks 620 --watch-pct 48

python3 -m operator_core.cli calendar                 # posting times, cadence
python3 -m operator_core.cli funnel                   # where the funnel leaks
python3 -m operator_core.cli learn                    # what the history supports
python3 -m operator_core.cli dashboard                # everything, one screen
python3 -m operator_core.cli dashboard --html reports/dashboard.html

# tests that are allowed to fail
python3 -m operator_core.cli experiment register --sku SEED-PETBRUSH-03 \
    --hypothesis "Demo beats problem-callout" --variable angle \
    --metric click_rate --threshold 0.015 --baseline 0.012 \
    --arm "demo=satisfying demo" --arm "problem=problem callout"
python3 -m operator_core.cli experiment evaluate --experiment EXP-... --conclude
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

### TikTok Shop commands (require live credentials)

```bash
python3 -m operator_core.cli tiktok-daily                  # THE MAIN LOOP
python3 -m operator_core.cli signals                      # coverage + observations
python3 -m operator_core.cli signal <sku> <source> --strength N
python3 -m operator_core.cli tiktok-verify                 # prove auth works
python3 -m operator_core.cli tiktok-products               # inventory
python3 -m operator_core.cli tiktok-orders --since 2026-07-01
python3 -m operator_core.cli tiktok-settlements --days 30  # real take rate
python3 -m operator_core.cli tiktok-trends                 # demand curve shapes
python3 -m operator_core.cli tiktok-report                 # daily optimisation
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
| `tiktok.py` | TikTok profit, trend shapes, daily optimisation. |
| `tiktok_pipeline.py` | **The main loop.** Health → signals → profit → scoring → capital. |
| `scoring.py` | Twelve-dimension product scorecard with coverage. |
| `content.py` | Hooks, scripts, shot lists, creators, content calendar. |
| `weekly.py` | Weekly business review and self-derived engineering backlog. |
| `growth_pipeline.py` | **The content-to-cash loop.** Research → screen → creative → publish → measure → learn. |
| `creative.py` | Ten creative angles, hook bank, captions, CTA variants, originality screen. |
| `production.py` | Storyboards, computed timecodes, SRT, thumbnails, export metadata. |
| `publishing.py` | Publishing calendar, posting times from own data, cadence, engagement. |
| `conversion.py` | Funnel diagnosis, AOV, bundles, upsells, refund analysis. |
| `experiments.py` | Sample sizing, arm comparison, scale/archive/inconclusive verdicts. |
| `learning.py` | What the history supports concluding, and what it does not. |
| `research.py` | Opportunity pipeline, per-source provenance, coverage reporting. |
| `dashboard.py` | Terminal and self-contained HTML dashboard. |
| `connectors/credentials.py` | Provider-agnostic credential resolution. A new provider is a spec. |
| `connectors/amazon/` | SP-API: auth → transport → client → connector. |
| `connectors/tiktok/` | TikTok Shop: signing → auth → transport → client → connector + CSV import. |
| `connectors/shopify/` | Admin GraphQL: credentials → transport → queries → client → connector. |
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

## TikTok Shop

Same four-layer structure as Amazon, plus `signing.py` because every request
carries an HMAC-SHA256 signature.

Operations wired: Authorization 202309 (shops, shop_cipher), Product 202309
(search, get, create, update, price, inventory, activate/deactivate,
categories, category rules, brands), Order 202309 (search, detail), Finance
202309 (statements, transactions), Analytics 202405 (shop and per-product
performance), Logistics 202309 (warehouses).

### The traps this integration handles

**TikTok returns HTTP 200 for failures.** A rejected product, a bad
`shop_cipher`, an expired token, a rate limit — all arrive as `200 OK` with a
non-zero `code` in the body. A client that checks `response.status` treats every
one of those as a success, and an inventory sync built that way silently stops
syncing while reporting green. `transport._interpret` is the one place that
decides success, and it checks the code.

**The signature is unforgiving.** `sign` and `access_token` are excluded from
the base string, parameters are sorted, the secret wraps the payload on both
ends *and* is the HMAC key, and the signed body must be byte-identical to the
body sent. The test vector is computed by hand rather than captured from the
implementation, so it catches a change that is self-consistent but wrong.

**Refresh tokens rotate.** TikTok issues a new refresh token on every refresh.
Discard it and the integration keeps working until the old one expires, then
dies months later with no deploy to correlate against. The provider surfaces
rotation through a callback and warns when none is configured.

**shop_cipher is not a credential you configure.** It comes from the
authorisation endpoint and nearly every other call needs it, so the connector
fetches it at startup and verifies the configured shop is one the app can reach.

**Order value is not revenue.** Commission, transaction fees, affiliate
payouts, and seller-funded promotions land between the two — typically 15-25%.
`tiktok-settlements` reports the settled take rate against the policy baseline.

**Prices are strings.** `12.30` as a float serialises as `12.3` and is rejected.
The connector formats them; the client rejects a float before it reaches the API.

**Product updates replace rather than merge.** Any attribute omitted from an
update payload is cleared. The connector says so on every update.

### Trend analysis: shape, not level

The distinction that matters on TikTok is **growth versus spike-then-decay**.
Both produce a strong 30-day total. One justifies a reorder; the other means the
video that drove it stopped circulating and anything you buy will sit. A 30-day
sum cannot tell them apart, so `analyse_trend` classifies the curve — comparing
recent against prior, locating the peak, and checking whether the tail is
holding. On the seed data it correctly flags a product with 372 units sold and a
44% margin as `SPIKE_DECAY` with 90+ days of cover, and says not to reorder on
the peak.

### What TikTok does not expose

**There is no competitor-pricing endpoint.** No equivalent of Amazon's Product
Pricing API exists for TikTok sellers. Scraping the storefront breaches
TikTok's Terms of Service and risks the shop this system exists to protect, so
`fetch_competitor_offers` raises and explains the legitimate alternatives.
Fabricated competitor prices would feed the repricer directly and mis-price live
listings, which is the worst possible failure mode.

Reviews are not retrievable through the Partner API either, and ads live in the
separate TikTok Marketing API.

### Policy screening before every write

TikTok enforces content policy faster and more broadly than the other
marketplaces, and a violation can suspend the whole shop rather than one
listing. `screen_for_policy` runs before any create or update and blocks
prohibited categories and efficacy claims *before* the API call — no rate-limit
slot is spent on a product that would be rejected or, worse, accepted and then
enforced against.

`[tiktok]` in the policy file holds the write thresholds: product creates and
updates always require human approval, price moves are capped at 5%
autonomously, and inventory moves at 500 units.

## The growth engines

### Product scorecard (`scoring.py`)

Twelve dimensions: demand, trend, competition, margin, shipping, return risk,
policy risk, supplier, review sentiment, virality, cash flow, overall.

Two rules do the work:

**Unmeasured is `None`, not zero.** Zero means measured and bad. Collapsing the
two makes an unresearched product look identical to a researched terrible one.
Every scorecard reports coverage, and ranking uses score × coverage — an 80 we
can support beats a 90 we cannot.

**Risk dimensions cap, but only when genuinely weak.** Below 60, policy risk and
return risk stop averaging and start capping the overall. Averaging is how a
trademark landmine with great margins gets funded. Capping whenever risk merely
sits below average would cap everything and turn the warning into noise.

Virality is explicitly **structural** — how well the product demonstrates on
video, inferred from its own attributes. Real virality needs hashtag and sound
data that no connector supplies, and the scorecard says so rather than implying
it measured something it did not.

### Content strategy (`content.py`)

Hooks, voiceover scripts, second-by-second shot lists, captions, hashtags,
creator briefs, and a rotating publishing calendar.

What it refuses to do matters more than what it generates:

- **Voiceovers leave a specification line unfilled.** The operator has not
  received the product and does not know its material or dimensions. A script
  that invents them becomes a false advertising claim the moment it is filmed.
- **Social-proof hooks are omitted without a verified order count.** An
  invented number is a false claim TikTok penalises the shop for.
- **Hashtags are labelled evergreen, not trending.** Trend data needs a feed
  that does not exist; a fabricated trending tag sends real production budget
  at a guess.
- **Generated copy is screened against TikTok's claim policy** before anyone
  films it. The operator produces this text, so it checks its own output.

Creator commission is bounded by the product's actual margin — offering a rate
the product cannot fund buys volume at a loss.

### Weekly business review (`weekly.py`)

Profit, opportunities, risks, pipeline, store health, and experiments — each
experiment carrying a hypothesis, a success metric, and a minimum sample, so it
is a test rather than a change someone later claims credit for.

The **engineering backlog is derived, not written**. The system introspects on
measured gaps — signal coverage, unconfigured connectors, unresolved decisions,
scorecard coverage — and every item must state what it costs the business
today. An improvement that cannot answer that is a preference, and preferences
do not belong on a backlog competing with buying inventory.

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

- Run reads first on both marketplaces, reconcile a day's orders against Seller
  Central and Seller Center by hand, and only then consider enabling writes.
- Advertising is not implemented on any marketplace. Amazon needs the Ads API
  and TikTok needs the Marketing API; both are separate applications. The
  business model here is organic, so this is a gap rather than a blocker — but
  it means no spend-to-revenue join exists.
- **Neither integration has executed against a live account.** Every path is
  covered by offline tests against scripted responses, but scripted responses
  are not the real API.
- TikTok exposes no competitor data and no retrievable reviews.
- Virality scoring is structural, not observed. Hashtag momentum, trending
  sounds, and creator adoption have no public API — the TikTok Research API is
  approval-gated and Creative Center has none at all.
- LTV assumes a 0% repeat rate until order history keyed by buyer exists. That
  understates every customer's value and caps what the business will pay to
  acquire one; it is deliberate, because an invented repeat rate funds
  unprofitable acquisition.
- Ten of twelve market signal sources have no connector. Confidence scores are
  computed from 30% of the intended weighted evidence base and should be read
  as provisional. `research` prints the current figure and what each missing
  source would need.
- **TikTok publishes no organic-analytics API.** Views, watch time and link
  clicks for the shop's own posts are typed in from the app (`video-metrics`)
  and stored as `data_source='manual'` — a reading at a point in time, not a
  feed. Nothing in `learning.py` will conclude without them, and it says so
  rather than filling the gap.
- **Shopify's Admin API does not expose session counts.** Conversion rate and
  revenue-per-visitor are reported as unknown rather than back-computed from
  orders. A conversion rate built on a guessed denominator is the most
  confidently wrong number a store can produce.
- Shopify has no competitor pricing and no native reviews. Both calls raise
  with the reason; scraping rival storefronts is a ToS breach with legal
  exposure, and an empty list would read as "no competitors".
- There is no built-in "best time to post" table and there will not be one.
  Every published one is a different audience in a different timezone.
  `calendar` reads this account's own history or says it cannot yet — which
  needs about fifteen mature posts concentrated on a few slots.
- The trend-participation creative angle cannot be generated, only templated.
  Trending sounds and formats have no API and Creative Center scraping breaches
  ToS, so the slot stays unfilled for a human who can open the app.
- Content packages routinely come back not-shootable, by design. The generated
  voiceover leaves the product-specific line unwritten because the operator has
  not held the product; that line is a blocker rather than a warning, because
  the alternative is a shoot day where somebody improvises the sentence
  carrying the claim.
- Experiment sizing assumes a normal approximation to two proportions. At these
  sample sizes the effects are large or not worth having; do not read the
  interval width as precision.
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
