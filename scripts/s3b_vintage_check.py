"""Is the age 2 gap in E5 an installation vintage effect?

B0 found that full cohort survival at age 2 sits 0.23 percentage points below the
incident cohort confidence band, while ages 0.5, 1.0 and 1.5 agree closely.
Matching on drive model changed the gap by 0.000 pp, so composition by model is
not the explanation.

The remaining structural candidate is installation vintage. Age 2 is 17,520
power on hours, and the observation window is 27 months, so the only incident
drives that can reach age 2 are those installed in roughly the first quarter of
2024: the incident arm at age 2 is one narrow manufacturing and installation
vintage. The full arm at the same age draws on drives installed across several
earlier years.

Installation date is recoverable even for truncated spells, because a spell's
power on hours at entry says how long it had already run:

    installed ~= entry_date - poh_entry / 24 days

This is approximate. It assumes a drive is powered continuously, which is true
for a storage fleet but not exactly, and weekly sampling adds up to seven days of
slack. It is precise enough to separate vintages a year apart, which is all this
test needs.

If survival at age 2 varies by vintage, and the 2024 vintage matches the incident
arm, the gap is a cohort effect rather than a truncation error and delayed entry
is working correctly.

**On the 21-quarter window (2021 Q1 to 2026 Q1) the paragraph above no longer
holds.** Incident drives reaching age 2 now come from installation years 2020 to
2024, so the incident arm is no longer one vintage and the single-vintage match is
not available. The like-for-like comparison section 9 specifies, same drive models
and same installation vintage, is therefore made directly: within each
installation year, the delayed-entry estimate on all of that year's spells is
compared with an estimate on that year's incident spells only. Added 2026-10-02,
after the 21-quarter s3 and s3b output had been read; DESIGN.md section 13 logs
it and fixes the decision rule before it ran.

Usage:

    py scripts/s3b_vintage_check.py --tables data/tables --reports reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s3_b0_baseline import HOURS_PER_YEAR, km_delayed_entry  # noqa: E402

INCIDENT_MAX_ENTRY_HOURS = 720
MIN_VINTAGE_SPELLS = 2000
TEST_AGES = (0.5, 1.0, 1.5, 2.0)
# A within-vintage comparison is made only where the incident arm still has this
# many drives at risk, so that its confidence band is not too wide to fail.
MIN_INCIDENT_AT_RISK = 1000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    tables = Path(args.tables)
    reports = Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(
        f"""
        CREATE OR REPLACE VIEW cohort AS
        SELECT
            spell_key, model, manufacturer, entry_date, exit_date, event,
            poh_entry, poh_exit,
            DATE_TRUNC('year',
                entry_date - CAST(poh_entry / 24 AS INTEGER) * INTERVAL 1 DAY
            ) AS install_year
        FROM read_parquet('{(tables / 'spells.parquet').as_posix()}')
        WHERE poh_entry IS NOT NULL AND poh_exit IS NOT NULL AND poh_exit > poh_entry
        """
    )

    # Hold model mix fixed the same way B0 did, so this test isolates vintage.
    models = con.execute(
        f"""
        SELECT model FROM cohort
        WHERE poh_entry <= {INCIDENT_MAX_ENTRY_HOURS}
        GROUP BY model HAVING COUNT(*) >= 500
        """
    ).df()["model"].tolist()
    lst = ", ".join("'" + m.replace("'", "''") + "'" for m in models)
    print(f"holding model mix fixed to the {len(models)} incident cohort models")

    print("\n--- s3b_vintage_composition ---")
    print("Who is actually in the risk set at age 2, by installation year.")
    comp = con.execute(
        f"""
        SELECT
            YEAR(install_year) AS install_year,
            COUNT(*)                                                    AS spells,
            SUM(CASE WHEN poh_entry <= {INCIDENT_MAX_ENTRY_HOURS}
                     THEN 1 ELSE 0 END)                                 AS incident_spells,
            SUM(CASE WHEN poh_entry < 2 * {HOURS_PER_YEAR}
                      AND poh_exit  > 2 * {HOURS_PER_YEAR}
                     THEN 1 ELSE 0 END)                                 AS at_risk_at_age_2,
            SUM(event)                                                  AS events
        FROM cohort WHERE model IN ({lst})
        GROUP BY 1 ORDER BY 1
        """
    ).df()
    comp.to_csv(reports / "s3b_vintage_composition.csv", index=False)
    with pd.option_context("display.width", 200):
        print(comp.to_string(index=False))

    vintages = comp.loc[comp["at_risk_at_age_2"] >= MIN_VINTAGE_SPELLS, "install_year"].tolist()
    print(f"\nfitting survival separately for install years {vintages}")

    rows = []
    for vy in vintages:
        c = km_delayed_entry(con, f"model IN ({lst}) AND YEAR(install_year) = {vy}")
        if c.empty:
            continue
        for a in TEST_AGES:
            t = a * HOURS_PER_YEAR
            if t > c["t"].max():
                continue
            rows.append({
                "install_year": vy,
                "age_years": a,
                "survival": float(np.interp(t, c["t"], c["survival"])),
                "ci_lo": float(np.interp(t, c["t"], c["survival_lo"])),
                "ci_hi": float(np.interp(t, c["t"], c["survival_hi"])),
            })

    inc = km_delayed_entry(
        con, f"model IN ({lst}) AND poh_entry <= {INCIDENT_MAX_ENTRY_HOURS}"
    )
    for a in TEST_AGES:
        t = a * HOURS_PER_YEAR
        rows.append({
            "install_year": "incident arm",
            "age_years": a,
            "survival": float(np.interp(t, inc["t"], inc["survival"])),
            "ci_lo": float(np.interp(t, inc["t"], inc["survival_lo"])),
            "ci_hi": float(np.interp(t, inc["t"], inc["survival_hi"])),
        })

    out = pd.DataFrame(rows)
    piv = out.pivot(index="install_year", columns="age_years", values="survival")
    out.to_csv(reports / "s3b_vintage_survival.csv", index=False)

    print("\n--- s3b_vintage_survival ---")
    print("Survival by installation year, model mix held fixed. If the rows differ")
    print("materially at age 2, vintage is a real effect and the E5 gap is a cohort")
    print("difference rather than a truncation error. If every row agrees and only")
    print("the pooled full cohort differs, the explanation lies elsewhere.")
    with pd.option_context("display.width", 200, "display.float_format", "{:.5f}".format):
        print(piv.to_string())

    if "incident arm" in piv.index and 2.0 in piv.columns:
        inc2 = piv.loc["incident arm", 2.0]
        spread = piv.loc[piv.index != "incident arm", 2.0].dropna()
        if len(spread):
            print(f"\nat age 2: incident arm {inc2:.5f}, vintages range "
                  f"{spread.min():.5f} to {spread.max():.5f}, "
                  f"spread {100 * (spread.max() - spread.min()):.3f} pp")
            gap_path = reports / "b0_e5_model_matched.csv"
            if gap_path.exists():
                g = pd.read_csv(gap_path)
                g2 = g.loc[np.isclose(g["age_years"], 2.0), "gap_pp"]
                if len(g2):
                    print(f"Compare that spread against the {abs(float(g2.iloc[0])):.3f} pp "
                          f"gap B0 reported at age 2. A spread of")
                    print("similar size or larger means vintage alone can account for the gap.")

    # ------------------------------------------------- like for like, per vintage
    # Section 9's E5 criterion: the delayed-entry estimate and an incident-only
    # estimate agree within confidence intervals when restricted to the same drive
    # models and the same installation vintage.
    like = []
    for vy in vintages:
        base = f"model IN ({lst}) AND YEAR(install_year) = {vy}"
        n_all, n_inc = con.execute(
            f"""SELECT COUNT(*), SUM(CASE WHEN poh_entry <= {INCIDENT_MAX_ENTRY_HOURS}
                                       THEN 1 ELSE 0 END)
                FROM cohort WHERE {base}"""
        ).fetchone()
        if not n_inc:
            continue
        full_v = km_delayed_entry(con, base)
        inc_v = km_delayed_entry(con, f"{base} AND poh_entry <= {INCIDENT_MAX_ENTRY_HOURS}")
        if full_v.empty or inc_v.empty:
            continue
        for a in TEST_AGES:
            t = a * HOURS_PER_YEAR
            if t > inc_v["t"].max() or t > full_v["t"].max():
                continue
            at_risk = int(inc_v.loc[inc_v["t"] <= t, "n_at_risk"].iloc[-1]) \
                if (inc_v["t"] <= t).any() else int(inc_v["n_at_risk"].iloc[0])
            if at_risk < MIN_INCIDENT_AT_RISK:
                continue
            sf = float(np.interp(t, full_v["t"], full_v["survival"]))
            si = float(np.interp(t, inc_v["t"], inc_v["survival"]))
            lo = float(np.interp(t, inc_v["t"], inc_v["survival_lo"]))
            hi = float(np.interp(t, inc_v["t"], inc_v["survival_hi"]))
            like.append({
                "install_year": vy, "age_years": a,
                "spells": int(n_all), "incident_share": round(n_inc / n_all, 3),
                "incident_at_risk": at_risk,
                "survival_delayed_entry": sf, "survival_incident": si,
                "incident_ci_lo": lo, "incident_ci_hi": hi,
                "gap_pp": 100 * (sf - si),
                "inside_incident_ci": bool(lo <= sf <= hi),
            })

    lk = pd.DataFrame(like)
    lk.to_csv(reports / "s3b_e5_like_for_like.csv", index=False)
    print("\n--- s3b_e5_like_for_like ---")
    print("Within each installation year: delayed-entry estimate on all spells")
    print("against an estimate on that year's incident spells only.")
    if lk.empty:
        print("no vintage had enough incident drives at risk to compare")
    else:
        with pd.option_context("display.width", 220, "display.float_format", "{:.5f}".format):
            print(lk.to_string(index=False))
        miss = lk[~lk["inside_incident_ci"]]
        print(f"\n{len(lk)} comparisons, {len(miss)} outside the incident band")
        print("E5 on this window: " + ("HOLDS" if miss.empty else "FAILS")
              + " (rule fixed in DESIGN.md section 13 before this ran)")

    con.close()
    print(f"\nwritten to {reports.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
