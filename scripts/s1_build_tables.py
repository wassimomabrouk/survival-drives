"""Stage 1: build the survival tables from the ingested drive day panel.

Produces two Parquet outputs that every later modelling step reads, so the raw
panel is never touched again:

  spells.parquet     one row per drive spell, on the power on hours time scale
  landmarks.parquet  one row per spell per monthly landmark, with covariates as
                     of that landmark and the 30 day outcome

Both follow DESIGN.md sections 3 and 5. The rules implemented here are locked:

  * Time scale is power on hours (smart_9_raw), not calendar days in fleet. A
    spell enters the risk set at the hours it reported on its first observed day,
    which is how left truncation is handled.
  * A (serial_number, model) pair is split into separate spells at a decrease in
    power on hours (counter reset or reused serial) or at any observation after a
    failure flag. Observation gaps are deliberately NOT a splitting condition:
    offline time accrues no power on hours, so it produces no gap in analysis
    time, and splitting there would manufacture a spurious truncated entry for a
    physically continuous drive.
  * A spell is an event if it carries the failure flag on its final observed day.
  * At each landmark a spell is scorable only if its most recent observation
    falls within the staleness window. This is where observation gaps are
    handled.

The script finishes with a reconciliation block. The event count after spell
splitting must tie back to Section 0, and the landmark outcome count must tie
back to the spell event count. If they do not, the splitting logic is wrong and
nothing downstream is trustworthy.

Usage (PowerShell or cmd):

    py scripts/s1_build_tables.py --parquet data/parquet --out data/tables ^
        --start 2024-01-01 --reports reports
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

SMART_RAW_COLS = [5, 9, 12, 187, 188, 190, 193, 194, 197, 198, 241, 242]

# Attributes that get a 30 day change feature. Power on hours is excluded since
# it is the time scale, and temperature is a level not an accumulator.
DELTA_COLS = [5, 12, 187, 188, 193, 197, 198, 241, 242]

DATA_DRIVE_MIN_BYTES = 1_000_000_000_000

# A spell whose last observation falls within this many days of the end of the
# data is administratively censored rather than removed from the fleet. Weekly
# sampling means the final observation can trail the true window end.
ADMIN_CENSOR_SLACK_DAYS = 14

HORIZON_DAYS = 30
STALENESS_DAYS = 14
DELTA_WINDOW_DAYS = 30

# Landmarks are spaced 28 days apart rather than monthly. Calendar months run 28
# to 31 days, so a monthly grid against a 30 day horizon leaves uncovered days in
# every 31 day month, and 4.7% of failures fell into no landmark window at all.
# A fixed 28 day step covers the timeline completely with a two day overlap
# between consecutive windows. The overlap is harmless: landmark observations
# within a spell are already correlated, and the bootstrap resamples at spell
# level rather than row level.
LANDMARK_STEP_DAYS = 28

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


def emit(con, sql: str, name: str, reports: Path, note: str = "") -> pd.DataFrame:
    df = con.execute(sql).df()
    reports.mkdir(parents=True, exist_ok=True)
    df.to_csv(reports / f"{name}.csv", index=False)
    print(f"\n--- {name} ---")
    if note:
        print(note)
    with pd.option_context("display.max_rows", 60, "display.width", 200):
        print(df.to_string(index=False) if len(df) else "(no rows)")
    return df


def build_panel(con, glob: str, start: str | None, end: str | None) -> None:
    """Data drives only, with manufacturer resolved and boot devices dropped."""
    where = ["date IS NOT NULL"]
    if start:
        where.append(f"date >= DATE '{start}'")
    if end:
        where.append(f"date <= DATE '{end}'")
    clause = " AND ".join(where)

    con.execute(
        f"""
        CREATE OR REPLACE TABLE panel AS
        WITH raw AS (
            SELECT * FROM read_parquet('{glob}') WHERE {clause}
        ), model_capacity AS (
            SELECT model, MAX(capacity_bytes) AS model_capacity_bytes
            FROM raw GROUP BY model
        )
        SELECT
            raw.date,
            raw.serial_number,
            raw.model,
            {MANUFACTURER_CASE} AS manufacturer,
            model_capacity_bytes AS capacity_bytes,
            raw.failure,
            {", ".join(f"raw.smart_{n}_raw" for n in SMART_RAW_COLS)}
        FROM raw JOIN model_capacity USING (model)
        WHERE model_capacity_bytes >= {DATA_DRIVE_MIN_BYTES}
          AND raw.smart_9_raw IS NOT NULL
        """
    )


def build_spells(con) -> None:
    """Split (serial, model) into spells, then collapse to one row per spell."""
    con.execute(
        """
        CREATE OR REPLACE TABLE panel_spelled AS
        WITH ordered AS (
            SELECT
                *,
                LAG(smart_9_raw) OVER w AS prev_poh,
                LAG(failure)     OVER w AS prev_failure
            FROM panel
            WINDOW w AS (PARTITION BY serial_number, model ORDER BY date)
        ), flagged AS (
            SELECT
                *,
                CASE
                    WHEN prev_poh IS NULL          THEN 1  -- first observation
                    WHEN smart_9_raw < prev_poh    THEN 1  -- counter reset or reused serial
                    WHEN prev_failure = 1          THEN 1  -- observation after a failure
                    ELSE 0
                END AS is_spell_start
            FROM ordered
        )
        SELECT
            * EXCLUDE (prev_poh, prev_failure, is_spell_start),
            serial_number || '|' || model || '|' ||
                CAST(SUM(is_spell_start) OVER (
                    PARTITION BY serial_number, model
                    ORDER BY date
                    ROWS UNBOUNDED PRECEDING
                ) AS VARCHAR) AS spell_key
        FROM flagged
        """
    )

    max_date = con.execute("SELECT MAX(date) FROM panel_spelled").fetchone()[0]
    print(f"data ends {max_date}")

    con.execute(
        f"""
        CREATE OR REPLACE TABLE spells AS
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY spell_key ORDER BY date ASC)  AS rn_first,
                ROW_NUMBER() OVER (PARTITION BY spell_key ORDER BY date DESC) AS rn_last
            FROM panel_spelled
        )
        SELECT
            spell_key,
            ANY_VALUE(serial_number)                            AS serial_number,
            ANY_VALUE(model)                                    AS model,
            ANY_VALUE(manufacturer)                             AS manufacturer,
            ANY_VALUE(capacity_bytes)                           AS capacity_bytes,
            MIN(date)                                           AS entry_date,
            MAX(date)                                           AS exit_date,
            COUNT(*)                                            AS n_obs,
            MAX(CASE WHEN rn_first = 1 THEN smart_9_raw END)    AS poh_entry,
            MAX(CASE WHEN rn_last  = 1 THEN smart_9_raw END)    AS poh_exit,
            MAX(CASE WHEN rn_last  = 1 THEN failure END)        AS event,
            CASE
                WHEN MAX(CASE WHEN rn_last = 1 THEN failure END) = 1 THEN 'failure'
                WHEN MAX(date) >= DATE '{max_date}' - {ADMIN_CENSOR_SLACK_DAYS}
                    THEN 'admin_censored'
                ELSE 'removed'
            END                                                 AS exit_type
        FROM ranked
        GROUP BY spell_key
        """
    )


def build_landmarks(con, start: str) -> None:
    """One row per spell per monthly landmark, covariates as of the landmark."""
    max_date = con.execute("SELECT MAX(date) FROM panel_spelled").fetchone()[0]

    # First landmark leaves room for the 30 day change features; last landmark
    # leaves room for the 30 day outcome window.
    # The grid is anchored to its END, not its start. Stepping forward from the
    # first landmark leaves a remainder at the far end of the window, which is
    # precisely where the rolling origin test folds sit: forward anchoring put the
    # last landmark at 2026-02-25 and left 14.9% of 2026 Q1 events visible to no
    # landmark. Anchoring backwards from (max_date - horizon) puts the last
    # landmark as late as the horizon allows and moves the remainder into the
    # 2024 burn in, where it costs training data only.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE landmark_dates AS
        WITH bounds AS (
            SELECT
                DATE '{start}' + {DELTA_WINDOW_DAYS} AS lo,
                DATE '{max_date}' - {HORIZON_DAYS}   AS hi
        )
        SELECT UNNEST(generate_series(
            hi - CAST(DATE_DIFF('day', lo, hi) / {LANDMARK_STEP_DAYS} AS INTEGER)
                 * INTERVAL {LANDMARK_STEP_DAYS} DAY,
            hi,
            INTERVAL {LANDMARK_STEP_DAYS} DAY
        ))::DATE AS landmark
        FROM bounds
        """
    )
    n_lm = con.execute("SELECT COUNT(*) FROM landmark_dates").fetchone()[0]
    lo, hi = con.execute("SELECT MIN(landmark), MAX(landmark) FROM landmark_dates").fetchone()
    print(f"{n_lm} landmarks at {LANDMARK_STEP_DAYS} day spacing, {lo} to {hi}")

    cur = ", ".join(f"cur.smart_{n}_raw AS smart_{n}" for n in SMART_RAW_COLS)
    # current values are already projected as smart_<n> by with_current, so the
    # delta references the CTE alias, not the source table
    delta = ", ".join(
        f"wc.smart_{n} - prv.smart_{n}_raw AS d30_smart_{n}" for n in DELTA_COLS
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE landmarks AS
        WITH at_risk AS (
            SELECT s.*, l.landmark
            FROM spells s CROSS JOIN landmark_dates l
            WHERE l.landmark >= s.entry_date
              AND l.landmark <= s.exit_date
        ), with_current AS (
            SELECT
                ar.*,
                cur.date AS obs_date,
                cur.smart_9_raw AS poh_at_landmark,
                {cur}
            FROM at_risk ar
            ASOF JOIN panel_spelled cur
              ON ar.spell_key = cur.spell_key
             AND ar.landmark >= cur.date
        ), with_delta AS (
            SELECT
                wc.*,
                {delta}
            FROM with_current wc
            ASOF LEFT JOIN panel_spelled prv
              ON wc.spell_key = prv.spell_key
             AND wc.landmark - {DELTA_WINDOW_DAYS} >= prv.date
        )
        SELECT
            spell_key, model, manufacturer, capacity_bytes,
            landmark, obs_date, entry_date, exit_date, exit_type,
            poh_entry, poh_at_landmark,
            DATE_DIFF('day', obs_date, landmark) AS obs_age_days,
            -- time from the landmark to whichever comes first: exit or horizon
            LEAST(DATE_DIFF('day', landmark, exit_date), {HORIZON_DAYS}) AS t_days,
            -- did this spell fail inside the horizon
            CASE
                WHEN event = 1
                 AND DATE_DIFF('day', landmark, exit_date) <= {HORIZON_DAYS}
                THEN 1 ELSE 0
            END AS fail_{HORIZON_DAYS}d,
            -- status at t_days: 1 failure, 0 censored or still alive
            CASE
                WHEN event = 1
                 AND DATE_DIFF('day', landmark, exit_date) <= {HORIZON_DAYS}
                THEN 1 ELSE 0
            END AS status,
            -- removals inside the horizon are the competing event, flagged so the
            -- Fine-Gray sensitivity analysis in DESIGN.md section 3 can use them
            CASE
                WHEN exit_type = 'removed'
                 AND DATE_DIFF('day', landmark, exit_date) <= {HORIZON_DAYS}
                THEN 1 ELSE 0
            END AS removed_{HORIZON_DAYS}d,
            * EXCLUDE (spell_key, model, manufacturer, capacity_bytes, landmark,
                       obs_date, entry_date, exit_date, exit_type, poh_entry,
                       poh_at_landmark, serial_number, n_obs, poh_exit, event)
        FROM with_delta
        WHERE DATE_DIFF('day', obs_date, landmark) <= {STALENESS_DAYS}
        """
    )


def reconcile(con, reports: Path) -> None:
    print("\n================ reconciliation ================")

    emit(
        con,
        """
        SELECT
            (SELECT COUNT(DISTINCT serial_number || '|' || model) FROM panel) AS drives_in,
            (SELECT COUNT(*) FROM spells)                                     AS spells_out,
            (SELECT COUNT(*) FROM spells WHERE event = 1)                     AS events_after_split,
            (SELECT SUM(failure) FROM panel)                                  AS failure_flags_in_panel
        """,
        "s1_reconcile_counts",
        reports,
        "events_after_split must match failure_flags_in_panel. A shortfall means "
        "a failure flag landed on a non final day of its spell, which the "
        "splitting rules should have prevented.",
    )

    emit(
        con,
        """
        SELECT
            exit_type,
            COUNT(*)                                  AS spells,
            ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct,
            CAST(MEDIAN(poh_entry) AS BIGINT)         AS median_poh_entry,
            CAST(MEDIAN(poh_exit - poh_entry) AS BIGINT) AS median_poh_observed
        FROM spells GROUP BY 1 ORDER BY spells DESC
        """,
        "s1_exit_types",
        reports,
        "The share of spells exiting as 'removed' sets how much the competing "
        "risks ambiguity can matter. A large share makes the Fine-Gray "
        "sensitivity analysis load bearing rather than decorative.",
    )

    emit(
        con,
        """
        SELECT
            COUNT(*)                                      AS spells,
            SUM(CASE WHEN n_obs = 1 THEN 1 ELSE 0 END)    AS single_observation_spells,
            SUM(CASE WHEN poh_exit < poh_entry THEN 1 ELSE 0 END) AS still_non_monotonic,
            SUM(CASE WHEN poh_entry > 720 THEN 1 ELSE 0 END)      AS truncated_entries,
            ROUND(AVG(CASE WHEN poh_entry > 720 THEN 1 ELSE 0 END), 4) AS frac_truncated
        FROM spells
        """,
        "s1_spell_quality",
        reports,
        "still_non_monotonic must be zero: the splitting rule exists precisely to "
        "eliminate it. Single observation spells carry no covariate history and "
        "are unusable at any landmark.",
    )

    emit(
        con,
        f"""
        SELECT
            COUNT(*)                             AS landmark_rows,
            COUNT(DISTINCT spell_key)            AS distinct_spells,
            SUM(fail_{HORIZON_DAYS}d)            AS events_in_horizon,
            SUM(removed_{HORIZON_DAYS}d)         AS removals_in_horizon,
            ROUND(1000.0 * SUM(fail_{HORIZON_DAYS}d) / COUNT(*), 3) AS events_per_1000_rows,
            ROUND(AVG(obs_age_days), 2)          AS mean_obs_age_days
        FROM landmarks
        """,
        "s1_landmark_summary",
        reports,
        "events_in_horizon counts a failure once per landmark that sees it, so it "
        "exceeds the spell event count. Roughly four landmarks precede any given "
        "failure within 30 days is not expected; one is, since landmarks are "
        "monthly and the horizon is 30 days.",
    )

    emit(
        con,
        f"""
        SELECT
            DATE_TRUNC('quarter', landmark) AS quarter,
            COUNT(*)                        AS landmark_rows,
            SUM(fail_{HORIZON_DAYS}d)       AS events,
            ROUND(1000.0 * SUM(fail_{HORIZON_DAYS}d) / COUNT(*), 3) AS events_per_1000
        FROM landmarks GROUP BY 1 ORDER BY 1
        """,
        "s1_landmark_by_quarter",
        reports,
        "Event rate per landmark should be broadly stable. The three rolling "
        "origin test quarters are the last three rows.",
    )

    emit(
        con,
        f"""
        SELECT
            (SELECT COUNT(*) FROM spells WHERE event = 1)                  AS events_total,
            (SELECT COUNT(DISTINCT spell_key) FROM landmarks
             WHERE fail_{HORIZON_DAYS}d = 1)                               AS events_covered,
            (SELECT COUNT(*) FROM spells WHERE event = 1)
              - (SELECT COUNT(DISTINCT spell_key) FROM landmarks
                 WHERE fail_{HORIZON_DAYS}d = 1)                           AS events_uncovered
        """,
        "s1_event_coverage",
        reports,
        "An event is covered if at least one landmark sees it inside the horizon. "
        "With a 28 day landmark step and a 30 day horizon the grid covers the "
        "timeline completely, so events_uncovered should be zero or near it. Any "
        "remainder is a spell whose entire life sat between the first landmark and "
        "its own entry date.",
    )

    emit(
        con,
        """
        SELECT
            manufacturer,
            COUNT(*)                                       AS spells,
            SUM(event)                                     AS events,
            SUM(CASE WHEN exit_type = 'removed' THEN 1 ELSE 0 END) AS removed
        FROM spells GROUP BY 1 ORDER BY events DESC
        """,
        "s1_spells_by_manufacturer",
        reports,
        "Compare with reports/q2_events_by_manufacturer.csv from Section 0. Small "
        "differences come from boot drives and spell splitting.",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parquet", default="data/parquet")
    ap.add_argument("--out", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--db", default="data/work.duckdb",
                    help="on disk database so large intermediates can spill")
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    pq = Path(args.parquet)
    if not list(pq.rglob("*.parquet")):
        print(f"no parquet under {pq}, run s0_ingest.py first")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(args.db)
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute("PRAGMA enable_progress_bar")

    glob = (pq / "**" / "*.parquet").as_posix()

    print("building panel")
    build_panel(con, glob, args.start, args.end)
    n = con.execute("SELECT COUNT(*) FROM panel").fetchone()[0]
    print(f"  {n:,} drive day rows on data drives with usable power on hours")

    print("splitting spells")
    build_spells(con)

    print("building landmarks")
    build_landmarks(con, args.start)

    for name in ("spells", "landmarks"):
        path = (out / f"{name}.parquet").as_posix()
        con.execute(f"COPY {name} TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        size = Path(path).stat().st_size / 1e6
        print(f"wrote {path} ({size:.1f} MB)")

    reconcile(con, Path(args.reports))
    con.close()

    print(f"\ntables in {out.resolve()}, diagnostics in {Path(args.reports).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
