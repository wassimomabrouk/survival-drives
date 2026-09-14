"""B0: nonparametric baseline with delayed entry.

The first model in the ladder, and the reference every later model is scored
against. Also the last structural check on the survival tables: reconciliation
counts confirm the right number of events exist, but only a hazard curve reveals
whether entry and exit are on the right time scale. A fleet of mechanical drives
should show a hazard that varies with age. A flat hazard would mean the entry and
exit logic is wrong in a way counting could not detect.

Everything here is computed from first principles rather than from a survival
library, because the delayed entry handling is the point and should be auditable
in the repository rather than hidden behind a library call.

Risk set convention: a spell is at risk at time t if

    poh_entry < t <= poh_exit

so a spell entering exactly at t is not yet at risk, and one exiting at t still
is. Time is power on hours throughout, never calendar days.

Outputs, per DESIGN.md section 7:

  1. Kaplan-Meier survival with delayed entry, overall and by drive model
  2. Nelson-Aalen cumulative hazard
  3. Hazard rate by age band, computed independently as events over exposure.
     This is a different estimator from the same data, so agreement with the
     Nelson-Aalen increments is a genuine check rather than a restatement.
  4. The B0 prediction itself: P(failure within 30 days | alive at age a)
  5. E5 validation: the same curves refit on the incident cohort, meaning drives
     that entered the fleet new during the window and carry almost no
     truncation. If delayed entry is handled correctly the two agree.

Usage:

    py scripts/s3_b0_baseline.py --tables data/tables --reports reports --figures figures
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HOURS_PER_YEAR = 8760
HORIZON_HOURS = 30 * 24  # the 30 day prediction horizon, in power on hours

# Age grid for the hazard and conditional risk tables, in hours. 4380 is half a
# year, which is fine enough to show shape without shattering into noise.
BAND_HOURS = 4380
MAX_AGE_HOURS = 100_000

N_MODELS_PLOTTED = 6

# A spell counts as incident if it entered the fleet effectively new. 720 hours
# is 30 days of prior use, the same threshold Section 0 used to call a drive
# truncated.
INCIDENT_MAX_ENTRY_HOURS = 720


def load(con, tables: Path) -> None:
    con.execute(
        f"CREATE OR REPLACE VIEW spells AS "
        f"SELECT * FROM read_parquet('{(tables / 'spells.parquet').as_posix()}')"
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW cohort AS
        SELECT
            spell_key, model, manufacturer, capacity_bytes,
            entry_date, exit_date, exit_type, event,
            poh_entry, poh_exit
        FROM spells
        WHERE poh_entry IS NOT NULL
          AND poh_exit  IS NOT NULL
          AND poh_exit > poh_entry
        """
    )


def km_delayed_entry(con, where: str = "TRUE") -> pd.DataFrame:
    """Kaplan-Meier and Nelson-Aalen on the power on hours scale, with delayed entry.

    The risk set is built as a counting process: cumulative entries strictly
    before t, minus cumulative exits strictly before t. That is what makes this a
    left truncated estimator rather than an ordinary one.
    """
    df = con.execute(
        f"""
        WITH c AS (SELECT * FROM cohort WHERE {where}),
        pts AS (
            SELECT poh_entry AS t, 1 AS n_enter, 0 AS n_exit, 0 AS n_event FROM c
            UNION ALL
            SELECT poh_exit, 0, 1, CASE WHEN event = 1 THEN 1 ELSE 0 END FROM c
        ),
        agg AS (
            SELECT t, SUM(n_enter) AS n_enter, SUM(n_exit) AS n_exit, SUM(n_event) AS d
            FROM pts GROUP BY t
        ),
        cum AS (
            SELECT
                t, n_enter, n_exit, d,
                SUM(n_enter) OVER (ORDER BY t) AS cum_enter,
                SUM(n_exit)  OVER (ORDER BY t) AS cum_exit
            FROM agg
        )
        SELECT
            t,
            d,
            -- entered strictly before t, minus exited strictly before t
            (cum_enter - n_enter) - (cum_exit - n_exit) AS n_at_risk
        FROM cum
        WHERE d > 0
        ORDER BY t
        """
    ).df()

    if df.empty:
        return df

    # Guard against a risk set that the convention leaves empty or smaller than
    # the events it must support, which would otherwise produce a survival of
    # exactly zero and poison the cumulative product.
    bad = df["n_at_risk"] < df["d"]
    if bad.any():
        print(f"    warning: {bad.sum()} event times with risk set below event count, dropped")
        df = df.loc[~bad].copy()

    df = df.loc[df["n_at_risk"] > 0].copy()
    df["hazard_increment"] = df["d"] / df["n_at_risk"]
    df["survival"] = np.cumprod(1.0 - df["hazard_increment"])
    df["cum_hazard"] = np.cumsum(df["hazard_increment"])
    # Greenwood variance, for the confidence band
    gw = np.cumsum(df["d"] / (df["n_at_risk"] * (df["n_at_risk"] - df["d"]).clip(lower=1)))
    se = df["survival"] * np.sqrt(gw)
    df["survival_lo"] = (df["survival"] - 1.96 * se).clip(lower=0)
    df["survival_hi"] = (df["survival"] + 1.96 * se).clip(upper=1)
    df["age_years"] = df["t"] / HOURS_PER_YEAR
    return df


