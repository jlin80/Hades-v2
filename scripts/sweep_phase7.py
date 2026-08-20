"""Sweep the Phase 6 defaults against a Phase 7 dataset.

    python scripts/sweep_phase7.py <database.db> [label_config]
    python scripts/sweep_phase7.py - tp20_sl10_15m      # use HADES_DATABASE_URL

Twenty numbers in ``hades.config`` decide what this system trades: nine risk
limits, five exit rules, six paper-trading parameters. Every one is documented as
"a plausible starting point, NOT a value derived from evidence". Phase 7 produced
the dataset that can start replacing some of them with measurements.

**Some of them.** This script's main job is to be honest about which, because a
sweep that quietly reports a best value for a parameter its data cannot see is
worse than no sweep: it launders a guess into a result.

What a static dataset *can* answer:

* **Exit take-profit and stop-loss**, from the stored MFE and MAE. Those are the
  extremes of the realised path, so whether a given barrier pair would have been
  touched is a fact about a row already collected — no re-labelling, no re-run.
* **Entry filters** — minimum liquidity, maximum token age, and the strategy's
  thresholds — because the dataset holds a feature vector for *every* evaluated
  observation, not only the ones that fired. That asymmetry is what makes the
  counterfactual available.

What it cannot, and why:

* **Portfolio limits** (max open positions, max open per token, daily loss,
  drawdown) bind on a *sequence* of trades competing for one balance. A dataset
  of independent rows has no sequence in it.
* **Position size, fee rate, latency** change the fills themselves, so answering
  them needs the simulator re-run against the snapshots, not the labels.

The same early-resolution bias that ``research_report.py`` warns about applies to
every number here, and for the same reason: it is the dataset's property, not the
report's.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

# Imported from the sibling script so the two cannot drift on how a database is
# opened or how the bias warning is worded.
from research_report import FileDatabase, _warn_about_early_resolution_bias

from hades.config import load_settings
from hades.outcomes.service import OutcomeService
from hades.research.analytics import LabelledRecord
from hades.research.sweep import BarrierResult, evaluate_barriers

TAKE_PROFITS: tuple[float, ...] = (0.10, 0.15, 0.20, 0.30, 0.50, 0.75, 1.00)
STOP_LOSSES: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20, 0.30)

# The entry filters, and the feature each one gates on.
ENTRY_FILTERS: tuple[tuple[str, str, tuple[float, ...]], ...] = (
    ("risk_min_liquidity_sol", "liquidity_sol", (0.0, 1.0, 3.0, 5.0, 10.0, 25.0)),
    ("risk_max_token_age_seconds", "token_age_seconds", (60.0, 120.0, 300.0, 600.0)),
    ("signal_min_market_cap_velocity", "market_cap_velocity_30s", (0.0, 0.05, 0.15, 0.40)),
    ("signal_min_price_movement_ratio", "price_movement_ratio_30s", (0.0, 0.25, 0.5, 0.75)),
)

# Parameters no static dataset can validate, named so their absence is a stated
# result rather than an oversight.
NOT_SWEEPABLE: tuple[tuple[str, str], ...] = (
    ("risk_max_open_positions", "binds on a sequence of trades sharing one balance"),
    ("risk_max_open_per_token", "same -- needs concurrent positions to exist"),
    ("risk_max_daily_loss_sol", "path-dependent across a day of trades"),
    ("risk_max_drawdown_fraction", "path-dependent across the equity series"),
    ("risk_max_position_sol", "changes the fill, not the label"),
    ("risk_max_slippage_fraction", "changes which orders are refused at fill time"),
    ("risk_max_data_age_seconds", "a data-freshness gate, not an outcome property"),
    ("paper_position_size_sol", "changes the fill; needs the simulator re-run"),
    ("paper_fee_rate", "applied per fill, not stored on the outcome"),
    ("paper_latency_seconds", "decides which snapshot fills; needs the snapshots"),
    ("paper_starting_balance_sol", "scales the portfolio, not the per-trade edge"),
    ("paper_pass_interval_seconds", "an operational cadence, not a strategy parameter"),
    ("paper_batch_size", "same"),
    ("exit_trailing_stop_fraction", "needs the intra-trade path, not just its extremes"),
    ("exit_trailing_arm_fraction", "same"),
    ("exit_max_hold_seconds", "fixed by the label config's own time barrier"),
)


def sweep_barriers(records: Sequence[LabelledRecord]) -> None:
    print("\n=== exit_take_profit_fraction x exit_stop_loss_fraction ===")
    print("  expectancy per observation, in return units; ties resolved as losses\n")
    print(f"  {'TP \\ SL':>9}" + "".join(f"{sl:>9.0%}" for sl in STOP_LOSSES))
    best: BarrierResult | None = None
    for take_profit in TAKE_PROFITS:
        cells = []
        for stop_loss in STOP_LOSSES:
            result = evaluate_barriers(records, take_profit=take_profit, stop_loss=stop_loss)
            cells.append("     --  " if result.expectancy is None else f"{result.expectancy:>9.4f}")
            if result.resolved and (
                best is None or (result.expectancy or 0.0) > (best.expectancy or float("-inf"))
            ):
                best = result
        print(f"  {take_profit:>9.0%}" + "".join(cells))

    if best is None:
        print("\n  No row resolved under any pair. Nothing here is a recommendation.")
        return
    print(
        f"\n  best cell: TP {best.take_profit:.0%} / SL {best.stop_loss:.0%} "
        f"-> expectancy {best.expectancy:.4f}, {best.resolved} resolved, "
        f"win rate {best.win_rate:.1%}"
    )
    print("  current defaults are TP 30% / SL 20%.")
    print(
        "  A best cell is not a validated default. With this many cells the top one is\n"
        "  partly a draw from the noise, and picking it is how a sweep overfits a run."
    )


def sweep_entry_filters(records: Sequence[LabelledRecord]) -> None:
    print("\n=== entry filters ===")
    print("  each row keeps only observations passing the threshold\n")
    for setting, feature, thresholds in ENTRY_FILTERS:
        print(f"  {setting}  (on {feature})")
        maximum = setting.endswith("_seconds")
        for threshold in thresholds:
            kept = [
                record
                for record in records
                if (value := record.feature(feature)) is not None
                and (value <= threshold if maximum else value >= threshold)
            ]
            if not kept:
                print(f"    {threshold:>10.4g}  n=0")
                continue
            result = evaluate_barriers(kept, take_profit=0.30, stop_loss=0.20)
            signalled = sum(1 for r in kept if r.had_signal)
            print(
                f"    {threshold:>10.4g}  n={len(kept):<6} signalled={signalled:<5} "
                f"resolved={result.resolved:<5} "
                f"win_rate={'  --  ' if result.win_rate is None else f'{result.win_rate:>5.1%}'} "
                f"exp={'  --  ' if result.expectancy is None else f'{result.expectancy:>7.4f}'}"
            )
        print()


def report_unsweepable() -> None:
    print("=== not validated by this sweep, and why ===")
    for setting, reason in NOT_SWEEPABLE:
        print(f"  {setting:<34} {reason}")
    print(
        "\n  These stay plausible starting points. Calling them validated because a\n"
        "  sweep ran would be exactly the substitution this project exists to avoid."
    )


async def main(database_arg: str, label_config: str) -> None:
    url = (
        str(load_settings().database_url)
        if database_arg == "-"
        else f"sqlite+aiosqlite:///{database_arg}"
    )
    database = FileDatabase(url)
    try:
        service = OutcomeService(database)  # type: ignore[arg-type]
        records = await service.dataset(label_config=label_config)
        stats = await service.stats()
    finally:
        await database.dispose()

    print(f"label_config={label_config}  records={len(records)}")
    if not records:
        print("Nothing to sweep. Collect a dataset first.")
        return

    signalled = sum(1 for r in records if r.had_signal)
    print(f"of which signalled: {signalled}")
    _warn_about_early_resolution_bias(stats, len(records), 3600.0)

    sweep_barriers(records)
    sweep_entry_filters(records)
    report_unsweepable()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "tp30_sl20_1h"))
