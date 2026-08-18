# Outcomes and research — Phase 7

Outcome engine, triple-barrier labelling, the §16 dataset, and the §17 questions.

## Outcomes are recorded for every observation

Spec §15: regardless of whether a signal fired or a trade was taken. The observations
we did **not** act on are the counterfactual — without them the dataset can describe our
trades and cannot say whether the hypothesis is any good.

Each observation gets one row per labelling scheme, because "did +30% happen before
-20%?" is only one of the questions the dataset should be able to answer. Three schemes
ship by default: `tp30_sl20_1h`, `tp50_sl25_1h`, `tp20_sl10_15m`.

## Forward-looking here is correct, and that is the point of the separation

Phase 4 spent its design budget making it impossible for a *feature* to see the future.
This module does the opposite on purpose: an outcome **is** what happened next.

What keeps both honest is that they cannot mix. Features come from `series.up_to(t)`;
outcomes come from what follows `t`; nothing joins them except a stored `observation_id`.
A pipeline that computed both from the same window is exactly the leak that produces
excellent offline metrics from a model that cannot work.

## UNRESOLVED is not TIMEOUT

A timeout means "neither barrier was touched in the allotted time". Unresolved means "we
do not know yet". Collapsing them fills the dataset with label noise wearing the costume
of evidence — and it is worst for the newest tokens, which are exactly the population the
research is about.

`observation_outcomes` is therefore the **one mutable table** in the system: a label
starts UNRESOLVED and is rewritten until its window elapses. That is correct here because
the row is a running measurement, not a record of a decision. `is_final` marks the point
past which it stops changing, and the dataset export excludes non-final rows by default.

## 🔴 Early-resolution bias — the finding that matters most

**A label goes final in one of two ways: a barrier is touched, or the window elapses.**
Early in a collection run only the first has happened. So the finalised set is made almost
entirely of tokens that moved sharply, while the quiet ones sit in "pending".

Reading an expectancy off that subset is reading the expectancy of *volatile tokens* and
calling it the market.

Measured on a 300-second live run: **34 of 646 observations (5.3%) had a final label, and
of those, 0% timed out.** Every single finalised row was a barrier touch. A 5% sample
selected on "moved at least 10-20% within minutes" is not a sample of the universe.

This cannot be corrected for. It can only be waited out — so `research_report.py` prints
the warning itself rather than a number that looks like evidence.

## First live result, stated plainly

From that same 300-second run (`tp20_sl10_15m`, +20%/-10% over 15 minutes):

| | n | resolved | upper | lower | expectancy | profit factor |
|---|---|---|---|---|---|---|
| all observations | 34 | 34 | 20.6% | 79.4% | **-0.038** | 0.52 |
| with a signal | 2 | 2 | 0.0% | 100% | **-0.100** | 0.00 |

**The EARLY MOMENTUM hypothesis did worse than the universe it was drawn from**: edge
-0.062, both signals hit the lower barrier. And this is *before* fees and slippage, where
a round trip at an unchanged price already costs ~2%.

Two reasons this is not a verdict:

1. **n=2 signals.** Nothing can be concluded from two observations. This is a sample size
   at which the sign of the result is noise.
2. **The bias above.** The comparison population is itself the volatile subset.

What it *is*: proof that the pipeline produces a falsifiable number end to end, and the
first evidence pointing somewhere. Spec §12 said not to assume the hypothesis is
profitable. So far it is not.

## The §17 questions

`summarise()` answers them for any population; `bucket_by()` splits by a feature first —
which is the question that actually matters, because an aggregate cannot say whether a
hypothesis works everywhere or only in one corner, and "only in one corner" is the far
more common truth.

Two choices worth naming:

* **Expectancy is in the units of the labelling scheme.** A +30%/-20% label and a
  +50%/-25% label give different expectancies from the same market, so the fractions are
  passed in and a number without its scheme is meaningless.
* **A timeout counts at what it was actually worth**, not zero. Treating every timeout as
  break-even hides a strategy that times out consistently underwater.
* **Profit factor with no losses is None, not infinity.** A strategy that has never lost
  has not been measured, it has been lucky so far.

## Running it

```bash
python scripts/run_full_pipeline.py 300 --keep run.db   # collect
python scripts/research_report.py run.db tp20_sl10_15m --csv dataset.csv
```

The report re-labels anything provisional before reporting, so the numbers always reflect
every window that has genuinely elapsed. `--csv` writes §16's exportable dataset: token,
41 features at T0, the label, MFE/MAE, and returns at each horizon.

## What is still missing

* **A run long enough to matter.** Everything above came from five minutes. The default
  1-hour barrier needs runs measured in hours before `observations_pending` is small.
* **Walk-forward and out-of-sample splits** (§17's second half). Deliberately not built:
  they are meaningless until there is a dataset with both classes in useful numbers.
* **The four missing features.** `unique_buyers`, `unique_sellers`, `buy_volume`,
  `sell_volume` still have no free source, so the hypothesis is still the substituted one
  from `docs/FEATURES.md` rather than §12's.