def hazard_by_band(con, where: str = "TRUE") -> pd.DataFrame:
    """Events divided by exposure in each age band.

    A different estimator from the same data. Agreement with the Nelson-Aalen
    increments is evidence the risk set construction is right; disagreement would
    mean one of the two is wrong.
    """
    return con.execute(
        f"""
        WITH c AS (SELECT * FROM cohort WHERE {where}),
        bands AS (
            SELECT
                UNNEST(generate_series(0, {MAX_AGE_HOURS}, {BAND_HOURS})) AS band_lo
        )
        SELECT
            b.band_lo,
            b.band_lo + {BAND_HOURS}                                  AS band_hi,
            (b.band_lo + {BAND_HOURS} / 2.0) / {HOURS_PER_YEAR}       AS age_years,
            SUM(GREATEST(0, LEAST(c.poh_exit, b.band_lo + {BAND_HOURS})
                            - GREATEST(c.poh_entry, b.band_lo)))      AS exposure_hours,
            SUM(CASE WHEN c.event = 1
                      AND c.poh_exit >= b.band_lo
                      AND c.poh_exit <  b.band_lo + {BAND_HOURS}
                     THEN 1 ELSE 0 END)                               AS events
        FROM bands b JOIN c
          ON c.poh_exit > b.band_lo AND c.poh_entry < b.band_lo + {BAND_HOURS}
        GROUP BY 1, 2, 3
        HAVING exposure_hours > 0
        ORDER BY 1
        """
    ).df().assign(
        hazard_per_hour=lambda d: d["events"] / d["exposure_hours"],
        afr_pct=lambda d: 100 * (1 - np.exp(-(d["events"] / d["exposure_hours"]) * HOURS_PER_YEAR)),
    )


MIN_RISK_SET = 500


def conditional_risk(km: pd.DataFrame, horizon_hours: int = HORIZON_HOURS) -> pd.DataFrame:
    """The B0 prediction: P(fail within the horizon | alive at age a).

    Read off the survival curve as 1 - S(a + horizon) / S(a). This is what later
    models must beat, so it is written out as a lookup table rather than only
    plotted.

    The table stops where the risk set falls below MIN_RISK_SET. Past that point
    the curve is flat for want of events rather than for want of risk, and
    interpolation would report a confident zero where the honest answer is that
    the data does not support an estimate.
    """
    if km.empty:
        return pd.DataFrame()
    supported = km.loc[km["n_at_risk"] >= MIN_RISK_SET, "t"]
    max_age = int(supported.max()) if len(supported) else int(km["t"].max())
    max_age = max(0, max_age - horizon_hours)
    grid = np.arange(0, min(MAX_AGE_HOURS, max_age), BAND_HOURS)
    if len(grid) == 0:
        return pd.DataFrame()
    s_at = np.interp(grid, km["t"], km["survival"], left=1.0, right=km["survival"].iloc[-1])
    s_ahead = np.interp(grid + horizon_hours, km["t"], km["survival"],
                        left=1.0, right=km["survival"].iloc[-1])
    with np.errstate(divide="ignore", invalid="ignore"):
        risk = np.where(s_at > 0, 1.0 - s_ahead / s_at, np.nan)
    return pd.DataFrame(
        {
            "age_hours": grid,
            "age_years": grid / HOURS_PER_YEAR,
            "survival_at_age": s_at,
            "risk_30d": risk,
            "risk_30d_per_1000": 1000 * risk,
        }
    )


