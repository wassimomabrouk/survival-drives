"""Section 0, stage 2: build the drive level survival table and answer the four
feasibility questions.

Run after s0_ingest.py. Reads the Parquet files, collapses the drive day panel
into one row per drive with entry and exit on the power on hours time scale, and
then runs four diagnostic blocks:

    Q1  does the table build cleanly, and what breaks
    Q2  how many failure events exist per drive model
    Q3  how much left truncation is actually present
    Q4  which SMART attributes are populated consistently

Every block prints to the console and writes a CSV into the reports folder, so the
output can be pasted or attached without rerunning anything.

Usage (PowerShell):

    py s0_diagnose.py --parquet data/parquet --reports reports --start 2024-01-01

The ingest samples one day in seven (keeping all failure rows), so exposure is
computed from the calendar span between first and last observation, not from the
number of observed rows. Pass --sample-every to match whatever the ingest used.

Notes on the survival table:

  * A drive is keyed on (serial_number, model). Serial numbers are not guaranteed
    unique on their own across manufacturers.
  * Time zero is power on hours, not calendar time. A drive enters the risk set at
    the power on hours it reported on its first observed day, which is how left
    truncation is handled. Using days in fleet instead would assume every drive
    was new on arrival and would bias the early hazard downward.
  * failed is 1 only if the drive carries the failure flag on its final observed
    day. Drives that leave the fleet without that flag are treated here as exits
    of unknown cause, which is the competing risks ambiguity to be resolved in
    the design document, not in Section 0.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

SMART_RAW_COLS = [5, 9, 12, 187, 188, 190, 193, 194, 197, 198, 241, 242]

# Boot drives and SSDs appear alongside the storage fleet. Model name patterns
# miss too many of them (WD Blue SA510, ST500LM030, TOSHIBA MQ01ABF050 are all
# boot devices with ordinary looking model strings). Capacity separates them
# cleanly: every data drive in the fleet is 4 TB or larger, every boot device is
# under 1 TB. Section 0 counts them so the exclusion is a documented decision
# rather than a silent one.
DATA_DRIVE_MIN_BYTES = 1_000_000_000_000

MANUFACTURER_CASE = """
    CASE
        WHEN model ILIKE 'ST%' OR model ILIKE 'Seagate%' THEN 'Seagate'
        WHEN model ILIKE 'WDC%' OR model ILIKE 'WUH%' OR model ILIKE 'WD%' THEN 'WDC'
        WHEN model ILIKE 'TOSHIBA%' OR model ILIKE 'MG%' OR model ILIKE 'MD%' THEN 'Toshiba'
        WHEN model ILIKE 'HGST%' OR model ILIKE 'HUH%' OR model ILIKE 'HUS%'
             OR model ILIKE 'HMS%' OR model ILIKE 'HDS%' THEN 'HGST'
        ELSE 'Other'
    END
