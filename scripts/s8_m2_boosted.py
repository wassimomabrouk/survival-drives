"""M2: does nonlinearity buy anything beyond B2's log-linear form?

Pre-committed expectation E4, locked in DESIGN.md before this script existed.

B2 models the hazard as

    log hazard_i = log h_B1(model_i, age_i) + x_i' beta

a log-linear function of the SMART features. M2 keeps everything about that
target and replaces only the functional form:

    log hazard_i = log h_B1(model_i, age_i) + f(x_i)

where f is a gradient boosted tree ensemble. Same rows, same features, same B1
offset, same exposure handling, same folds, same metrics. The only difference
between B2 and M2 is linear against trees, which is what makes the comparison a
clean test of whether interactions and nonlinear thresholds among SMART
attributes carry signal that the log-linear form misses.

Mechanically this works because XGBoost's Poisson objective accepts a per-row
`base_margin`, which enters the model as a fixed additive term on the log scale
before any tree contributes. Setting

    base_margin_i = log(h_B1_i * exposure_i)

makes the trees learn a multiplicative correction to the B1 baseline, exactly as
B2's coefficients do, and handles censoring inside the horizon through the same
exposure term. No subsampling: all 7.5 million training rows are used.

Hyperparameters are chosen on the validation quarter, never on the test quarter.
The search is deliberately small, because the point is to test a functional form
rather than to win a tuning competition, and a large search on a flat surface
would mostly fit noise.

Usage:

    py scripts/s8_m2_boosted.py --tables data/tables --reports reports --figures figures
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
from s4_b1_baseline import FOLDS, HORIZON_DAYS, fit_predict  # noqa: E402
from s5_b2_smart import (  # noqa: E402
    L2_GRID, _penalised_loglik, build_features, drop_redundant, fit_poisson_offset,
    load_window, standardise,
)

try:
    import xgboost as xgb
except ImportError:  # pragma: no cover
    print("xgboost is required: py -m pip install xgboost")
    raise

# Small deliberately. The question is whether a nonlinear form helps at all, not
# which of forty configurations wins by a hair on a flat validation surface.
GRID = [
    {"max_depth": 3, "eta": 0.10, "min_child_weight": 50},
    {"max_depth": 4, "eta": 0.05, "min_child_weight": 100},
    {"max_depth": 6, "eta": 0.05, "min_child_weight": 200},
]
NUM_ROUNDS = 300
EARLY_STOPPING = 20


def offsets(df: pd.DataFrame, b1: pd.DataFrame) -> np.ndarray:
    return (np.maximum(b1["h_b1"].to_numpy(float), 1e-12)
            * np.maximum(df["expo"].to_numpy(float), 1e-6))


def to_dmatrix(X: np.ndarray, y: np.ndarray, off: np.ndarray, names: list[str]):
    d = xgb.DMatrix(X, label=y, feature_names=names)
    # base_margin enters on the log scale before any tree, so the ensemble learns
    # a correction to the B1 baseline rather than the hazard from scratch.
    d.set_base_margin(np.log(np.maximum(off, 1e-300)))
    return d


def fit_b2(Xtr, y_tr, off_tr, Xv, yv, ov):
    """B2 refitted here so both models are scored on exactly the same rows."""
    best = None
    for lam in L2_GRID:
        b, _, _, ok = fit_poisson_offset(Xtr, y_tr, off_tr, lam=lam)
        if not ok:
            continue
        vll = _penalised_loglik(Xv, yv, ov, b, 0.0) / len(yv)
        if best is None or vll > best[1]:
            best = (lam, vll, b)
    return (None, None) if best is None else (best[2], best[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--figures", default="figures")
    ap.add_argument("--n-boot", type=int, default=100)
    ap.add_argument("--nthread", type=int, default=0)
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    reports.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    smart_cols = ([f"smart_{n}" for n in (5, 12, 193, 194, 197, 198)]
                  + [f"d30_smart_{n}" for n in (5, 12, 193, 197, 198)])

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(
        f"CREATE OR REPLACE VIEW landmarks AS "
        f"SELECT * FROM read_parquet('{(tables / 'landmarks.parquet').as_posix()}')"
    )

    rows, pooled, tuning, imp_last = [], [], [], None
    for name, val_start, test_start, test_end in FOLDS:
        print(f"\n{name}: train < {test_start}, test {test_start} to {test_end}")
        inner = load_window(con, None, val_start, smart_cols)
        val = load_window(con, val_start, test_start, smart_cols)
        train = load_window(con, None, test_start, smart_cols)
        test = load_window(con, test_start, test_end, smart_cols)
        print(f"  {len(train):,} train rows ({int(train['fail'].sum()):,} events), "
              f"{len(test):,} test rows ({int(test['fail'].sum()):,} events)")
        if test.empty or test["fail"].sum() == 0:
            continue

        # Shared design: built once, used by both B2 and M2 so the only thing
        # differing between them is the functional form.
        Xi_raw, raw_names = build_features(inner)
        Xi_raw, names, keep, _ = drop_redundant(Xi_raw, raw_names)
        Xi, mu, sd = standardise(Xi_raw)
        oi, yi = offsets(inner, fit_predict(inner, inner)), inner["fail"].to_numpy(float)

        Xv = standardise(drop_redundant(build_features(val)[0], raw_names, keep)[0], mu, sd)[0]
        ov, yv = offsets(val, fit_predict(inner, val)), val["fail"].to_numpy(float)

        # Hyperparameters on the validation quarter, from a model that has not
        # seen it.
        di = to_dmatrix(Xi, yi, oi, names)
        dv = to_dmatrix(Xv, yv, ov, names)
        best = None
        for params in GRID:
            p = dict(params, objective="count:poisson", eval_metric="poisson-nloglik",
                     nthread=args.nthread, seed=0)
            bst = xgb.train(p, di, NUM_ROUNDS, evals=[(dv, "val")],
                            early_stopping_rounds=EARLY_STOPPING, verbose_eval=False)
            score = float(bst.best_score)
            print(f"    depth={params['max_depth']} eta={params['eta']:<5g} "
                  f"mcw={params['min_child_weight']:<4d} rounds={bst.best_iteration + 1:<4d} "
                  f"val nloglik {score:.8f}")
            if best is None or score < best[0]:
                best = (score, params, bst.best_iteration + 1)
        _, params, rounds = best
        print(f"  selected depth={params['max_depth']} eta={params['eta']:g} "
              f"rounds={rounds}")
        tuning.append({"fold": name, **params, "rounds": rounds,
                       "val_nloglik": best[0]})

        # Refit both models on the full training window.
        b1_train, b1_test = fit_predict(train, train), fit_predict(train, test)
        Xtr = standardise(drop_redundant(build_features(train)[0], raw_names, keep)[0], mu, sd)[0]
        Xte = standardise(drop_redundant(build_features(test)[0], raw_names, keep)[0], mu, sd)[0]
        off_tr, y_tr = offsets(train, b1_train), train["fail"].to_numpy(float)
        off_te = offsets(test, b1_test)

        beta, lam = fit_b2(Xtr, y_tr, off_tr, Xv, yv, ov)
        if beta is None:
            print("  B2 did not converge, aborting")
            return 1

        dtr = to_dmatrix(Xtr, y_tr, off_tr, names)
        p = dict(params, objective="count:poisson", nthread=args.nthread, seed=0)
        bst = xgb.train(p, dtr, rounds, verbose_eval=False)
        imp_last = bst.get_score(importance_type="gain")

        dte = xgb.DMatrix(Xte, feature_names=names)
        dte.set_base_margin(np.zeros(len(Xte)))   # margin excluded to recover f(x) alone
        f_x = bst.predict(dte, output_margin=True)

        scored = b1_test.copy()
        h_b1 = np.maximum(b1_test["h_b1"].to_numpy(float), 1e-12)
        scored["risk_b2"] = 1.0 - np.exp(
            -h_b1 * np.exp(np.clip(Xte @ beta, -30, 30)) * HORIZON_DAYS)
        scored["risk_m2"] = 1.0 - np.exp(
            -h_b1 * np.exp(np.clip(f_x, -30, 30)) * HORIZON_DAYS)
        pooled.append(scored)

        for label, col in (("B2 log-linear", "risk_b2"), ("M2 boosted trees", "risk_m2")):
            m = ev.evaluate(scored, col)
            m.update({"fold": name, "model": label})
            rows.append(m)
            print(f"    {label:17s} brier={m['ipcw_brier']:.6f}  auc={m['ipcw_auc']:.4f}")

    if not rows:
        print("no folds produced results")
        return 1

    per_fold = pd.DataFrame(rows)[["fold", "model", "n_rows", "n_events",
                                   "mean_predicted", "ipcw_brier", "ipcw_auc"]]
    per_fold.to_csv(reports / "m2_metrics_by_fold.csv", index=False)
    pd.DataFrame(tuning).to_csv(reports / "m2_tuning.csv", index=False)
    print("\n--- m2_metrics_by_fold ---")
    with pd.option_context("display.width", 200):
        print(per_fold.to_string(index=False))

    if imp_last:
        imp = (pd.Series(imp_last).sort_values(ascending=False)
               .rename("gain").reset_index().rename(columns={"index": "feature"}))
        imp.to_csv(reports / "m2_feature_importance.csv", index=False)
        print("\n--- m2_feature_importance, final fold, by gain ---")
        print("Gain is how much each feature improved the objective when split on.")
        print("It is not a hazard ratio and carries no direction.")
        with pd.option_context("display.width", 200, "display.float_format", "{:.1f}".format):
            print(imp.to_string(index=False))
        fig, ax = plt.subplots(figsize=(8, 0.42 * len(imp) + 1.5))
        ax.barh(imp["feature"][::-1], imp["gain"][::-1], color="#1F3A5F")
        ax.set_xlabel("total gain")
        ax.set_title("M2 feature importance, final fold")
        ax.grid(alpha=0.25, lw=0.6, axis="x")
        fig.tight_layout()
        fig.savefig(figures / "m2_feature_importance.png", dpi=150)
        plt.close(fig)

    all_test = pd.concat(pooled, ignore_index=True)
    print(f"\npooled: {len(all_test):,} rows, {int(all_test['fail'].sum()):,} events")

    # Oracle level, as for B2 and B3: one constant per model fitted on the test
    # rows. Diagnostic only, never a reported model.
    expo_te = all_test["expo"].to_numpy(float)
    observed = float(all_test["fail"].sum())
    for col in ("risk_b2", "risk_m2"):
        r = all_test[col].to_numpy(float)
        h = -np.log(np.clip(1.0 - r, 1e-15, 1.0)) / HORIZON_DAYS
        expected = float(np.sum(h * expo_te))
        c = observed / expected if expected > 0 else 1.0
        all_test[col + "_oracle"] = 1.0 - np.exp(-h * c * HORIZON_DAYS)

    print(f"bootstrapping at spell level, {args.n_boot} resamples")
    pairs = []
    for suffix, level in (("", "raw"), ("_oracle", "oracle")):
        for metric in ("ipcw_auc", "ipcw_brier"):
            r = ev.bootstrap_paired_difference(
                all_test, f"risk_b2{suffix}", f"risk_m2{suffix}",
                metric, n_boot=args.n_boot)
            r.update({"comparison": "M2 minus B2", "level": level, "metric": metric})
            pairs.append(r)
    pair_df = pd.DataFrame(pairs)[["comparison", "level", "metric", "difference",
                                   "lo", "hi", "excludes_zero"]]
    pair_df.to_csv(reports / "m2_paired_comparison.csv", index=False)
    print("\n--- m2_paired_comparison ---")
    with pd.option_context("display.width", 220):
        print(pair_df.to_string(index=False))

    cal = {}
    for label, col in (("B2", "risk_b2"), ("M2", "risk_m2")):
        c = ev.calibration_table(all_test[col].to_numpy(),
                                 all_test["t_days"].to_numpy(),
                                 all_test["status"].to_numpy())
        c.to_csv(reports / f"m2_calibration_{label.lower()}.csv", index=False)
        cal[label] = float(c["ratio_obs_pred"].max() - c["ratio_obs_pred"].min())
    print(f"\ncalibration decile spread: B2 {cal['B2']:.3f}, M2 {cal['M2']:.3f}")

    auc_r = next(p for p in pairs if p["level"] == "raw" and p["metric"] == "ipcw_auc")
    bri_r = next(p for p in pairs if p["level"] == "raw" and p["metric"] == "ipcw_brier")
    auc_ok = auc_r["difference"] > 0 and auc_r["excludes_zero"]

    print("\n================ E4 verdict ================")
    print("Criterion locked in DESIGN.md section 9 before M2 was fitted:")
    print("  primary   IPCW AUC improves, paired interval excludes zero")
    print("  secondary IPCW Brier reported raw and at oracle level, no threshold")
    print("  expected  a small margin, well under the +0.187 SMART bought over B1")
    print(f"\n  AUC, M2 minus B2   {auc_r['difference']:+.5f} "
          f"[{auc_r['lo']:+.5f}, {auc_r['hi']:+.5f}]  {'PASS' if auc_ok else 'FAIL'}")
    print(f"  Brier, M2 minus B2 {bri_r['difference']:+.3e} "
          f"[{bri_r['lo']:+.3e}, {bri_r['hi']:+.3e}]")
    verdict = "E4 HOLDS" if auc_ok else "E4 FAILS"
    print(f"\n  {verdict}")
    if auc_ok and auc_r["difference"] > 0.05:
        print("  NOTE: the gain is far larger than 'small'. Interactions matter more")
        print("  than expected, which is a surprise worth reporting as one.")
    elif not auc_ok:
        print("  Nonlinearity buys nothing here: B2's log-linear form already captures")
        print("  the signal in these features. That is a real finding, not a failure.")

    pd.DataFrame([{
        "auc_difference": auc_r["difference"], "auc_lo": auc_r["lo"],
        "auc_hi": auc_r["hi"], "auc_pass": auc_ok,
        "brier_difference": bri_r["difference"],
        "calibration_spread_b2": cal["B2"], "calibration_spread_m2": cal["M2"],
        "verdict": verdict,
    }]).to_csv(reports / "m2_e4_verdict.csv", index=False)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