def plot_survival_by_model(curves: dict[str, pd.DataFrame], overall: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.step(overall["age_years"], overall["survival"], where="post",
            color="black", lw=2.2, label="all data drives", zorder=5)
    ax.fill_between(overall["age_years"], overall["survival_lo"], overall["survival_hi"],
                    step="post", color="black", alpha=0.10, zorder=4)
    for name, c in curves.items():
        ax.step(c["age_years"], c["survival"], where="post", lw=1.4, alpha=0.9, label=name)
    ax.set_xlabel("drive age (years of power on time)")
    ax.set_ylabel("surviving fraction")
    ax.set_title("Kaplan-Meier survival with delayed entry, by drive model")
    ax.set_xlim(0, 12)
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=8, loc="lower left", frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_hazard(bands: pd.DataFrame, km: pd.DataFrame, path: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    ax1.plot(bands["age_years"], bands["afr_pct"], marker="o", ms=3.5, lw=1.5, color="#B3412C")
    ax1.set_xlabel("drive age (years of power on time)")
    ax1.set_ylabel("annualised failure rate (%)")
    ax1.set_title("Hazard by age band, events over exposure")
    ax1.set_xlim(0, 12)
    ax1.grid(alpha=0.25, lw=0.6)

    ax2.step(km["age_years"], km["cum_hazard"], where="post", lw=1.8, color="#1F3A5F")
    ax2.set_xlabel("drive age (years of power on time)")
    ax2.set_ylabel("cumulative hazard")
    ax2.set_title("Nelson-Aalen cumulative hazard")
    ax2.set_xlim(0, 12)
    ax2.grid(alpha=0.25, lw=0.6)

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_incident_validation(full: pd.DataFrame, incident: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.step(full["age_years"], full["survival"], where="post", lw=2.0,
            color="#1F3A5F", label="full cohort, delayed entry")
    ax.fill_between(full["age_years"], full["survival_lo"], full["survival_hi"],
                    step="post", color="#1F3A5F", alpha=0.15)
    if not incident.empty:
        ax.step(incident["age_years"], incident["survival"], where="post", lw=2.0,
                color="#B3412C", ls="--", label="incident cohort, no truncation")
        ax.fill_between(incident["age_years"], incident["survival_lo"], incident["survival_hi"],
                        step="post", color="#B3412C", alpha=0.15)
    ax.set_xlabel("drive age (years of power on time)")
    ax.set_ylabel("surviving fraction")
    ax.set_title("E5 validation: delayed entry against an untruncated cohort")
    ax.set_xlim(0, 3)
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=9, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--figures", default="figures")
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    tables = Path(args.tables)
    if not (tables / "spells.parquet").exists():
        print(f"spells.parquet not found in {tables}, run s1_build_tables.py first")
        return 1

    reports, figures = Path(args.reports), Path(args.figures)
    reports.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    load(con, tables)

    n, ev = con.execute("SELECT COUNT(*), SUM(event) FROM cohort").fetchone()
    print(f"cohort: {n:,} spells, {ev:,} events")

    print("\nfitting Kaplan-Meier with delayed entry")
    overall = km_delayed_entry(con)
    overall.to_csv(reports / "b0_km_overall.csv", index=False)
    print(f"  {len(overall):,} distinct event times")
    print(f"  max risk set {overall['n_at_risk'].max():,}, "
          f"survival at 5 years {np.interp(5 * HOURS_PER_YEAR, overall['t'], overall['survival']):.4f}")

    top = con.execute(
        f"""
        SELECT model, COUNT(*) AS spells, SUM(event) AS events
        FROM cohort GROUP BY model
        ORDER BY events DESC LIMIT {N_MODELS_PLOTTED}
        """
    ).df()
    curves = {}
    for m in top["model"]:
        c = km_delayed_entry(con, f"model = '{m}'")
        if not c.empty:
            curves[m] = c
            c.to_csv(reports / f"b0_km_{m.replace(' ', '_').replace('/', '_')}.csv", index=False)
    print(f"  fitted {len(curves)} per model curves")

    print("\ncomputing hazard by age band")
    bands = hazard_by_band(con)
    bands.to_csv(reports / "b0_hazard_by_band.csv", index=False)
    peak = bands.loc[bands["afr_pct"].idxmax()]
    print(f"  peak annualised failure rate {peak['afr_pct']:.2f}% at age "
          f"{peak['age_years']:.1f} years")
    print(f"  rate at 1 year {np.interp(1.0, bands['age_years'], bands['afr_pct']):.2f}%, "
          f"at 5 years {np.interp(5.0, bands['age_years'], bands['afr_pct']):.2f}%, "
          f"at 8 years {np.interp(8.0, bands['age_years'], bands['afr_pct']):.2f}%")

    print("\ncross check: Nelson-Aalen increments against exposure based hazard")
    na = overall.copy()
    na["band"] = (na["t"] // BAND_HOURS) * BAND_HOURS
    na_band = na.groupby("band", as_index=False)["hazard_increment"].sum()
    cmp = bands.merge(na_band, left_on="band_lo", right_on="band", how="inner")
    cmp["na_rate_per_hour"] = cmp["hazard_increment"] / BAND_HOURS
    cmp["ratio"] = cmp["na_rate_per_hour"] / cmp["hazard_per_hour"]
    cmp = cmp[["age_years", "events", "exposure_hours", "hazard_per_hour",
               "na_rate_per_hour", "ratio"]]
    cmp.to_csv(reports / "b0_estimator_crosscheck.csv", index=False)
    med = cmp.loc[cmp["events"] >= 20, "ratio"].median()
    print(f"  median ratio of the two estimators, bands with 20+ events: {med:.3f}")
    print("  a ratio near 1.0 means the risk set construction agrees with a")
    print("  completely independent exposure calculation")

    print("\ncomputing the B0 prediction, 30 day risk given age")
    risk = conditional_risk(overall)
    risk.to_csv(reports / "b0_conditional_risk_30d.csv", index=False)
    for a in (1, 3, 5, 7, 9):
        r = np.interp(a, risk["age_years"], risk["risk_30d_per_1000"])
        print(f"  age {a} years: {r:.2f} failures per 1000 drives per 30 days")

    print("\nE5 validation on the incident cohort")
    inc_where = f"poh_entry <= {INCIDENT_MAX_ENTRY_HOURS}"
    n_inc, ev_inc = con.execute(
        f"SELECT COUNT(*), SUM(event) FROM cohort WHERE {inc_where}"
    ).fetchone()
    print(f"  {n_inc:,} spells entering effectively new, {ev_inc:,} events")
    incident = km_delayed_entry(con, inc_where)
    incident.to_csv(reports / "b0_km_incident_cohort.csv", index=False)
    if not incident.empty:
        rows = []
        for a in (0.5, 1.0, 1.5, 2.0):
            t = a * HOURS_PER_YEAR
            sf = np.interp(t, overall["t"], overall["survival"])
            si = np.interp(t, incident["t"], incident["survival"])
            lo = np.interp(t, incident["t"], incident["survival_lo"])
            hi = np.interp(t, incident["t"], incident["survival_hi"])
            rows.append({
                "age_years": a,
                "survival_full_cohort": sf,
                "survival_incident": si,
                "incident_ci_lo": lo,
                "incident_ci_hi": hi,
                "full_inside_incident_ci": bool(lo <= sf <= hi),
            })
        e5 = pd.DataFrame(rows)
        e5.to_csv(reports / "b0_e5_incident_validation.csv", index=False)
        print("\n--- b0_e5_incident_validation ---")
        print("If delayed entry is handled correctly the full cohort survival should")
        print("fall inside the incident cohort confidence band at every age.")
        with pd.option_context("display.width", 200):
            print(e5.to_string(index=False))

    print("\nE5 follow up: is the age 2 gap composition or truncation?")
    inc_models = con.execute(
        f"""
        SELECT model FROM cohort WHERE {inc_where}
        GROUP BY model HAVING COUNT(*) >= 500
        """
    ).df()["model"].tolist()
    if inc_models:
        lst = ", ".join("'" + m.replace("'", "''") + "'" for m in inc_models)
        print(f"  restricting both arms to the {len(inc_models)} models that make up the")
        print("  incident cohort, so the comparison holds model mix fixed")
        full_m = km_delayed_entry(con, f"model IN ({lst})")
        inc_m = km_delayed_entry(con, f"model IN ({lst}) AND {inc_where}")
        rows = []
        for a in (0.5, 1.0, 1.5, 2.0):
            t = a * HOURS_PER_YEAR
            sf = np.interp(t, full_m["t"], full_m["survival"])
            si = np.interp(t, inc_m["t"], inc_m["survival"])
            lo = np.interp(t, inc_m["t"], inc_m["survival_lo"])
            hi = np.interp(t, inc_m["t"], inc_m["survival_hi"])
            rows.append({
                "age_years": a,
                "survival_full_matched": sf,
                "survival_incident_matched": si,
                "gap_pp": 100 * (sf - si),
                "incident_ci_lo": lo,
                "incident_ci_hi": hi,
                "full_inside_incident_ci": bool(lo <= sf <= hi),
            })
        e5m = pd.DataFrame(rows)
        e5m.to_csv(reports / "b0_e5_model_matched.csv", index=False)
        print("\n--- b0_e5_model_matched ---")
        print("If the gap closes once model mix is held fixed, the unmatched failure was")
        print("composition, not a truncation error. If it persists, delayed entry is")
        print("mishandled and everything downstream inherits the problem.")
        with pd.option_context("display.width", 200):
            print(e5m.to_string(index=False))

    print("\nwriting figures")
    plot_survival_by_model(curves, overall, figures / "b0_survival_by_model.png")
    plot_hazard(bands, overall, figures / "b0_hazard.png")
    plot_incident_validation(overall, incident, figures / "b0_incident_validation.png")
    for f in sorted(figures.glob("b0_*.png")):
        print(f"  {f}")

    con.close()
    print(f"\ntables in {reports.resolve()}, figures in {figures.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
