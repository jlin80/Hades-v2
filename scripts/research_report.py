"""Answer spec §17's questions against a collected dataset, and export it.

    python scripts/research_report.py <database.db> [label_config]
    python scripts/research_report.py <database.db> [label_config] --csv out.csv

Reads a SQLite file produced by the smoke runs, or point HADES_DATABASE_URL at
Postgres and pass ``-`` to use it. Prints the §17 summary and the breakdowns
that say whether a hypothesis works everywhere or only in one corner.

No plotting, no notebooks, no ML. §17 is explicit that the simple questions come
first, and V1 built a twelve-model committee before any component had shown an
edge.
"""

from __future__ import annotations

import asyncio
import csv
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hades.config import load_settings
from hades.outcomes.labels import DEFAULT_BARRIERS
from hades.outcomes.service import OutcomeService
from hades.research.analytics import LabelledRecord, Summary, bucket_by, summarise

BUCKETS: tuple[tuple[str, list[float]], ...] = (
    ("token_age_seconds", [60.0, 120.0, 300.0]),
    ("liquidity_sol", [5.0, 15.0, 40.0]),
    ("price_movement_ratio_30s", [0.34, 0.67]),
    ("market_cap_velocity_30s", [0.0, 0.05]),
)


class FileDatabase:
    def __init__(self, url: str) -> None:
        self._engine = create_async_engine(url)
        self._maker = async_sessionmaker(self._engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as active:
            yield active

    async def dispose(self) -> None:
        await self._engine.dispose()


def render(title: str, summary: Summary) -> None:
    if summary.total == 0:
        print(f"  {title:<38} (no records)")
        return
    print(f"  {title:<38} n={summary.total:<6} resolved={summary.resolved:<6}", end="")
    if summary.resolved == 0:
        print("  (all unresolved)")
        return
    print(
        f" upper={_pct(summary.upper_rate)} lower={_pct(summary.lower_rate)}"
        f" timeout={_pct(summary.timeout_rate)}"
        f" exp={_num(summary.expectancy)} pf={_num(summary.profit_factor)}"
    )


def _pct(value: float | None) -> str:
    return " --  " if value is None else f"{value:>5.1%}"


def _num(value: float | None) -> str:
    return "  --  " if value is None else f"{value:>6.3f}"


def export_csv(records: list[LabelledRecord], path: Path) -> None:
    """Spec §16: the dataset must be exportable for later research."""
    if not records:
        print("nothing to export")
        return
    feature_names = sorted({name for r in records for name in r.features})
    columns = (
        ["token_address", "label", "mfe", "mae"]
        + [f"return_{h}" for h in ("1m", "5m", "15m", "30m", "1h")]
        + ["had_signal", *feature_names]
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for record in records:
            writer.writerow(
                [
                    record.token_address,
                    record.label.value,
                    record.mfe,
                    record.mae,
                    *[record.returns.get(h) for h in ("1m", "5m", "15m", "30m", "1h")],
                    int(record.had_signal),
                    *[record.features.get(name) for name in feature_names],
                ]
            )
    print(f"exported {len(records)} rows and {len(feature_names)} features to {path}")


def _warn_about_early_resolution_bias(
    stats: dict[str, int], finalised: int, barrier_seconds: float
) -> None:
    """The bias that would otherwise make this whole report a lie.

    A label goes final in one of two ways: a barrier is touched, or the window
    elapses. Early in a collection run only the *first* has happened — so the
    finalised set is made almost entirely of tokens that moved sharply, and the
    quiet ones are still sitting in "pending". Reading an expectancy off that
    subset is reading the expectancy of volatile tokens and calling it the
    market.

    It cannot be corrected for. It can only be waited out, so the report says so
    rather than printing a number that looks like evidence.
    """
    pending = stats.get("observations_pending", 0)
    if pending <= finalised:
        return
    share = finalised / (finalised + pending)
    print("\n" + "!" * 100)
    print("EARLY-RESOLUTION BIAS -- these numbers are not yet a measurement of the market.")
    print(
        f"  Only {finalised} of {finalised + pending} observations ({share:.1%}) have a final "
        f"label."
    )
    print(
        "  A label goes final either by touching a barrier or by its window elapsing. "
        f"The window here is {barrier_seconds / 60:.0f} minutes, so early in a run almost "
        "every"
    )
    print(
        "  finalised row is one that MOVED SHARPLY. Quiet tokens are still pending, and they "
        "are exactly the population being excluded."
    )
    print("  Wait until `observations_pending` is small before treating any of this as evidence.")
    print("!" * 100)


def _database_url(target: str) -> str:
    """Resolve the target before the event loop starts.

    Sync on purpose: filesystem work inside an async function is blocking I/O
    dressed up as concurrency, and the linter is right to flag it.
    """
    if target == "-":
        return str(load_settings().database_url)
    return f"sqlite+aiosqlite:///{Path(target).resolve()}"


async def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1

    url = _database_url(argv[0])
    config_name = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else "tp30_sl20_1h"
    csv_path = Path(argv[argv.index("--csv") + 1]) if "--csv" in argv else None

    database = FileDatabase(url)
    service = OutcomeService(database)  # type: ignore[arg-type]

    # Label anything still provisional before reporting, so the numbers reflect
    # every window that has actually elapsed.
    await service.label_pending()
    stats = await service.stats()
    records = await service.dataset(label_config=config_name)

    barrier = next((b for b in DEFAULT_BARRIERS if b.name == config_name), DEFAULT_BARRIERS[0])
    kwargs = {"upper_fraction": barrier.upper_fraction, "lower_fraction": barrier.lower_fraction}

    print("=" * 100)
    print(
        f"RESEARCH REPORT   config={config_name}   "
        f"barriers +{barrier.upper_fraction:.0%}/-{barrier.lower_fraction:.0%} "
        f"over {barrier.time_barrier_seconds / 60:.0f}m"
    )
    print("=" * 100)
    print(
        f"outcomes stored {stats['outcomes_total']}, final {stats['outcomes_final']}, "
        f"observations still pending {stats['observations_pending']}"
    )

    if not records:
        print(
            "\nNo finalised records yet. Outcomes need their window to elapse -- "
            f"{barrier.time_barrier_seconds / 60:.0f} minutes after each observation."
        )
        await database.dispose()
        return 0

    overall = summarise(records, **kwargs)  # type: ignore[arg-type]
    signalled = summarise([r for r in records if r.had_signal], **kwargs)  # type: ignore[arg-type]

    _warn_about_early_resolution_bias(stats, len(records), barrier.time_barrier_seconds)

    print("\nSPEC §17 QUESTIONS")
    render("all observations", overall)
    render("observations with a signal", signalled)
    print(f"\n  mean MFE {_num(overall.mean_mfe)}   mean MAE {_num(overall.mean_mae)}")
    print(f"  median MFE {_num(overall.median_mfe)}   median MAE {_num(overall.median_mae)}")
    print(
        "  mean return by horizon: "
        + "  ".join(f"{h}={_num(v)}" for h, v in overall.mean_return.items())
    )

    print("\nHOW RESULTS CHANGE WITH ...")
    for feature, edges in BUCKETS:
        if not any(r.feature(feature) is not None for r in records):
            continue
        print(f"\n  by {feature}")
        for bucket in bucket_by(records, feature, edges, **kwargs):
            render(bucket.name, bucket.summary)

    print("\n" + "=" * 100)
    if signalled.total == 0:
        print(
            "The strategy fired on none of these observations, so it has not been "
            "measured -- only the universe has."
        )
    elif signalled.expectancy is not None and overall.expectancy is not None:
        edge = signalled.expectancy - overall.expectancy
        print(
            f"Strategy expectancy {signalled.expectancy:+.4f} vs universe "
            f"{overall.expectancy:+.4f}  ->  edge {edge:+.4f}"
        )
        print(
            "This is BEFORE fees and slippage. See docs/PAPER_TRADING.md: a round trip "
            "at an unchanged price already loses ~2%."
        )

    if csv_path is not None:
        export_csv(records, csv_path)

    await database.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
