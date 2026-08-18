# Paper trading — Phase 6

Risk engine, position management, realistic execution, fees, slippage, TP/SL.
**Simulated fills only.** No signer, no wallet, no RPC — enforced by the AST scan in
`tests/test_safety.py`.

## Slippage is computed, not assumed

Spec §14 asks for slippage to be modelled. On most venues that means picking a
percentage and hoping. Here it does not have to: a Pump.fun token trades against a
constant-product bonding curve whose reserves we already store, so the fill is exact.

```
x · y = k          x = virtual SOL, y = virtual tokens
x' = x + sol_in    y' = k / x'    tokens_out = y − y'
```

The effective price is `sol_in / tokens_out`, strictly worse than spot `x / y`. **That
difference is the slippage** — derived from the same reserves the features come from, so
a research result can be reproduced from stored data.

This matters more than it looks. A hardcoded slippage assumption is a free parameter that
quietly decides whether a backtest shows an edge. V1 had a Slippage Manager and never
established that its numbers matched reality.

Measured properties, pinned by tests: the constant product is preserved; slippage grows
monotonically with order size; an order the size of the pool costs 100%; **a round trip at
an unchanged price always loses money** — two fees plus slippage both ways.

## The eight gates of §13, plus one

All eight: MAX TOKEN AGE, MIN LIQUIDITY, MAX SLIPPAGE, MAX POSITION SIZE, MAX OPEN
POSITIONS, MAX DAILY LOSS, MAX DRAWDOWN, STALE DATA. Every decision carries
`signal_created_at`, `decision_at` and `data_age_ms`, as §13 requires.

**The ninth is not in the spec and was required by measurement.** Phase 5's live run
produced four signals on the same token inside a minute — correct for research, since each
is a distinct T0, and ruinous for position sizing. `max_open_per_token` defaults to 1.

Two properties the engine will not compromise on:

* **Fail-closed.** Any exception inside the checks becomes a rejection, never an escape. A
  bug here must block trading, not permit it.
* **Unknown is not permission.** A check whose input is None fails. A missing liquidity
  reading is not evidence of sufficient liquidity, and treating it as such is how a risk
  engine ends up approving exactly the trades it exists to stop.

Every verdict is persisted, approved or not. §17 asks how results vary with token age,
liquidity and activity — a rejection is a data point about the strategy's *reach*, and a
log line cannot be joined against.

## Latency is modelled by a pending state

An order decided at T does not fill at T. An approved signal becomes `PENDING` with
`submit_at = decision_at + latency`, and fills against the **first snapshot at or after**
that.

Filling against a later snapshot is not look-ahead — the decision was already made from
data available at the time. Filling at the decision's own price would be the unrealistic
choice, handing the simulator a price nobody could have traded at.

Symmetrically, the risk decision reads the newest snapshot **at or before** `decision_at`,
never simply the newest. In production those are the same; in a replay they are not, and
taking the latest would gate a decision on data that did not exist when it was made.

## 🔴 The assumption that shapes every result

**Stop loss is checked before take profit.** Between two snapshots — ~12 seconds apart —
the price may have visited both levels, and we cannot see which came first. Recording the
loss assumes the worse path, so results are **understated rather than flattered**.

This is a known limitation of snapshot-based backtesting, not something this design
solves. Closing it needs per-trade data: the same gap that removes half of §12's
hypothesis.

Other ways the simulation is **optimistic**, listed so a good-looking result can be
discounted properly:

* **Other traders.** The curve state is from our snapshot; real fills would move it first.
* **Priority fees and MEV.** A real buy competes for block space. Not modelled.
* **Failed transactions.** A real order can revert and still cost a fee.
* **Peak equity** is approximated by `max(start, now)` rather than a true high-water mark,
  which *under*-reports drawdown. A real equity series is Phase 7's job.

## What a trade records (§14)

`trade_id · token · signal_id · strategy · entry_time · entry_price · position_size ·
exit_time · exit_price · gross_pnl · fees · slippage · net_pnl · exit_reason`

Exit reasons are exactly §14's: `TAKE_PROFIT`, `STOP_LOSS`, `TRAILING_STOP`, `TIMEOUT`,
`RISK_EXIT`, `MANUAL`.

**Fees and slippage are stored apart from PnL rather than folded in**, so a result reads as
"the edge before friction" and "what friction took" — which is the whole question §17
exists to answer. Both sides of the round trip pay a fee: V1's realised PnL understated
friction until its Stage 2 hardening found the buy-side fee was never captured.

The trailing stop **arms** only after a real run (default +15%). Without arming it is just
a tighter stop loss, triggering on ordinary noise right after entry.

Portfolio state is recomputed from closed trades on every pass, never held in memory: a
restart must not reset the daily loss limit, or a bad day starts over as often as the
process does.

## Not yet done

This phase built the engine and its tests. **It has not been run against live sources
end-to-end**, and no trade has been simulated on real market data — Phase 5's measured
signal rate was 2.4%, so a meaningful sample needs hours, not a 200-second smoke run.
Doing that, and reporting what it produced, belongs to Phase 7 alongside the outcome
engine.
