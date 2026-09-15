"""B0 and B1 on landmark rows, evaluated across the rolling origin folds.

From here on every model in the ladder lives in the landmark frame: one row per
spell per landmark, predicting P(failure within 30 days | alive at the landmark).
This is the quantity the decision layer consumes, and putting all six models on
identical rows with identical metrics is what makes the comparison in RQ2 mean
anything.

  B0  age alone. The 30 day risk from a drive's power on hours, ignoring which
      model it is. This is the floor.

  B1  drive model and age. The bar that SMART telemetry has to clear, and the
      subject of pre-committed expectation E2.

B1 is estimated nonparametrically rather than as a Cox model, which departs from
DESIGN.md section 7 as originally written. With two predictors and millions of
rows a Cox model buys nothing over the direct estimate and imposes a
proportional hazards assumption that B0 already suggests will not hold. The
nonparametric version is exact and strictly harder to beat, which is the point:
a weak B1 would let E2 pass trivially and prove nothing.

A recalibration step was tried here and rejected; the code and its output are kept
as evidence rather than deleted. See the note printed with the b1_recalibration
table, and the amendment log in DESIGN.md dated 2026-09-15.

Estimation, per drive model and half year age band:

    hazard = events / exposure                      events over drive days at risk
    risk   = 1 - exp(-hazard * 30)

with exposure being the days each row is actually observed inside the horizon, so
drives removed part way through a window contribute the time they were at risk
rather than being scored as survivors.

Thin cells are shrunk toward the model level rate, and thin model rates toward
the global rate, by a Gamma-Poisson posterior with prior strength expressed in
events. Without shrinkage, a cell holding two drives and one failure would
predict a catastrophic hazard and wreck calibration.

Usage:

    py scripts/s4_b1_baseline.py --tables data/tables --reports reports --figures figures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluation as ev  # noqa: E402

BAND_HOURS = 4380          # half a year of power on time
HORIZON_DAYS = 30
PRIOR_EVENTS = 20.0        # shrinkage strength, in events

# Rolling origin folds, DESIGN.md section 6. Training is everything strictly
# before the test quarter.
# name, validation start, test start, test end. Training is everything before the
# validation start; the validation quarter is used only to fit the recalibration
# factor, never to choose between models.
FOLDS = [
    ("fold1", "2025-04-01", "2025-07-01", "2025-10-01"),
    ("fold2", "2025-07-01", "2025-10-01", "2026-01-01"),
    ("fold3", "2025-10-01", "2026-01-01", "2026-04-01"),
]


def shrink(events: pd.Series, exposure: pd.Series, parent_hazard) -> pd.Series:
    """Gamma-Poisson posterior mean hazard, shrunk toward a parent rate.

    Prior strength is PRIOR_EVENTS events' worth of information, so a cell with
    far more than that is essentially unshrunk and a cell with far fewer falls
    back to its parent.
    """
    prior_exposure = PRIOR_EVENTS / np.maximum(parent_hazard, 1e-12)
    return (events + PRIOR_EVENTS) / (exposure + prior_exposure)


def fit_predict(train: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    """Fit B0 and B1 on the training rows and score the test rows."""
    glob_events = train["fail"].sum()
    glob_expo = train["expo"].sum()
    glob_h = glob_events / glob_expo if glob_expo > 0 else 0.0

    # B0: age band only
    b0 = train.groupby("age_band", as_index=False).agg(fail=("fail", "sum"),
                                                       expo=("expo", "sum"))
    b0["hazard"] = shrink(b0["fail"], b0["expo"], glob_h)

    # B1: model level rate, shrunk toward global
    by_model = train.groupby("model", as_index=False).agg(fail=("fail", "sum"),
                                                          expo=("expo", "sum"))
    by_model["hazard_model"] = shrink(by_model["fail"], by_model["expo"], glob_h)

    # B1: model and age band, shrunk toward that model's rate
    cell = train.groupby(["model", "age_band"], as_index=False).agg(fail=("fail", "sum"),
                                                                    expo=("expo", "sum"))
    cell = cell.merge(by_model[["model", "hazard_model"]], on="model", how="left")
    cell["hazard"] = shrink(cell["fail"], cell["expo"], cell["hazard_model"])

    out = test.merge(b0[["age_band", "hazard"]].rename(columns={"hazard": "h_b0"}),
                     on="age_band", how="left")
    out = out.merge(cell[["model", "age_band", "hazard"]].rename(columns={"hazard": "h_b1"}),
                    on=["model", "age_band"], how="left")
    out = out.merge(by_model[["model", "hazard_model"]], on="model", how="left")

    # Fallbacks for combinations the training window never saw: an unseen cell
    # falls back to its model rate, an entirely unseen model to the global rate.
    out["h_b0"] = out["h_b0"].fillna(glob_h)
    out["h_b1"] = out["h_b1"].fillna(out["hazard_model"]).fillna(glob_h)

    for col, h in (("risk_b0", "h_b0"), ("risk_b1", "h_b1")):
        out[col] = 1.0 - np.exp(-out[h] * HORIZON_DAYS)
    return out


def load_window(con, lo: str | None, hi: str) -> pd.DataFrame:
    cols = f"""
        spell_key, model,
        CAST(poh_at_landmark / {BAND_HOURS} AS INTEGER) * {BAND_HOURS} AS age_band,
        t_days, status,
        fail_{HORIZON_DAYS}d AS fail,
        -- exposure inside the horizon: the days this row was actually at risk
        LEAST(t_days, {HORIZON_DAYS})                   AS expo
    """
    where = f"landmark < DATE '{hi}'"
    if lo is not None:
        where += f" AND landmark >= DATE '{lo}'"
    return con.execute(f"SELECT {cols} FROM landmarks WHERE {where}").df()


def plot_calibration(cal_b0: pd.DataFrame, cal_b1: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6.5, 6))
    lim = max(cal_b1["mean_predicted"].max(), cal_b1["observed_ipcw"].max()) * 1.15
    ax.plot([0, lim], [0, lim], color="grey", lw=1, ls="--", label="perfect calibration")
    ax.plot(cal_b0["mean_predicted"], cal_b0["observed_ipcw"], "o-", ms=5, lw=1.4,
            color="#7A7A7A", label="B0, age only")
    ax.plot(cal_b1["mean_predicted"], cal_b1["observed_ipcw"], "o-", ms=5, lw=1.6,
            color="#1F3A5F", label="B1, model and age")
    ax.set_xlabel("mean predicted 30 day risk")
    ax.set_ylabel("observed 30 day risk (IPCW)")
    ax.set_title("Calibration by predicted risk decile, pooled test folds")
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
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
    ap.add_argument("--n-boot", type=int, default=100)
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    reports.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(
        f"CREATE OR REPLACE VIEW landmarks AS "
        f"SELECT * FROM read_parquet('{(tables / 'landmarks.parquet').as_posix()}')"
    )

    rows, pooled, cal_rows = [], [], []
    for name, val_start, test_start, test_end in FOLDS:
        print(f"\n{name}: train < {val_start}, validate {val_start} to {test_start}, "
              f"test {test_start} to {test_end}")
        train = load_window(con, None, val_start)
        val = load_window(con, val_start, test_start)
        full_train = load_window(con, None, test_start)
        test = load_window(con, test_start, test_end)
        print(f"  {len(train):,} train rows ({int(train['fail'].sum()):,} events), "
              f"{len(val):,} validation rows ({int(val['fail'].sum()):,} events), "
              f"{len(test):,} test rows ({int(test['fail'].sum()):,} events)")
        if test.empty or test["fail"].sum() == 0:
            print("  no test events, skipping fold")
            continue

        # Fit the recalibration factor on the validation quarter, using a model
        # that has never seen it. The factor corrects the overall hazard level
        # only, so ranking and therefore AUC are unchanged by construction.
        val_scored = fit_predict(train, val)
        factors = {}
        for col, hcol in (("risk_b0", "h_b0"), ("risk_b1", "h_b1")):
            expected = float((val_scored[hcol] * val_scored["expo"]).sum())
            observed = float(val_scored["fail"].sum())
            factors[col] = ev.recalibration_factor(observed, expected)
        print(f"  recalibration factors: B0 {factors['risk_b0']:.3f}, "
              f"B1 {factors['risk_b1']:.3f}  (below 1.0 means over-prediction)")
        cal_rows.append({"fold": name,
                         "train_rate_per_1000": 1000 * train["fail"].sum() / len(train),
                         "test_rate_per_1000": 1000 * test["fail"].sum() / len(test),
                         "factor_b0": factors["risk_b0"], "factor_b1": factors["risk_b1"]})

        # Refit on the full training window, then apply the factor to the test rows.
        scored = fit_predict(full_train, test)
        for col, hcol in (("risk_b0", "h_b0"), ("risk_b1", "h_b1")):
            scored[col + "_cal"] = 1.0 - np.exp(
                -scored[hcol] * factors[col] * HORIZON_DAYS
            )
        pooled.append(scored)

        for label, col in (("B0 age only", "risk_b0"),
                           ("B1 model and age", "risk_b1"),
                           ("B0 recalibrated", "risk_b0_cal"),
                           ("B1 recalibrated", "risk_b1_cal")):
            m = ev.evaluate(scored, col)
            m.update({"fold": name, "model": label})
            rows.append(m)
            print(f"  {label:18s} brier={m['ipcw_brier']:.6f}  auc={m['ipcw_auc']:.4f}")

    if not rows:
        print("no folds produced results")
        return 1

    per_fold = pd.DataFrame(rows)[
        ["fold", "model", "n_rows", "n_events", "mean_predicted", "ipcw_brier", "ipcw_auc"]
    ]
    per_fold.to_csv(reports / "b1_metrics_by_fold.csv", index=False)
    print("\n--- b1_metrics_by_fold ---")
    with pd.option_context("display.width", 200):
        print(per_fold.to_string(index=False))

    all_test = pd.concat(pooled, ignore_index=True)
    print(f"\npooled over folds: {len(all_test):,} rows, "
          f"{int(all_test['fail'].sum()):,} events")

    cal = pd.DataFrame(cal_rows)
    cal.to_csv(reports / "b1_recalibration.csv", index=False)
    print("\n--- b1_recalibration ---")
    print("REJECTED APPROACH, retained as evidence. The factor is fitted on the quarter")
    print("before each test quarter. Compare the two rate columns: the fleet's failure")
    print("rate oscillates without a trend, so the preceding quarter does not predict")
    print("the next one. In fold 1 the factor corrects downward while the test quarter")
    print("runs hotter than validation. Measured effect on decile calibration ratios:")
    print("0.74 to 1.05 without the correction, 0.80 to 1.51 with it. The correction is")
    print("the same magnitude as the noise it is estimated from. Headline metrics below")
    print("therefore use raw predictions; level uncertainty is swept in the decision")
    print("layer instead (DESIGN.md section 10).")
    with pd.option_context("display.width", 200):
        print(cal.to_string(index=False))

    print(f"\nbootstrapping at spell level, {args.n_boot} resamples")
    summary = []
    for label, col in (("B0 age only", "risk_b0"), ("B1 model and age", "risk_b1"),
                       ("B0 recalibrated", "risk_b0_cal"),
                       ("B1 recalibrated", "risk_b1_cal")):
        m = ev.evaluate(all_test, col)
        lo, hi = ev.bootstrap_metric(all_test, col, "ipcw_brier", n_boot=args.n_boot)
        alo, ahi = ev.bootstrap_metric(all_test, col, "ipcw_auc", n_boot=args.n_boot)
        summary.append({
            "model": label,
            "ipcw_brier": m["ipcw_brier"], "brier_lo": lo, "brier_hi": hi,
            "ipcw_auc": m["ipcw_auc"], "auc_lo": alo, "auc_hi": ahi,
            "mean_predicted": m["mean_predicted"],
        })
    summ = pd.DataFrame(summary)
    summ.to_csv(reports / "b1_metrics_pooled.csv", index=False)
    print("\n--- b1_metrics_pooled ---")
    with pd.option_context("display.width", 220):
        print(summ.to_string(index=False))

    print("\n--- b1_paired_comparison ---")
    print("Both models are scored inside each bootstrap resample and the difference")
    print("taken, which removes the variation they share. This is far more powerful")
    print("than comparing the marginal intervals above, and is the test E2 uses.")
    pairs = []
    for metric in ("ipcw_auc", "ipcw_brier"):
        r = ev.bootstrap_paired_difference(all_test, "risk_b0", "risk_b1",
                                           metric, n_boot=args.n_boot)
        r.update({"metric": metric, "comparison": "B1 minus B0"})
        pairs.append(r)
    pair_df = pd.DataFrame(pairs)[
        ["comparison", "metric", "difference", "lo", "hi", "excludes_zero"]
    ]
    pair_df.to_csv(reports / "b1_paired_comparison.csv", index=False)
    with pd.option_context("display.width", 200):
        print(pair_df.to_string(index=False))

    cal_b0 = ev.calibration_table(all_test["risk_b0"].to_numpy(),
                                  all_test["t_days"].to_numpy(),
                                  all_test["status"].to_numpy())
    cal_b1 = ev.calibration_table(all_test["risk_b1"].to_numpy(),
                                  all_test["t_days"].to_numpy(),
                                  all_test["status"].to_numpy())
    cal_b0.to_csv(reports / "b1_calibration_b0.csv", index=False)
    cal_b1.to_csv(reports / "b1_calibration_b1.csv", index=False)
    print("\n--- b1_calibration_b1 ---")
    print("ratio_obs_pred near 1.0 in every decile means the predicted probabilities")
    print("can be fed to a cost calculation. Systematic drift away from 1.0 means they")
    print("rank correctly but are not usable as probabilities.")
    with pd.option_context("display.width", 200):
        print(cal_b1.to_string(index=False))

    plot_calibration(cal_b0, cal_b1, figures / "b1_calibration.png")
    print(f"\nfigure written to {figures / 'b1_calibration.png'}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
