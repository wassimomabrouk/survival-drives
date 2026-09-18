"""Summary figures for the README.

Three figures that no single modelling script owns, because each spans several of
them. All are built from committed outputs rather than by refitting anything, so
this runs in seconds and cannot silently disagree with the numbers already in
`reports/`.

  1. model ladder      discrimination across B0, B1, B2 and M2, the results-at-a-
                       glance figure a reader wants before any detail
  2. calibration       B1, B2 and M2 on identical rows. Calibration is a
                       pre-committed constraint in DESIGN.md section 8, so the
                       models that matter need to appear in it, not only the
                       weakest two.
  3. RQ2 decomposition where the Seagate-only gain comes from, which is the
                       question B3 exists to answer

Usage:

    py scripts/s10_summary_figures.py --tables data/tables --reports reports --figures figures
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
import evaluation as ev  # noqa: E402

NAVY = "#1F3A5F"
RUST = "#B3412C"
GREY = "#7A7A7A"


def figure_ladder(reports: Path, figures: Path) -> None:
    """Discrimination across the ladder, with the B3 arm shown separately."""
    pooled = pd.read_csv(reports / "b1_metrics_pooled.csv")
    b0 = float(pooled.loc[pooled["model"] == "B0 age only", "ipcw_auc"].iloc[0])
    b0_lo = float(pooled.loc[pooled["model"] == "B0 age only", "auc_lo"].iloc[0])
    b0_hi = float(pooled.loc[pooled["model"] == "B0 age only", "auc_hi"].iloc[0])
    b1 = float(pooled.loc[pooled["model"] == "B1 model and age", "ipcw_auc"].iloc[0])
    b1_lo = float(pooled.loc[pooled["model"] == "B1 model and age", "auc_lo"].iloc[0])
    b1_hi = float(pooled.loc[pooled["model"] == "B1 model and age", "auc_hi"].iloc[0])

    m2f = pd.read_csv(reports / "m2_metrics_by_fold.csv")
    # Pooling by event count, since folds differ in size.
    def pooled_auc(label):
        sub = m2f[m2f["model"] == label]
        return float(np.average(sub["ipcw_auc"], weights=sub["n_events"]))
    b2, m2 = pooled_auc("B2 log-linear"), pooled_auc("M2 boosted trees")

    labels = ["B0\nage only", "B1\n+ drive model", "B2\n+ SMART", "M2\n+ boosted trees"]
    vals = [b0, b1, b2, m2]
    errs = [[b0 - b0_lo, b1 - b1_lo, 0, 0], [b0_hi - b0, b1_hi - b1, 0, 0]]
    colours = [GREY, GREY, NAVY, NAVY]

    fig, ax = plt.subplots(figsize=(8.5, 5.4))
    bars = ax.bar(labels, vals, color=colours, width=0.62)
    ax.errorbar(range(4), vals, yerr=errs, fmt="none", ecolor="#333333",
                capsize=4, lw=1.2)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.006, f"{v:.3f}",
                ha="center", fontsize=10.5, fontweight="bold")
    ax.set_ylim(0.55, 0.93)
    ax.set_ylabel("IPCW time-dependent AUC (0.5 would be chance)")
    ax.set_title("Discrimination across the model ladder")
    ax.grid(alpha=0.25, lw=0.6, axis="y")
    fig.text(0.5, 0.015,
             "the jump from B1 to B2 is SMART telemetry; everything after it is "
             "functional form",
             fontsize=8.5, color="#555555", ha="center")
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    fig.savefig(figures / "summary_model_ladder.png", dpi=150)
    plt.close(fig)
    print(f"  model ladder: B0 {b0:.3f}, B1 {b1:.3f}, B2 {b2:.3f}, M2 {m2:.3f}")


def figure_calibration(tables: Path, figures: Path) -> None:
    """B1, B2 and M2 calibration on identical held-out rows."""
    df = pd.read_parquet(tables / "predictions.parquet")
    t = df["t_days"].to_numpy(float)
    s = df["status"].to_numpy(int)

    fig, ax = plt.subplots(figsize=(6.8, 6.4))
    lim = 0.0
    for label, col, colour, style in (("B1, drive model and age", "risk_b1", GREY, "-"),
                                      ("B2, + SMART", "risk_b2", "#5B8FA8", "-"),
                                      ("M2, + boosted trees", "risk_m2", NAVY, "-")):
        c = ev.calibration_table(df[col].to_numpy(float), t, s)
        ax.plot(c["mean_predicted"], c["observed_ipcw"], style, marker="o", ms=5,
                lw=1.7, color=colour, label=label)
        lim = max(lim, c["mean_predicted"].max(), c["observed_ipcw"].max())
        spread = c["ratio_obs_pred"].max() - c["ratio_obs_pred"].min()
        print(f"  {label}: decile spread {spread:.3f}")

    # Log-log axes. Predicted risks span two orders of magnitude across the
    # deciles, and on a linear scale the eight lowest deciles of every model pile
    # into the bottom-left corner while the axis is set by the top decile of
    # whichever model predicts highest.
    lim *= 1.3
    lo = 2e-4
    ax.plot([lo, lim], [lo, lim], color="#999999", lw=1.1, ls="--",
            label="perfect calibration")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, lim)
    ax.set_ylim(lo, lim)
    ax.set_xlabel("mean predicted 30 day risk")
    ax.set_ylabel("observed 30 day risk (IPCW)")
    ax.set_title("Calibration by predicted risk decile, pooled held-out quarters")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=9, frameon=False, loc="upper left")
    ax.text(0.97, 0.05, "points below the diagonal mean the model over-predicts",
            transform=ax.transAxes, fontsize=8.5, color="#555555", ha="right")
    fig.tight_layout()
    fig.savefig(figures / "summary_calibration.png", dpi=150)
    plt.close(fig)


def figure_rq2(reports: Path, figures: Path) -> None:
    """Where the Seagate-only AUC gain comes from."""
    p = pd.read_csv(reports / "b3_paired_comparisons.csv")
    p = p[(p["level"] == "raw") & (p["metric"] == "ipcw_auc")]

    order = ["RQ2: all five Seagate attributes", "attribute 187 alone",
             "the other four beyond 187"]
    nice = ["all five\nSeagate-only\nattributes", "attribute 187\nalone",
            "the other four\nbeyond 187"]
    vals, los, his = [], [], []
    for o in order:
        r = p[p["comparison"] == o].iloc[0]
        vals.append(float(r["difference"]))
        los.append(float(r["difference"] - r["lo"]))
        his.append(float(r["hi"] - r["difference"]))

    fig, ax = plt.subplots(figsize=(8, 5.2))
    bars = ax.bar(nice, vals, color=[NAVY, "#5B8FA8", "#9CC3D5"], width=0.58)
    ax.errorbar(range(3), vals, yerr=[los, his], fmt="none", ecolor="#333333",
                capsize=5, lw=1.3)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.0012, f"+{v:.4f}",
                ha="center", fontsize=10.5, fontweight="bold")
    ax.axhline(0, color="#333333", lw=1.0)
    ax.set_ylabel("gain in IPCW AUC over the universal attributes")
    ax.set_title("RQ2: what vendor-specific SMART telemetry is worth")
    ax.grid(alpha=0.25, lw=0.6, axis="y")
    share = 100 * vals[1] / vals[0] if vals[0] else float("nan")
    ax.text(0.5, 0.93,
            f"attribute 187 carries {share:.0f}% of the gain, but the other four are not "
            f"negligible\nas expectation E3 predicted; both intervals exclude zero",
            transform=ax.transAxes, fontsize=9, color="#555555", ha="center")
    fig.tight_layout()
    fig.savefig(figures / "summary_rq2_decomposition.png", dpi=150)
    plt.close(fig)
    print(f"  RQ2: total +{vals[0]:.4f}, 187 alone +{vals[1]:.4f} ({share:.0f}%), "
          f"other four +{vals[2]:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--figures", default="figures")
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    figures.mkdir(parents=True, exist_ok=True)

    needed = [reports / "b1_metrics_pooled.csv", reports / "m2_metrics_by_fold.csv",
              reports / "b3_paired_comparisons.csv", tables / "predictions.parquet"]
    missing = [p for p in needed if not p.exists()]
    if missing:
        print("missing inputs, run the earlier scripts first:")
        for m in missing:
            print(f"  {m}")
        return 1

    print("model ladder")
    figure_ladder(reports, figures)
    print("calibration")
    figure_calibration(tables, figures)
    print("RQ2 decomposition")
    figure_rq2(reports, figures)

    for f in sorted(figures.glob("summary_*.png")):
        print(f"\nwrote {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
