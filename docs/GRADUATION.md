# Graduation criteria — paper (1 SOL) → live (1 SOL)

> **Nothing in this repository can trade.** There is no signer, no wallet and no RPC, and
> `tests/test_safety.py` fails the build if a signing library or key-material identifier
> reaches `src/`. This document defines **when the question would be worth asking**, not a
> switch to flip. Graduating means starting a different, deliberate piece of work — and
> `docs/SAFETY.md` governs whether it happens at all.

The point of writing this down *before* there is a result is that a criterion invented
after seeing the numbers is not a criterion. It is a description of the numbers.

## The gate

All seven must hold **simultaneously, on one dataset, without re-running the sweep to find
a corner where they do.**

| # | Criterion | Threshold | Why this number |
|---|---|---|---|
| 1 | Signalled observations with a **final** outcome | **≥ 400** | See "why not 100" below. |
| 2 | Distinct tokens contributing signals | **≥ 150** | 63 signals across 42 tokens means the effective sample is 42, not 63. Per-token clustering is the dominant correlation here. |
| 3 | `observations_pending / (pending + final)` | **≤ 0.20** | The early-resolution bias. Above this the finalised set is mostly tokens that moved sharply. |
| 4 | Expectancy per resolved signal, after fees and slippage | **> 0** with a **95% bootstrap CI whose lower bound is also > 0** | A point estimate above zero is what a random walk produces half the time. |
| 5 | Signal expectancy **minus** non-signal expectancy on the same dataset | **> 0**, same CI rule | The counterfactual. This is the only number that says the *strategy* works rather than the *market* being up that week. It is available only because §16 stores a vector for every observation, not just the ones that fired. |
| 6 | Max realised drawdown over the paper equity curve | **≤ 25%** | Matches `risk_max_drawdown_fraction`. Measurable only since `peak_equity_sol` became a true high-water mark — the old `max(start, now)` reported zero for exactly the drawdowns this gate is about. |
| 7 | Result survives the **friction haircut** below | still passes #4 and #5 | Paper trading is optimistic in ways that are now sized. |

## Why not 100

`research_ready_threshold` defaults to **100**, and that is a good threshold for *"there is
enough here to run the report"*. It is not enough to *act* on, and the two should not be
confused because they share a number.

At a 20% win rate — roughly what the current TP 30% / SL 20% barriers imply — 100 samples
puts the standard error on the win rate near 4 percentage points, so a strategy that is
truly break-even and one that is truly good are inside each other's error bars. Criterion 2
makes it worse: signals cluster on tokens, so 100 signals across ~65 tokens carries less
information than 100 independent draws.

**400 is the smallest number at which criterion 4's confidence interval can exclude zero for
an effect of the size worth trading.** It is a sample-size floor, not a promise.

So: the Discord ping at 100 means *"come and look"*. This document is what "look" means.

## The friction haircut (criterion 7)

Measured 2026-08-20, `scripts/probe_paper_optimism.py` — see `docs/PAPER_TRADING.md`:

- **Curve drift between snapshots: median 15.9%, worst 47.6%** over the 12-second gap,
  against our own exactly-modelled slippage of 0.02%–0.07%. The unmodelled part is two to
  three orders of magnitude larger than the modelled part.
- **Priority fees / MEV and failed transactions: unmeasured**, and unmeasurable from here
  without a signer. They are costs, so their true value is worse than the zero the
  simulator uses.

The haircut is therefore **not a coefficient**, and inventing one would be the substitution
this project exists to avoid. It is a **precondition**: criterion 7 cannot be evaluated
honestly while the snapshot interval is 10 seconds, because the drift inside that interval
dominates the edge being measured. Either the interval comes down far enough that drift is
small next to expectancy, or graduation is decided on a number known to be optimistic by an
unknown margin.

**Of the four ways the simulation is optimistic, this is the one that blocks the gate**, and
it is an interval problem rather than a modelling problem.

## What passing does not authorise

Passing all seven means the hypothesis is worth a *deliberate, separately scoped* decision
about real execution, subject to `docs/SAFETY.md`. It does not authorise:

- widening position size beyond the 1 SOL the criteria were measured at — none of these
  numbers survive a size change, since drift and slippage both scale with it;
- reusing the result for a different strategy, a different barrier pair, or a later
  `FEATURE_VERSION`, each of which invalidates the dataset it was measured on;
- skipping a fresh paper run after any parameter change. A criterion met before the change
  says nothing about after it.

## How to evaluate it

```bash
curl -s http://<host>:8000/status | jq '.outcomes'          # criteria 1 and 3
python scripts/research_report.py <database> tp30_sl20_1h   # criteria 4 and 5
python scripts/sweep_phase7.py <database> tp30_sl20_1h      # sensitivity of 4 to the barriers
```

Criterion 5 is the one to read first. If signal and non-signal expectancy are the same, the
other six do not matter.
