"""Why are the savings smaller on two held-out quarters than on three?

`s9_decision_layer.py` simulates the policy over all three held-out quarters and
finds, with the threshold chosen in hindsight, a 25.1% lower fleet cost rate at a
cost ratio of 10. `s11_prospective_policy.py` applies it to the last two quarters
only, and even the hindsight-best threshold there saves 15.4%. Choosing the
threshold in advance is not the cause: s11 measures that optimism directly, at
under half a percentage point. Something about the window is.

Two explanations fit, and they predict different things.

  window length  a shorter simulation gives preventive replacement less time to
                 pay off, so any single quarter should save less than the pooled
                 three, whichever quarter it is
  composition    the policy is worth more when more drives fail, and 2025 Q3 ran
                 at a higher failure rate than the two quarters after it, so the
                 saving in each window should track that window's own failure
                 rate, and a single high-rate quarter can save as much as the
                 pooled three

This runs the hindsight-best policy, exactly as s9 does, on every single quarter,
both adjacent pairs and all three, and reports each window's saving next to its
failure rate. It is a diagnostic added on 2026-10-02 after the s9 and s11 output
had been read; no verdict depends on it.

Usage:

    py scripts/s11b_window_check.py --tables data/tables --reports reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s9_decision_layer import K_GRID, cost_rate, simulate, threshold_grid  # noqa: E402
from s11_prospective_policy import spell_bounds  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--risk-col", default="risk_m2")
    args = ap.parse_args()

    tables, reports = Path(args.tables), Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)
    path = tables / "predictions.parquet"
    if not path.exists():
        print(f"{path} not found. Run s8_m2_boosted.py first.")
        return 1

    df = pd.read_parquet(path)
    df["quarter"] = pd.PeriodIndex(pd.to_datetime(df["landmark"]), freq="Q")
    quarters = sorted(df["quarter"].unique())
    if len(quarters) != 3:
        print(f"expected three held-out quarters, found {len(quarters)}")
        return 1

    # The same threshold grid as s9, built on all held-out rows.
    risk_all = df[args.risk_col].to_numpy(float)
    tau_grid = threshold_grid(risk_all)

    q1, q2, q3 = quarters
    windows = {
        str(q1): [q1], str(q2): [q2], str(q3): [q3],
        f"{q1}+{q2}": [q1, q2], f"{q2}+{q3}": [q2, q3],
        f"{q1}+{q2}+{q3}": [q1, q2, q3],
    }

    rows = []
    for name, qs in windows.items():
        w = (df[df["quarter"].isin(qs)]
             .sort_values(["spell_key", "landmark"], kind="mergesort")
             .reset_index(drop=True))
        risk = w[args.risk_col].to_numpy(float)
        t_days = w["t_days"].to_numpy(float)
        fail = w["fail"].to_numpy(int)
        starts, ends = spell_bounds(w)

        base = simulate(risk, np.zeros(len(w), dtype=bool), t_days, fail, starts, ends)
        # One simulation per threshold; the cost ratio only re-weights its counts.
        sims = [simulate(risk, risk > tau, t_days, fail, starts, ends) for tau in tau_grid]
        print(f"  {name}: {len(w):,} rows, {base['failures']:,} failures over "
              f"{base['served_years']:,.0f} drive years")

        for k in K_GRID:
            rtf = cost_rate(base, k)
            costs = [cost_rate(s, k) for s in sims]
            i = int(np.nanargmin(costs))
            rows.append({
                "window": name, "n_quarters": len(qs),
                "failures": base["failures"],
                "drive_years": base["served_years"],
                "failures_per_100_drive_years": 100 * base["failures"] / base["served_years"],
                "k": k, "best_tau": float(tau_grid[i]),
                "replacements": sims[i]["replacements"],
                "projected_prevented": sims[i]["projected_prevented"],
                "hindsight_vs_rtf_pct": 100 * (costs[i] - rtf) / rtf,
            })

    out = pd.DataFrame(rows)
    out.to_csv(reports / "s11b_window_check.csv", index=False)

    piv = out.pivot_table(index=["window", "failures_per_100_drive_years"],
                          columns="k", values="hindsight_vs_rtf_pct").reset_index()
    order = list(windows)
    piv["window"] = pd.Categorical(piv["window"], order, ordered=True)
    piv = piv.sort_values("window")
    print("\n--- s11b_window_check ---")
    print("Hindsight-best saving against run-to-failure, by window (negative is")
    print("cheaper), beside each window's own failure rate per 100 drive years.")
    with pd.option_context("display.width", 200, "display.float_format", "{:.2f}".format):
        print(piv.to_string(index=False))

    print("\nReading: if every single quarter saves less than the pooled three, the")
    print("window length is the cause. If single quarters scatter around the pooled")
    print("figure in line with their own failure rates, composition is.")
    print(f"\nwritten to {reports.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
