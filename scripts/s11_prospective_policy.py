"""Does the policy still pay when the threshold is chosen without hindsight?

`s9_decision_layer.py` searches the replacement threshold tau over the held-out
rows and reports the value that minimises cost there. That uses the answer: a
real operator picks a threshold before seeing the quarter it will be applied to,
and cannot land on the optimum by construction. The reported savings are
therefore optimistic by an unmeasured amount.

This script measures it. The models themselves were never affected: each was
trained strictly before the quarter it scored, and the simulator never consults
an outcome when deciding. The hindsight enters at exactly one point, choosing
tau, and that is what is corrected here.

**The prospective protocol**, which is what an operator can actually do:

    pick tau on the FIRST held-out quarter by minimising realised cost there
    apply that same tau, unchanged, to every later held-out quarter
    report what it actually cost

The first quarter's outcomes are known by the time the next begins, so this uses
nothing the operator would not have. Each test quarter in the rolling origin was
scored by a different model trained up to its own start, which is also what an
operator would have: last quarter's model and last quarter's outcomes.

**The application window is pooled, with spells left intact across quarters.**
An earlier version of this script evaluated quarter by quarter, which splits a
drive observed across three quarters into three separate units. That silently
destroys the policy's cross-quarter credit: replacing a drive in one quarter no
longer prevents its failure two quarters later, because the drive reappears as a
fresh unit. The measured optimism was unaffected, since both arms shared the
error, but the absolute cost rates came out far weaker than `s9_decision_layer.py`
reports for the same data and were not comparable to anything else in the
project. Pooling the application window matches s9's accounting exactly, so the
only difference between the two is how tau was chosen.

The gap between this and the hindsight optimum on the same quarter is the
optimism in the headline figures. A small gap means the cost curve is flat enough
near its minimum that picking the threshold in advance costs little, and the
reported result stands. A large gap means the headline overstates what the policy
delivers in practice, and the prospective number becomes the one to report.

Usage:

    py scripts/s11_prospective_policy.py --tables data/tables --reports reports --figures figures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s9_decision_layer import K_GRID, cost_rate, simulate  # noqa: E402

NAVY = "#1F3A5F"
RUST = "#B3412C"
GREY = "#7A7A7A"


def spell_bounds(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Start and end row index of each spell, for a frame already sorted by
    (spell_key, landmark)."""
    keys = df["spell_key"].to_numpy()
    _, first = np.unique(keys, return_index=True)
    starts = np.sort(first)
    ends = np.append(starts[1:], len(df))
    return starts, ends


def best_tau(df: pd.DataFrame, risk_col: str, tau_grid: np.ndarray, k: float) -> float:
    """The threshold minimising cost on this quarter, with its outcomes known."""
    risk = df[risk_col].to_numpy(float)
    t_days = df["t_days"].to_numpy(float)
    fail = df["fail"].to_numpy(int)
    starts, ends = spell_bounds(df)
    best, best_cost = tau_grid[0], np.inf
    for tau in tau_grid:
        c = cost_rate(simulate(risk, risk > tau, t_days, fail, starts, ends), k)
        if np.isfinite(c) and c < best_cost:
            best, best_cost = float(tau), c
    return best