"""


def emit(con: duckdb.DuckDBPyConnection, sql: str, name: str, reports: Path, note: str = "") -> pd.DataFrame:
    df = con.execute(sql).df()
    path = reports / f"{name}.csv"
    df.to_csv(path, index=False)
    print(f"\n--- {name} ---")
    if note:
        print(note)
    with pd.option_context("display.max_rows", 60, "display.width", 200):
        print(df.to_string(index=False) if len(df) else "(no rows)")
    print(f"[written to {path}]")
    return df


def build_tables(con: duckdb.DuckDBPyConnection, glob: str, start: str | None, end: str | None) -> None:
    where = ["date IS NOT NULL"]
    if start:
        where.append(f"date >= DATE '{start}'")
    if end:
        where.append(f"date <= DATE '{end}'")
    clause = " AND ".join(where)

    # Capacity is occasionally null or negative on an individual drive day, so the
    # boot device flag is resolved once per model from the largest capacity ever
    # reported for it, never per row. Deciding per row and then taking a MAX would
    # let one bad record flag an entire 12 TB model as a boot device.
    con.execute(
        f"""
        CREATE OR REPLACE VIEW drive_days AS
        WITH raw AS (
            SELECT * FROM read_parquet('{glob}') WHERE {clause}
        ), model_capacity AS (
            SELECT model, MAX(capacity_bytes) AS model_capacity_bytes
            FROM raw GROUP BY model
        )
        SELECT
            raw.*,
            {MANUFACTURER_CASE} AS manufacturer,
            CASE
                WHEN model_capacity_bytes IS NULL
                     OR model_capacity_bytes < {DATA_DRIVE_MIN_BYTES}
                THEN 1 ELSE 0
            END AS is_ssd
        FROM raw JOIN model_capacity USING (model)
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE drive_spans AS
        WITH ranked AS (
            SELECT
                serial_number,
                model,
                manufacturer,
                is_ssd,
                date,
                failure,
                capacity_bytes,
                smart_9_raw AS poh,
                ROW_NUMBER() OVER (PARTITION BY serial_number, model ORDER BY date ASC)  AS rn_first,
                ROW_NUMBER() OVER (PARTITION BY serial_number, model ORDER BY date DESC) AS rn_last
            FROM drive_days
        )
        SELECT
            serial_number,
            model,
            ANY_VALUE(manufacturer)                                AS manufacturer,
            MAX(is_ssd)                                            AS is_ssd,
            MAX(capacity_bytes)                                    AS capacity_bytes,
            MIN(date)                                              AS entry_date,
            MAX(date)                                              AS exit_date,
            COUNT(*)                                               AS n_obs_days,
            DATE_DIFF('day', MIN(date), MAX(date)) + 1             AS span_days,
            MAX(CASE WHEN rn_first = 1 THEN poh END)               AS poh_entry,
            MAX(CASE WHEN rn_last  = 1 THEN poh END)               AS poh_exit,
            MAX(CASE WHEN rn_last  = 1 THEN failure END)           AS failed_on_last_day,
            MAX(failure)                                           AS failure_any_day,
            SUM(failure)                                           AS failure_flag_count
        FROM ranked
        GROUP BY serial_number, model
        """
    )


def q1_integrity(con: duckdb.DuckDBPyConnection, reports: Path, sample_every: int) -> None:
    print("\n================ Q1  does the table build cleanly ================")

    emit(
        con,
        """
        SELECT
            DATE_TRUNC('quarter', date) AS quarter,
            COUNT(*)                                              AS drive_days,
            COUNT(DISTINCT serial_number || '|' || model)         AS distinct_drives,
            SUM(failure)                                          AS failure_flags,
            AVG(CASE WHEN smart_9_raw IS NULL THEN 1 ELSE 0 END)  AS poh_null_rate,
            AVG(CASE WHEN capacity_bytes IS NULL OR capacity_bytes <= 0
                     THEN 1 ELSE 0 END)                           AS bad_capacity_rate
        FROM drive_days
        GROUP BY 1 ORDER BY 1
        """,
        "q1_by_quarter",
        reports,
        "Row counts and null rates per quarter. A sudden jump or a spike in "
        "poh_null_rate means a schema change that the ingest did not handle.",
    )

    emit(
        con,
        f"""
        SELECT
            COUNT(*)                                                        AS n_drives,
            SUM(CASE WHEN n_obs_days < 0.8 * (span_days / {sample_every}.0) THEN 1 ELSE 0 END)
                                                                             AS drives_with_gaps,
            SUM(CASE WHEN poh_entry IS NULL OR poh_exit IS NULL
                     THEN 1 ELSE 0 END)                                      AS drives_missing_poh,
            SUM(CASE WHEN poh_exit < poh_entry THEN 1 ELSE 0 END)            AS poh_non_monotonic,
            SUM(CASE WHEN failure_flag_count > 1 THEN 1 ELSE 0 END)          AS multiple_failure_flags,
            SUM(CASE WHEN failure_any_day = 1 AND failed_on_last_day = 0
                     THEN 1 ELSE 0 END)                                      AS failure_not_on_last_day,
            SUM(is_ssd)                                                      AS ssd_drives
        FROM drive_spans
        """,
        "q1_span_integrity",
        reports,
        "Each nonzero count here is a case the survival table construction has to "
        "make an explicit decision about. Gaps mean a drive left and came back. "
        "Non monotonic power on hours means a counter reset or a serial reused.",
    )

    emit(
        con,
        """
        SELECT serial_number, model, COUNT(*) AS rows_for_same_day
        FROM (
            SELECT serial_number, model, date, COUNT(*) AS c
            FROM drive_days GROUP BY 1,2,3 HAVING c > 1
        )
        GROUP BY 1,2 ORDER BY rows_for_same_day DESC LIMIT 20
        """,
        "q1_duplicate_drive_days",
        reports,
        "Duplicate rows for the same drive on the same day. Should be empty. If not, "
        "the key is wrong or the source has repeated records.",
    )


def q2_events(con: duckdb.DuckDBPyConnection, reports: Path, min_events: int) -> None:
    print("\n================ Q2  how many failure events per model ================")

    emit(
        con,
        """
        SELECT
            model,
            ANY_VALUE(manufacturer)                       AS manufacturer,
            MAX(is_ssd)                                   AS is_ssd,
            ROUND(MAX(capacity_bytes) / 1e12, 1)          AS capacity_tb,
            COUNT(*)                                      AS n_drives,
            SUM(failed_on_last_day)                       AS n_failures,
            SUM(span_days)                                AS drive_days,
            ROUND(100.0 * 365.25 * SUM(failed_on_last_day)
                  / NULLIF(SUM(span_days), 0), 2)         AS afr_pct
        FROM drive_spans
        GROUP BY model
        HAVING n_drives >= 100
        ORDER BY n_failures DESC
        """,
        "q2_events_by_model",
        reports,
        "The core feasibility number. A stratified model needs enough events within "
        "each stratum, not just in total.",
    )

    emit(
        con,
        f"""
        SELECT
            SUM(n_failures)                                              AS total_failures,
            COUNT(*)                                                     AS models_with_100plus_drives,
            SUM(CASE WHEN n_failures >= 50  THEN 1 ELSE 0 END)           AS models_50plus_events,
            SUM(CASE WHEN n_failures >= {min_events} THEN 1 ELSE 0 END)  AS models_target_events,
            SUM(CASE WHEN n_failures >= 200 THEN 1 ELSE 0 END)           AS models_200plus_events
        FROM (
            SELECT model, COUNT(*) AS n_drives, SUM(failed_on_last_day) AS n_failures
            FROM drive_spans GROUP BY model HAVING n_drives >= 100
        )
        """,
        "q2_event_summary",
        reports,
        f"Rule of thumb is 10 to 20 events per candidate predictor. With roughly 12 "
        f"SMART covariates plus capacity, a stratum needs on the order of {min_events} "
        f"events to support the full model.",
    )

    emit(
        con,
        """
        SELECT
            manufacturer,
            COUNT(*)                 AS n_drives,
            SUM(failed_on_last_day)  AS n_failures,
            ROUND(100.0 * 365.25 * SUM(failed_on_last_day)
                  / NULLIF(SUM(span_days), 0), 2) AS afr_pct
        FROM drive_spans
        WHERE is_ssd = 0
        GROUP BY 1 ORDER BY n_failures DESC
        """,
        "q2_events_by_manufacturer",
        reports,
        "Fallback stratification if per model event counts turn out too thin.",
    )


def q3_truncation(con: duckdb.DuckDBPyConnection, reports: Path) -> None:
    print("\n================ Q3  how much left truncation is present ================")

    emit(
        con,
        """
        SELECT
            COUNT(*)                                                         AS n_drives,
            ROUND(AVG(CASE WHEN poh_entry > 720   THEN 1 ELSE 0 END), 4)     AS frac_entered_over_30d_used,
            ROUND(AVG(CASE WHEN poh_entry > 8760  THEN 1 ELSE 0 END), 4)     AS frac_entered_over_1y_used,
            ROUND(AVG(CASE WHEN poh_entry > 26280 THEN 1 ELSE 0 END), 4)     AS frac_entered_over_3y_used,
            CAST(MEDIAN(poh_entry) AS BIGINT)                                AS median_poh_entry,
            CAST(QUANTILE_CONT(poh_entry, 0.9) AS BIGINT)                    AS p90_poh_entry,
            CAST(MAX(poh_entry) AS BIGINT)                                   AS max_poh_entry
        FROM drive_spans
        WHERE is_ssd = 0 AND poh_entry IS NOT NULL
        """,
        "q3_truncation_overall",
        reports,
        "If most drives enter with substantial prior hours, delayed entry is not "
        "optional. If almost all enter near zero, the left truncation argument is "
        "weaker and should be scaled back in the write up rather than overstated.",
    )

    emit(
        con,
        """
        SELECT
            YEAR(entry_date)                                             AS entry_year,
            COUNT(*)                                                     AS n_drives,
            ROUND(AVG(CASE WHEN poh_entry > 720 THEN 1 ELSE 0 END), 4)   AS frac_used_on_entry,
            CAST(MEDIAN(poh_entry) AS BIGINT)                            AS median_poh_entry
        FROM drive_spans
        WHERE is_ssd = 0 AND poh_entry IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        "q3_truncation_by_entry_year",
        reports,
        "Important nuance. Drives whose entry year equals the first year of the "
        "window are truncated by the window itself, not by fleet history. Later "
        "entry years show the genuine rate of used drives arriving.",
    )


