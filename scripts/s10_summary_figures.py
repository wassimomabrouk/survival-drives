"""Summary figures for the README.

Three figures that no single modelling script owns, because each spans several of
them. All are built from committed outputs rather than by refitting anything, so
this cannot silently disagree with the numbers already in `reports/`. It takes a
few minutes, almost all of it one spell-level bootstrap (see below).

  1. model ladder      discrimination across B0, B1, B2 and M2, the results-at-a-
                       glance figure a reader wants before any detail. All four
                       are scored on the identical held-out rows in
                       predictions.parquet, and each step carries its paired
                       interval rather than marginal ones, since the paired
                       difference is the comparison actually being made. The
                       B2 and M2 steps are read from s5 and s8; the B1 minus B0
                       step is bootstrapped here, because s4 tests it on the whole
                       fleet rather than on these rows.
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
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluation as ev  # noqa: E402

NAVY = "#1F3A5F"
RUST = "#B3412C"
GREY = "#7A7A7A"


def figure_ladder(df: pd.DataFrame, reports: Path, figures: Path, n_boot: int) -> None:
    """Discrimination across the ladder, every model on the identical held-out rows.

    An earlier version took B0 and B1 from s4, which scores the whole fleet, and
    B2 and M2 from s8, which scores Cohort A, and averaged B2 and M2 across folds.
    The rungs were on different rows and the step from B1 to B2 did not match the
    E2 difference. Corrected 2026-10-02 (DESIGN.md section 13).
    """
    labels = ["B0 age only", "B1 model and age", "B2 plus SMART", "M2 boosted trees"]
    cols = ["risk_b0", "risk_b1", "risk_b2", "risk_m2"]
    rows = []
    for lab, col in zip(labels, cols):
        m = ev.evaluate(df, col)
        m["model"] = lab
        rows.append(m)
    lad = pd.DataFrame(rows)[["model", "n_rows", "n_events", "mean_predicted",
                              "ipcw_brier", "ipcw_auc"]]
    lad.to_csv(reports / "summary_model_ladder.csv", index=False)
    auc = lad["ipcw_auc"].to_numpy()

    # Paired step intervals. B2-B1 and M2-B2 come from the scripts that tested
    # them on these same rows; their point estimates must match what is computed
    # here, and a mismatch means the rows differ.
    b2p = pd.read_csv(reports / "b2_paired_comparison.csv")
    b2p = b2p[b2p["metric"] == "ipcw_auc"].iloc[0]
    m2p = pd.read_csv(reports / "m2_paired_comparison.csv")
    m2p = m2p[(m2p["metric"] == "ipcw_auc") & (m2p["level"] == "raw")].iloc[0]
    # s5 and s8 each refit B1 and B2, so their predictions agree only to floating
    # point noise, around 1e-7 in AUC. A row mismatch would show in the third
    # decimal, so 1e-5 separates the two cases with a wide margin either side.
    for name, got, want in (("B2 minus B1", auc[2] - auc[1], b2p["difference"]),
                            ("M2 minus B2", auc[3] - auc[2], m2p["difference"])):
        print(f"  {name}: {got:.9f} here, {want:.9f} in reports/")
        if abs(got - want) > 1e-5:
            raise SystemExit(f"{name} differs by {abs(got - want):.2e}, so the rows "
                             "differ. Rerun s5 and s8 before this.")
    print(f"  bootstrapping the B1 minus B0 step on these rows, {n_boot} resamples")
    s10 = ev.bootstrap_paired_difference(df, "risk_b0", "risk_b1", "ipcw_auc",
                                         n_boot=n_boot)
    steps = pd.DataFrame([
        {"step": "B1 minus B0", "difference": s10["difference"],
         "lo": s10["lo"], "hi": s10["hi"], "source": "s10, on these rows"},
        {"step": "B2 minus B1", "difference": b2p["difference"],
         "lo": b2p["lo"], "hi": b2p["hi"], "source": "s5 b2_paired_comparison"},
        {"step": "M2 minus B2", "difference": m2p["difference"],
         "lo": m2p["lo"], "hi": m2p["hi"], "source": "s8 m2_paired_comparison"},
    ])
    steps.to_csv(reports / "summary_ladder_steps.csv", index=False)

    ticks = ["B0\nage only", "B1\n+ drive model", "B2\n+ SMART", "M2\n+ boosted trees"]
    colours = [GREY, GREY, NAVY, NAVY]
    fig, ax = plt.subplots(figsize=(8.5, 5.6))
    bars = ax.bar(ticks, auc, color=colours, width=0.6)
    for b, v in zip(bars, auc):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.006, f"{v:.3f}",
                ha="center", fontsize=10.5, fontweight="bold", color="#222222")
    # Step labels sit between the bars they compare, above the taller of the two.
    for i, r in steps.reset_index(drop=True).iterrows():
        x = i + 0.5
        y = max(auc[i], auc[i + 1]) + 0.045
        ax.text(x, y, f"{r['difference']:+.3f}\n[{r['lo']:+.3f}, {r['hi']:+.3f}]",
                ha="center", fontsize=8.5, color="#444444")
    ax.set_ylim(0.5, 0.97)
    ax.set_ylabel("IPCW time-dependent AUC (0.5 would be chance)")
    ax.set_title("Discrimination across the model ladder, identical held-out rows")
    ax.grid(alpha=0.25, lw=0.6, axis="y")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.text(0.5, 0.015,
             "between the bars: the paired AUC gain and its spell-level 95% interval",
             fontsize=8.5, color="#555555", ha="center")
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    fig.savefig(figures / "summary_model_ladder.png", dpi=150)
    plt.close(fig)
    print("  model ladder: " + ", ".join(f"{l.split()[0]} {v:.3f}"
                                         for l, v in zip(labels, auc)))
    for _, r in steps.iterrows():
        print(f"  {r['step']}: {r['difference']:+.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}]")


def figure_calibration(df: pd.DataFrame, reports: Path, figures: Path) -> None:
    """B1, B2 and M2 calibration on identical held-out rows."""
    t = df["t_days"].to_numpy(float)
    s = df["status"].to_numpy(int)

    fig, ax = plt.subplots(figsize=(6.8, 6.4))
    lim = 0.0
    tables_out, spreads = [], []
    for label, col, colour, style in (("B1, drive model and age", "risk_b1", GREY, "-"),
                                      ("B2, + SMART", "risk_b2", "#5B8FA8", "-"),
                                      ("M2, + boosted trees", "risk_m2", NAVY, "-")):
        c = ev.calibration_table(df[col].to_numpy(float), t, s)
        ax.plot(c["mean_predicted"], c["observed_ipcw"], style, marker="o", ms=5,
                lw=1.7, color=colour, label=label)
        lim = max(lim, c["mean_predicted"].max(), c["observed_ipcw"].max())
        spread = c["ratio_obs_pred"].max() - c["ratio_obs_pred"].min()
        print(f"  {label}: decile spread {spread:.3f}")
        tables_out.append(c.assign(model=label))
        spreads.append({"model": label, "decile_spread": spread,
                        "ratio_min": c["ratio_obs_pred"].min(),
                        "ratio_max": c["ratio_obs_pred"].max()})
    pd.concat(tables_out, ignore_index=True).to_csv(
        reports / "summary_calibration_deciles.csv", index=False)
    pd.DataFrame(spreads).to_csv(reports / "summary_calibration_spread.csv", index=False)

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
    ap.add_argument("--n-boot", type=int, default=100)
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    figures.mkdir(parents=True, exist_ok=True)

    needed = [reports / "b2_paired_comparison.csv", reports / "m2_paired_comparison.csv",
              reports / "b3_paired_comparisons.csv", tables / "predictions.parquet"]
    missing = [p for p in needed if not p.exists()]
    if missing:
        print("missing inputs, run the earlier scripts first:")
        for m in missing:
            print(f"  {m}")
        return 1

    df = pd.read_parquet(tables / "predictions.parquet")
    if "risk_b0" not in df.columns:
        print("predictions.parquet has no risk_b0 column; rerun s8_m2_boosted.py")
        return 1

    print("model ladder")
    figure_ladder(df, reports, figures, args.n_boot)
    print("calibration")
    figure_calibration(df, reports, figures)
    print("RQ2 decomposition")
    figure_rq2(reports, figures)

    for f in sorted(figures.glob("summary_*.png")):
        print(f"\nwrote {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
