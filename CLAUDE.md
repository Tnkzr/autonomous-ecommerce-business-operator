# Operating instructions for this repository

This system makes decisions that spend money and that can get marketplace
accounts suspended. Work accordingly.

## Non-negotiables

1. **Never fabricate business data.** No invented revenue, competitor prices,
   search volumes, or demand estimates — not even as a placeholder or a demo.
   If data is unavailable, say so and raise. `DataEnvelope.source` must be
   accurate, and any report built from non-live data keeps its banner. Deleting
   or softening that banner is not a formatting change.

2. **Thresholds live in `config/policy.toml`, never in code.** If an engine
   needs a limit, read it from the policy. A hardcoded number is a rule that
   cannot be audited or changed by the business owner.

3. **Compliance gates are not overridable by economics.** Screening rejects on
   compliance before scoring. Do not add a "high ROI bypasses the flag" path.

4. **Every money-touching action goes through `risk.authorise()`.** Do not add
   a code path that spends, publishes, or mutates an account without it.

5. **Connectors fail loudly.** A missing credential raises. Never return `[]`
   or `0.0` to make a call site simpler — a silent zero becomes a wrong decision.

## Conventions

- Python 3.11+, standard library only. Do not add dependencies without asking;
  zero-install is a feature for an operator that must run anywhere.
- Money: `models.money()` at output boundaries. Floats are acceptable for
  per-unit decision math, not for settlement reconciliation.
- New engine → add to `pipeline.run_daily`, journal its decisions with a
  `dedupe_key` so re-runs stay idempotent, and surface it in `reporting`.
- Comments explain *why*, especially where a rule looks arbitrary (cooldown
  windows, minimum click thresholds, the 0.5 unsellable-return factor).

## Testing

`python3 -m unittest tests.test_operator` — 75 tests, must stay green.

The highest-value tests assert *refusal*: that hazmat blocks a profitable
product, that the repricer will not follow a rival below the floor, that caps
reject, that advisory mode blocks writes. When adding a guard rail, add the
test that proves it cannot be bypassed.

## Before changing fee schedules

`[fees.*]` values drive every profit calculation in the system. Changing them
silently changes which products qualify, what prices are floors, and which ads
look profitable. Reconcile against real settlement reports and say in the commit
message where the numbers came from.