def q4_smart_coverage(con: duckdb.DuckDBPyConnection, reports: Path) -> None:
    print("\n================ Q4  SMART attribute coverage ================")

    cov = ",\n            ".join(
        f"ROUND(AVG(CASE WHEN smart_{n}_raw IS NULL THEN 0 ELSE 1 END), 3) AS smart_{n}"
        for n in SMART_RAW_COLS
    )

    emit(
        con,
        f"""
        SELECT
            manufacturer,
            COUNT(*) AS drive_days,
            {cov}
        FROM drive_days
        WHERE is_ssd = 0
        GROUP BY 1 ORDER BY drive_days DESC
        """,
        "q4_coverage_by_manufacturer",
        reports,
        "Fraction of drive days where the attribute is populated. Anything well "
        "below 1.0 for a manufacturer cannot be used as a global covariate and "
        "either gets dropped or forces a manufacturer stratified feature set.",
    )

    emit(
        con,
        f"""
        SELECT
            YEAR(date) AS year,
            COUNT(*)   AS drive_days,
            {cov}
        FROM drive_days
        WHERE is_ssd = 0
        GROUP BY 1 ORDER BY 1
        """,
        "q4_coverage_by_year",
        reports,
        "Coverage drifting over time means the usable feature set depends on the "
        "window, which constrains how far back training data can go.",
    )

    emit(
        con,
        f"""
        SELECT
            model,
            ANY_VALUE(manufacturer) AS manufacturer,
            COUNT(*)                AS drive_days,
            {cov}
        FROM drive_days
        WHERE is_ssd = 0
        GROUP BY 1
        HAVING drive_days > 100000
        ORDER BY drive_days DESC
        LIMIT 30
        """,
        "q4_coverage_by_model",
        reports,
        "Per model coverage for the largest populations, since coverage is really "
        "a firmware property and varies within a manufacturer.",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parquet", default="data/parquet", help="folder written by s0_ingest.py")
    ap.add_argument("--reports", default="reports", help="folder for the diagnostic CSVs")
    ap.add_argument("--start", default=None, help="window start, for example 2021-01-01")
    ap.add_argument("--end", default=None, help="window end, for example 2026-03-31")
    ap.add_argument("--sample-every", type=int, default=7,
                    help="day sampling rate used by the ingest, for gap detection")
    ap.add_argument("--min-events", type=int, default=130,
                    help="target failures per stratum, roughly 10 per candidate predictor")
    ap.add_argument("--memory-limit", default="8GB")
    ap.add_argument("--db", default=None, help="persist to this DuckDB file instead of memory")
    args = ap.parse_args()

    pq_dir = Path(args.parquet)
    files = sorted(pq_dir.rglob("*.parquet"))
    if not files:
        print(f"no parquet files in {pq_dir}, run s0_ingest.py first")
        return 1

    reports = Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(args.db) if args.db else duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute("PRAGMA enable_progress_bar")

    glob = (pq_dir / "**" / "*.parquet").as_posix()
    print(f"reading {len(files)} parquet files from {pq_dir}")
    if args.start or args.end:
        print(f"window: {args.start or 'min'} to {args.end or 'max'}")

    build_tables(con, glob, args.start, args.end)

    n_drives = con.execute("SELECT COUNT(*) FROM drive_spans").fetchone()[0]
    n_days = con.execute("SELECT COUNT(*) FROM drive_days").fetchone()[0]
    print(f"survival table built: {n_drives:,} drives from {n_days:,} drive days")

    q1_integrity(con, reports, args.sample_every)
    q2_events(con, reports, args.min_events)
    q3_truncation(con, reports)
    q4_smart_coverage(con, reports)

    con.close()
    print(f"\nall diagnostics written to {reports.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
