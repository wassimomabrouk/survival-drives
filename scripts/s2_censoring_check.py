"""Stage 2: is removal from the fleet informative censoring?

Section 0 and Stage 1 established that removals outnumber failures four to one
(39,580 against 9,790) and that removed spells are far older at entry, a median
of 65,995 power on hours against 11,203 for administratively censored spells.
Removal is therefore strongly age selected, which rules out treating it as
independent censoring without justification.

The question this script answers is narrower and decisive:

    conditional on drive model and age, does health predict removal?

If it does not, censoring is conditionally independent given covariates the
models already adjust for, and the primary analysis stands. If it does,
operators are pulling drives that look sick, censoring is informative, and every
survival estimate is biased in a direction this data cannot reveal.

Two independent lines of evidence, since neither settles it alone.

  A. Burstiness. Wholesale retirement of an ageing model concentrates removals
     into a few weeks. Culling individual unhealthy drives spreads them evenly.
     Concentration is evidence for migration, which is benign.

  B. Health at removal. At each landmark, compare SMART health between spells
     removed within the next 30 days and spells that are not, within strata of
     model and age. Excess prevalence of non-zero sector counts among the
     removed group is evidence for selection on health, which is not benign.

Block B deliberately excludes spells that fail inside the horizon, so the
comparison is removal against survival, not removal against failure.

This runs before any survival model is fit, per DESIGN.md section 3.

Usage:

    py scripts/s2_censoring_check.py --tables data/tables --reports reports
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

# Health attributes available on every manufacturer. Deliberately not the
# Seagate only ones: the censoring mechanism has to be assessed fleet wide.
HEALTH_COLS = [5, 197, 198]

HOURS_PER_YEAR = 8760

# A model and age cell needs both arms populated before its difference means
# anything.
MIN_CELL_REMOVED = 30
MIN_CELL_CONTROL = 100


def emit(con, sql: str, name: str, reports: Path, note: str = "") -> pd.DataFrame:
    df = con.execute(sql).df()
    reports.mkdir(parents=True, exist_ok=True)
    df.to_csv(reports / f"{name}.csv", index=False)
    print(f"\n--- {name} ---")
    if note:
        print(note)
    with pd.option_context("display.max_rows", 40, "display.width", 220):
        print(df.to_string(index=False) if len(df) else "(no rows)")
    return df


def block_a_burstiness(con, reports: Path) -> None:
    print("\n================ A. are removals bursty ================")

    emit(
        con,
        """
        WITH by_month AS (
            SELECT model, DATE_TRUNC('month', exit_date) AS month, COUNT(*) AS n
            FROM spells WHERE exit_type = 'removed'
            GROUP BY 1, 2
        ), totals AS (
            SELECT model, SUM(n) AS removed_total, COUNT(*) AS months_active
            FROM by_month GROUP BY 1
        )
        SELECT
            t.model,
            t.removed_total,
            t.months_active,
            MAX(m.n)                                              AS biggest_month,
            ROUND(MAX(m.n) * 1.0 / t.removed_total, 3)            AS share_in_biggest_month,
            ROUND(SUM(POWER(m.n * 1.0 / t.removed_total, 2)), 3)  AS concentration_index
        FROM by_month m JOIN totals t USING (model)
        WHERE t.removed_total >= 200
        GROUP BY t.model, t.removed_total, t.months_active
        ORDER BY t.removed_total DESC
        """,
        "s2_removal_burstiness",
        reports,
        "concentration_index is the sum of squared monthly shares, so 1.0 means every\n"
        "removal happened in a single month and 0.04 means they are spread evenly over\n"
        "26 months. Values above roughly 0.3 indicate wholesale retirement rather than\n"
        "steady culling.",
    )

    emit(
        con,
        """
        SELECT
            DATE_TRUNC('quarter', exit_date) AS quarter,
            COUNT(*)                          AS removals,
            COUNT(DISTINCT model)             AS models_affected,
            ROUND(MAX(cnt) * 1.0 / COUNT(*), 3) AS largest_model_share
        FROM (
            SELECT exit_date, model,
                   COUNT(*) OVER (PARTITION BY DATE_TRUNC('quarter', exit_date), model) AS cnt
            FROM spells WHERE exit_type = 'removed'
        )
        GROUP BY 1 ORDER BY 1
        """,
        "s2_removals_by_quarter",
        reports,
        "If one model dominates each quarter's removals, the fleet is being migrated\n"
        "model by model, which is the benign explanation.",
    )


def block_b_health(con, reports: Path) -> None:
    print("\n================ B. does health predict removal ================")

    health_rates = ",\n            ".join(
        f"AVG(CASE WHEN smart_{n} > 0 THEN 1.0 ELSE 0.0 END) AS nonzero_{n}"
        for n in HEALTH_COLS
    )

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE cells AS
        SELECT
            model,
            CAST(poh_at_landmark / {HOURS_PER_YEAR} AS INTEGER) AS age_years,
            removed_30d,
            COUNT(*) AS n,
            {health_rates}
        FROM landmarks
        WHERE fail_30d = 0          -- removal against survival, not against failure
          AND poh_at_landmark IS NOT NULL
        GROUP BY 1, 2, 3
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE paired AS
        SELECT
            r.model,
            r.age_years,
            r.n AS n_removed,
            c.n AS n_control,
            {", ".join(f"r.nonzero_{n} AS rem_{n}, c.nonzero_{n} AS ctl_{n}" for n in HEALTH_COLS)}
        FROM cells r
        JOIN cells c
          ON r.model = c.model AND r.age_years = c.age_years
         AND r.removed_30d = 1 AND c.removed_30d = 0
        WHERE r.n >= {MIN_CELL_REMOVED} AND c.n >= {MIN_CELL_CONTROL}
        """
    )

    n_cells, n_rem = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(n_removed), 0) FROM paired"
    ).fetchone()
    print(f"\n{n_cells} model and age cells usable, covering {n_rem:,} pre-removal landmark rows")

    if n_cells == 0:
        print("no cell has both arms populated; the comparison cannot be made as specified")
        return

    diffs = ",\n            ".join(
        f"ROUND(SUM(n_removed * (rem_{n} - ctl_{n})) / SUM(n_removed), 4) AS excess_nonzero_{n}"
        for n in HEALTH_COLS
    )
    ratios = ",\n            ".join(
        f"ROUND(SUM(n_removed * rem_{n}) / NULLIF(SUM(n_removed * ctl_{n}), 0), 2) AS ratio_{n}"
        for n in HEALTH_COLS
    )

    emit(
        con,
        f"""
        SELECT
            COUNT(*)          AS cells,
            SUM(n_removed)    AS removed_rows,
            SUM(n_control)    AS control_rows,
            {diffs},
            {ratios}
        FROM paired
        """,
        "s2_health_excess_pooled",
        reports,
        "excess_nonzero_N is the share of soon to be removed drives with a non-zero\n"
        "value on attribute N, minus the same share among matched surviving drives,\n"
        "averaged over model and age cells and weighted by removal volume.\n"
        "ratio_N is the same comparison as a multiple.\n\n"
        "Attribute 5 is reallocated sectors, 197 current pending sectors, 198 offline\n"
        "uncorrectable sectors. Near zero excess means removal does not select on\n"
        "health once age and model are held fixed, and censoring is conditionally\n"
        "independent. A large positive excess means it does, and the competing risks\n"
        "treatment becomes the primary analysis rather than a sensitivity check.",
    )

    emit(
        con,
        f"""
        SELECT
            model, age_years, n_removed, n_control,
            {", ".join(f"ROUND(rem_{n} - ctl_{n}, 4) AS excess_{n}" for n in HEALTH_COLS)}
        FROM paired
        ORDER BY n_removed DESC
        LIMIT 25
        """,
        "s2_health_excess_by_cell",
        reports,
        "Per cell view. A pooled average can hide a handful of cells behaving very\n"
        "differently from the rest, so the largest cells are listed individually.",
    )

    emit(
        con,
        f"""
        SELECT
            CASE WHEN removed_30d = 1 THEN 'removed within 30d'
                 ELSE 'survived' END AS arm,
            SUM(n)                   AS landmark_rows,
            {", ".join(f"ROUND(SUM(n * nonzero_{n}) / SUM(n), 4) AS nonzero_{n}" for n in HEALTH_COLS)}
        FROM cells GROUP BY 1 ORDER BY 1
        """,
        "s2_health_unmatched",
        reports,
        "Unmatched comparison, shown for contrast only. Any gap here mixes the age\n"
        "and model effect with the health effect and must not be read as evidence.\n"
        "The matched table above is the one that answers the question.",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    tables = Path(args.tables)
    for name in ("spells", "landmarks"):
        if not (tables / f"{name}.parquet").exists():
            print(f"{name}.parquet not found in {tables}, run s1_build_tables.py first")
            return 1

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute("PRAGMA enable_progress_bar")
    for name in ("spells", "landmarks"):
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{(tables / (name + '.parquet')).as_posix()}')"
        )

    block_a_burstiness(con, Path(args.reports))
    block_b_health(con, Path(args.reports))

    con.close()
    print(f"\ndiagnostics written to {Path(args.reports).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
