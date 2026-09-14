"""Why do some events fall into no landmark window?

s1_event_coverage reports how many failures no landmark sees. This attributes
each uncovered event to a cause, so the number can be stated precisely rather
than assumed. Runs in seconds against the tables s1 already wrote.

Usage:

    py scripts/s1b_coverage_audit.py --tables data/tables --reports reports
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

HORIZON_DAYS = 30
DELTA_WINDOW_DAYS = 30
LANDMARK_STEP_DAYS = 28


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    args = ap.parse_args()

    t = Path(args.tables)
    con = duckdb.connect()
    for name in ("spells", "landmarks"):
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{(t / (name + '.parquet')).as_posix()}')"
        )

    lo, hi = con.execute("SELECT MIN(landmark), MAX(landmark) FROM landmarks").fetchone()
    print(f"landmark grid runs {lo} to {hi}, step {LANDMARK_STEP_DAYS} days, horizon {HORIZON_DAYS} days")
    print(f"so an event is only visible if it exits between {lo} and {hi} + {HORIZON_DAYS} days\n")

    df = con.execute(
        f"""
        WITH covered AS (
            SELECT DISTINCT spell_key FROM landmarks WHERE fail_{HORIZON_DAYS}d = 1
        ), uncovered AS (
            SELECT s.*
            FROM spells s
            LEFT JOIN covered c USING (spell_key)
            WHERE s.event = 1 AND c.spell_key IS NULL
        )
        SELECT
            CASE
                WHEN exit_date < DATE '{lo}'
                    THEN '1. failed before the first landmark'
                WHEN exit_date > DATE '{hi}' + {HORIZON_DAYS}
                    THEN '2. failed after the last landmark plus horizon'
                WHEN entry_date > DATE '{hi}'
                    THEN '3. entered after the last landmark'
                WHEN DATE_DIFF('day', entry_date, exit_date) < {LANDMARK_STEP_DAYS}
                    THEN '4. whole spell fell between two landmarks'
                ELSE '5. excluded by the staleness rule'
            END AS reason,
            COUNT(*) AS events,
            CAST(MEDIAN(DATE_DIFF('day', entry_date, exit_date)) AS INTEGER) AS median_spell_days
        FROM uncovered
        GROUP BY 1 ORDER BY 1
        """
    ).df()

    reports = Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)
    df.to_csv(reports / "s1b_coverage_audit.csv", index=False)
    with pd.option_context("display.width", 200):
        print(df.to_string(index=False))

    total = con.execute("SELECT COUNT(*) FROM spells WHERE event = 1").fetchone()[0]
    print(f"\n{df['events'].sum():,} uncovered of {total:,} events "
          f"({100 * df['events'].sum() / total:.1f}%)")

    # Do the uncovered events sit in the rolling origin test quarters, where they
    # would affect reported performance, or in the training burn in, where they
    # would not?
    q = con.execute(
        f"""
        WITH covered AS (
            SELECT DISTINCT spell_key FROM landmarks WHERE fail_{HORIZON_DAYS}d = 1
        )
        SELECT
            DATE_TRUNC('quarter', s.exit_date) AS quarter,
            COUNT(*)                                                  AS events,
            SUM(CASE WHEN c.spell_key IS NULL THEN 1 ELSE 0 END)      AS uncovered,
            ROUND(100.0 * SUM(CASE WHEN c.spell_key IS NULL THEN 1 ELSE 0 END)
                  / COUNT(*), 1)                                      AS pct_uncovered
        FROM spells s LEFT JOIN covered c USING (spell_key)
        WHERE s.event = 1
        GROUP BY 1 ORDER BY 1
        """
    ).df()
    q.to_csv(reports / "s1b_coverage_by_quarter.csv", index=False)
    print("\n--- coverage by quarter ---")
    print("The last three quarters are the rolling origin test folds. Uncovered")
    print("events there affect reported performance; uncovered events in early")
    print("quarters only cost training data.")
    with pd.option_context("display.width", 200):
        print(q.to_string(index=False))

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