def apply_tau(df: pd.DataFrame, risk_col: str, tau: float, k: float) -> dict:
    """Run one fixed threshold over a quarter and report what it cost."""
    risk = df[risk_col].to_numpy(float)
    res = simulate(risk, risk > tau,
                   df["t_days"].to_numpy(float), df["fail"].to_numpy(int),
                   *spell_bounds(df))
    res["cost_rate"] = cost_rate(res, k)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--figures", default="figures")
    ap.add_argument("--risk-col", default="risk_m2")
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    reports.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    path = tables / "predictions.parquet"
    if not path.exists():
        print(f"{path} not found. Run s8_m2_boosted.py first.")
        return 1

    df = pd.read_parquet(path)
    df["quarter"] = pd.PeriodIndex(pd.to_datetime(df["landmark"]), freq="Q")
    quarters = sorted(df["quarter"].unique())
    print(f"{len(df):,} held-out rows across {len(quarters)} quarters: "
          f"{', '.join(str(q) for q in quarters)}")
    if len(quarters) < 2:
        print("need at least two held-out quarters to choose a threshold on one "
              "and apply it to the next")
        return 1

    frames = {}
    for q in quarters:
        sub = (df[df["quarter"] == q]
               .sort_values(["spell_key", "landmark"], kind="mergesort")
               .reset_index(drop=True))
        frames[q] = sub
        print(f"  {q}: {len(sub):,} rows, {sub['spell_key'].nunique():,} spells, "
              f"{int(sub['fail'].sum()):,} events")

    # Same threshold grid as the headline analysis, built on all held-out rows so
    # the two are directly comparable.
    risk_all = df[args.risk_col].to_numpy(float)
    tau_grid = np.unique(np.quantile(risk_all, np.linspace(0.90, 0.99999, 40)))

    select_q = quarters[0]
    apply_q = quarters[1:]
    select_df = frames[select_q]

    # Application window: every later quarter pooled, spells left whole, exactly
    # as s9_decision_layer.py handles them.
    apply_df = (df[df["quarter"].isin(apply_q)]
                .sort_values(["spell_key", "landmark"], kind="mergesort")
                .reset_index(drop=True))
    print(f"\nthreshold chosen on {select_q}, applied to "
          f"{', '.join(str(q) for q in apply_q)} pooled "
          f"({len(apply_df):,} rows, {apply_df['spell_key'].nunique():,} spells, "
          f"{int(apply_df['fail'].sum()):,} events)")

    base = simulate(apply_df[args.risk_col].to_numpy(float),
                    np.zeros(len(apply_df), dtype=bool),
                    apply_df["t_days"].to_numpy(float),
                    apply_df["fail"].to_numpy(int),
                    *spell_bounds(apply_df))

    rows = []
    for k in K_GRID:
        rtf = cost_rate(base, k)

        tau_p = best_tau(select_df, args.risk_col, tau_grid, k)
        prosp = apply_tau(apply_df, args.risk_col, tau_p, k)

        tau_h = best_tau(apply_df, args.risk_col, tau_grid, k)
        hind = apply_tau(apply_df, args.risk_col, tau_h, k)

        rows.append({
            "threshold_from": str(select_q),
            "applied_to": "+".join(str(q) for q in apply_q), "k": k,
            "tau_prospective": tau_p, "tau_hindsight": tau_h,
            "prospective_vs_rtf_pct": 100 * (prosp["cost_rate"] - rtf) / rtf,
            "hindsight_vs_rtf_pct": 100 * (hind["cost_rate"] - rtf) / rtf,
            "optimism_pp": 100 * (prosp["cost_rate"] - hind["cost_rate"]) / rtf,
            "replacements": prosp["replacements"],
            "failures": prosp["failures"],
            "projected_prevented": prosp["projected_prevented"],
            "unnecessary": prosp["unnecessary"],
        })

    out = pd.DataFrame(rows)
    out.to_csv(reports / "s11_prospective_policy.csv", index=False)
    summary = out[["k", "prospective_vs_rtf_pct", "hindsight_vs_rtf_pct",
                   "optimism_pp"]].copy()
    summary.to_csv(reports / "s11_prospective_summary.csv", index=False)

    print("\n--- s11_prospective_policy ---")
    print("tau is chosen on the first held-out quarter and applied unchanged to the")
    print("rest. Negative percentages are cheaper than running to failure, and")
    print("optimism_pp is how much the hindsight figure flatters the policy.")
    with pd.option_context("display.width", 240, "display.float_format", "{:.3f}".format):
        print(out[["k", "tau_prospective", "tau_hindsight",
                   "prospective_vs_rtf_pct", "hindsight_vs_rtf_pct", "optimism_pp",
                   "replacements", "projected_prevented"]].to_string(index=False))

    fig, ax = plt.subplots(figsize=(8.5, 5.4))
    ax.plot(summary["k"], summary["hindsight_vs_rtf_pct"], "--", marker="o", ms=6,
            lw=1.8, color=GREY, label="threshold chosen with hindsight")
    ax.plot(summary["k"], summary["prospective_vs_rtf_pct"], "-", marker="o", ms=6,
            lw=2.2, color=NAVY, label="threshold chosen on the previous quarter")
    ax.axhline(0.0, color=RUST, lw=1.3, ls=":", label="run-to-failure")
    ax.set_xscale("log")
    ax.set_xticks(K_GRID)
    ax.set_xticklabels([str(k) for k in K_GRID])
    ax.set_xlabel("cost ratio k = cost of unplanned failure / cost of planned replacement")
    ax.set_ylabel("change in fleet cost rate (%)")
    ax.set_title("What the policy is worth when the threshold is set in advance")
    ax.text(0.98, 0.95, f"threshold fitted on {select_q}, applied to later quarters",
            transform=ax.transAxes, fontsize=8.5, color="#555555", ha="right")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=9, frameon=False, loc="lower left")
    fig.tight_layout()
    fig.savefig(figures / "s11_prospective_policy.png", dpi=150)
    plt.close(fig)

    worst = float(summary["optimism_pp"].abs().max())
    print("\n================ verdict ================")
    print(f"  applied to {len(apply_q)} held-out quarter(s) after the selection quarter")
    print(f"  largest optimism from choosing tau with hindsight: {worst:.2f} "
          f"percentage points of the cost rate")
    if worst < 5:
        print("  The cost curve is flat enough near its minimum that setting the")
        print("  threshold in advance costs little. The headline figures stand, with")
        print("  the prospective numbers reported alongside them.")
    else:
        print("  Setting the threshold in advance costs materially. The prospective")
        print("  figures, not the hindsight ones, are what the policy delivers.")
    neg = summary[summary["prospective_vs_rtf_pct"] < 0]
    if len(neg):
        print(f"  Prospectively the policy still pays from k = {int(neg['k'].min())} upward.")
    else:
        print("  Prospectively the policy does not pay at any cost ratio tested.")

    print(f"\nwritten to {reports.resolve()} and {figures.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
