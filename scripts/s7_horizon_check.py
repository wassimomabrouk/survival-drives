"""Does a 30 calendar day horizon equal 720 power on hours?

The survival time scale is power on hours, but the prediction horizon is 30
calendar days. Those coincide only if drives run 24 hours a day. For a storage
fleet they should be close, but "should be close" is not a number, and if drives
average materially less than 24 hours a day then the horizon is not what the
model claims it is and should be redefined in operating hours.

The measurement is direct. For each spell, power on hours accumulated divided by
calendar days elapsed:

    hours per calendar day = (poh_exit - poh_entry) / (exit_date - entry_date)

A fleet running continuously gives 24. Values below that mean drives are powered
down, idle in a way the counter does not accrue, or reporting the attribute in
units other than hours.

The distribution matters more than the mean. If most drives sit at 24 and a small
tail sits far lower, the horizon is fine and the tail is a caveat. If the centre
of the distribution is well below 24, the horizon needs redefining.

Usage:

    py scripts/s7_horizon_check.py --tables data/tables --reports reports
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

HORIZON_DAYS = 30
MIN_SPAN_DAYS = 60   # short spells give a noisy ratio, so require real elapsed time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    args = ap.parse_args()

    tables, reports = Path(args.tables), Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(
        f"""
        CREATE OR REPLACE VIEW s AS
        SELECT *,
            DATE_DIFF('day', entry_date, exit_date)          AS span_days,
            (poh_exit - poh_entry)                           AS poh_gained,
            (poh_exit - poh_entry)
                / NULLIF(DATE_DIFF('day', entry_date, exit_date), 0) AS hours_per_day
        FROM read_parquet('{(tables / 'spells.parquet').as_posix()}')
        WHERE poh_entry IS NOT NULL AND poh_exit IS NOT NULL
        """
    )

    overall = con.execute(
        f"""
        SELECT
            COUNT(*)                                          AS spells,
            ROUND(MEDIAN(hours_per_day), 3)                   AS median,
            ROUND(QUANTILE_CONT(hours_per_day, 0.05), 3)      AS p05,
            ROUND(QUANTILE_CONT(hours_per_day, 0.25), 3)      AS p25,
            ROUND(QUANTILE_CONT(hours_per_day, 0.75), 3)      AS p75,
            ROUND(QUANTILE_CONT(hours_per_day, 0.95), 3)      AS p95,
            ROUND(AVG(CASE WHEN hours_per_day BETWEEN 23.5 AND 24.5
                           THEN 1 ELSE 0 END), 4)             AS frac_near_24,
            ROUND(AVG(CASE WHEN hours_per_day < 20 THEN 1 ELSE 0 END), 4) AS frac_below_20,
            ROUND(AVG(CASE WHEN hours_per_day > 24.5 THEN 1 ELSE 0 END), 4) AS frac_above_24
        FROM s WHERE span_days >= {MIN_SPAN_DAYS}
        """
    ).df()
    overall.to_csv(reports / "s7_hours_per_day.csv", index=False)
    print(f"--- hours of power on time accrued per calendar day "
          f"(spells observed {MIN_SPAN_DAYS}+ days) ---")
    with pd.option_context("display.width", 220):
        print(overall.to_string(index=False))

    n_spells = int(overall["spells"].iloc[0])
    if n_spells == 0 or pd.isna(overall["median"].iloc[0]):
        print(f"\n  No spells span {MIN_SPAN_DAYS}+ days, so the ratio cannot be "
              f"measured. No verdict.")
        con.close()
        return 1
    med = float(overall["median"].iloc[0])
    implied = med * HORIZON_DAYS
    print(f"\n  median {med:.3f} hours per calendar day")
    print(f"  a {HORIZON_DAYS} day horizon therefore spans about {implied:.0f} power on hours")
    print(f"  against 720 if the fleet ran continuously: {100 * implied / 720:.1f}%")

    by_mfr = con.execute(
        f"""
        SELECT manufacturer, COUNT(*) AS spells,
            ROUND(MEDIAN(hours_per_day), 3)              AS median_hours_per_day,
            ROUND(QUANTILE_CONT(hours_per_day, 0.05), 3) AS p05,
            ROUND(QUANTILE_CONT(hours_per_day, 0.95), 3) AS p95
        FROM s WHERE span_days >= {MIN_SPAN_DAYS}
        GROUP BY 1 ORDER BY spells DESC
        """
    ).df()
    by_mfr.to_csv(reports / "s7_hours_per_day_by_manufacturer.csv", index=False)
    print("\n--- by manufacturer ---")
    print("A manufacturer far from the rest would mean the attribute is reported in")
    print("different units by that firmware, not that its drives run differently.")
    with pd.option_context("display.width", 220):
        print(by_mfr.to_string(index=False))

    print("\n--- verdict ---")
    if abs(med - 24.0) <= 0.5:
        print(f"  The fleet runs essentially continuously ({med:.2f} h/day). A 30 calendar")
        print("  day horizon and a 720 operating hour horizon are the same thing to within")
        print("  measurement error, so the calendar horizon needs no redefinition. The gap")
        print("  is stated as a limitation rather than engineered away.")
    else:
        print(f"  The fleet does NOT run continuously ({med:.2f} h/day). A 30 calendar day")
        print(f"  horizon spans about {implied:.0f} operating hours, not 720, so the horizon")
        print("  is not what the model claims and must be redefined in operating hours.")

    con.close()
    print(f"\nwritten to {reports.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
