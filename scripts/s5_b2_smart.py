"""B2: do SMART attributes predict failure beyond drive model and age?

This is the model expectation E2 was written for. E2 is declared in DESIGN.md
section 9 and was locked before this script existed.

Specification. B1 estimates a hazard for each drive model and half year age band.
B2 multiplies that baseline by the effect of SMART telemetry:

    log hazard_i = log h_B1(model_i, age_i) + x_i' beta

A piecewise exponential (Poisson) hazard model, fitted by maximum likelihood with
log(exposure * h_B1) as an offset. Three properties make this the right shape
here rather than a workaround:

  * B2 nests B1 exactly. Set beta to zero and B2 is B1. The E2 comparison is
    therefore a test on beta rather than a comparison of two unrelated fits.
  * Stratification by drive model and age arrives through the offset instead of
    fifty dummy columns, so the design matrix holds only the SMART features.
  * The exposure offset handles censoring inside the 30 day window exactly, the
    same way B1 does, rather than scoring a drive removed on day 12 as a
    survivor.

It also means no subsampling. Newton-Raphson on fourteen parameters over eight
million rows costs a few seconds per iteration, so every model in the ladder is
fit on all of the data.

Standard errors are clustered by spell. A spell contributes many correlated
landmark rows, and naive standard errors would be far too small.

Cohort A per DESIGN.md section 4: all four manufacturers, universal SMART
attributes only, `TOSHIBA MG08ACA16TEY` excluded because it does not report
attribute 197.

Usage:

    py scripts/s5_b2_smart.py --tables data/tables --reports reports --figures figures
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
from s4_b1_baseline import BAND_HOURS, FOLDS, HORIZON_DAYS, fit_predict  # noqa: E402

# Cohort A, universal attributes. Excluded from the design matrix: 9 is the time
# scale, and 187, 188, 190, 241, 242 are Seagate only and belong to B3.
COUNT_COLS = [5, 12, 193, 197, 198]   # accumulating counters, heavily skewed
LEVEL_COLS = [194]                    # temperature, a level not a counter
DELTA_COLS = [5, 12, 193, 197, 198]

# Non-zero indicators for 5, 197 and 198 were in the first version of this model
# and removed: log1p(x) is already zero exactly when x is zero, so the indicator
# is very nearly collinear with it. The first fit showed the consequence, a zero
# standard error and a NaN z statistic on nonzero_smart_197, which is a singular
# design matrix reporting itself.

# L2 penalties searched on the validation quarter. The intercept is never
# penalised: it carries the overall hazard level, which is not a quantity we want
# shrunk toward zero.
L2_GRID = [1e-4, 1e-3, 1e-2, 1e-1, 1.0, 10.0]

EXCLUDE_MODELS = ("TOSHIBA MG08ACA16TEY",)


def build_features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Design matrix for the SMART effects.

    Counters are log1p transformed because they span several orders of magnitude
    and a raw scale would let a handful of extreme drives dominate the fit. Only
    increases in the 30 day change are kept: a counter going down is a firmware
    quirk rather than a drive deteriorating.
    """
    cols, names = [], []

    for n in COUNT_COLS:
        cols.append(np.log1p(np.maximum(df[f"smart_{n}"].fillna(0).to_numpy(float), 0)))
        names.append(f"log1p_smart_{n}")

    for n in LEVEL_COLS:
        v = df[f"smart_{n}"].to_numpy(float)
        cols.append(np.nan_to_num(v, nan=float(np.nanmedian(v))))
        names.append(f"smart_{n}")

    for n in DELTA_COLS:
        d = df[f"d30_smart_{n}"].fillna(0).to_numpy(float)
        cols.append(np.log1p(np.maximum(d, 0)))
        names.append(f"log1p_rise_smart_{n}")

    X = np.column_stack(cols)
    return X, names


def drop_redundant(X: np.ndarray, names: list[str], keep=None, tol: float = 1e-6):
    """Drop columns that duplicate an earlier column, and constant columns.

    Some SMART attributes are the same underlying value under two attribute
    numbers on a given firmware. On Seagate drives 190 (airflow temperature
    difference) and 194 (temperature) are identical, as are 197 (current pending
    sectors) and 198 (offline uncorrectable). Correlations are exactly 1.0000, not
    approximately, so the design matrix is genuinely rank deficient and the
    affected coefficients are not identified.

    Detection is per cohort, because the duplication is a firmware property: 197
    and 198 differ across the full fleet and coincide within Seagate. The mask is
    computed on the training rows and reused for validation and test, so the
    design stays identical across windows.

    Returns (X_kept, names_kept, keep_mask, dropped) where `dropped` lists each
    removed column with the column it duplicates.
    """
    dropped = []
    if keep is None:
        p = X.shape[1]
        keep = np.ones(p, dtype=bool)
        sd = X.std(axis=0)
        for j in range(p):
            if sd[j] < 1e-12:
                keep[j] = False
                dropped.append((names[j], "constant"))
                continue
            for i in range(j):
                if not keep[i] or sd[i] < 1e-12:
                    continue
                r = float(np.corrcoef(X[:, i], X[:, j])[0, 1])
                if abs(r) > 1.0 - tol:
                    keep[j] = False
                    dropped.append((names[j], f"duplicate of {names[i]} (r={r:+.4f})"))
                    break
    return X[:, keep], [n for n, k in zip(names, keep) if k], keep, dropped


def standardise(X: np.ndarray, mu=None, sd=None):
    if mu is None:
        mu, sd = X.mean(axis=0), X.std(axis=0)
        sd = np.where(sd < 1e-12, 1.0, sd)
    return (X - mu) / sd, mu, sd


def _penalised_loglik(X, y, offset, beta, lam):
    """Poisson log likelihood minus an L2 penalty. The intercept is not penalised."""
    eta = X @ beta
    if not np.all(np.isfinite(eta)) or np.max(eta) > 30:
        return -np.inf
    return float(np.sum(y * eta - offset * np.exp(eta)) - lam * np.sum(beta[1:] ** 2))


def fit_poisson_offset(X: np.ndarray, y: np.ndarray, offset: np.ndarray,
                       lam: float = 1e-3, max_iter: int = 100, tol: float = 1e-8):
    """Penalised Newton-Raphson for a Poisson log-link model with a fixed offset.

    Objective, dropping terms free of beta:

        l(beta) = sum_i [ y_i * x_i'beta - offset_i * exp(x_i'beta) ]
                  - lam * ||beta_slopes||^2

    with gradient X'(y - mu) - 2*lam*beta and Hessian -X' diag(mu) X - 2*lam*I.

    Returns (beta, iterations, loglik, converged). The caller must check
    `converged`. The first version of this function did not report it, ran to its
    iteration cap on every fold, and produced a divergent fit whose coefficients
    still looked plausible enough to be mistaken for a result.

    Two defects in that version, both fixed here. The line search compared each
    candidate against a stale likelihood and could accept a step that made things
    worse. And the diagonal term was 1e-8, numerical noise rather than
    regularisation, which left a nearly singular design matrix free to send the
    intercept toward minus infinity while the slopes blew up.

    No clipping of eta. Clipping hides divergence instead of stopping it; the
    line search rejects any step producing a non-finite or extreme objective.
    """
    n, p = X.shape
    beta = np.zeros(p)
    ll = _penalised_loglik(X, y, offset, beta, lam)

    for it in range(max_iter):
        eta = X @ beta
        mu = offset * np.exp(eta)
        grad = X.T @ (y - mu)
        grad[1:] -= 2.0 * lam * beta[1:]

        H = X.T @ (X * mu[:, None])
        H[np.diag_indices_from(H)] += 2.0 * lam
        H[0, 0] -= 2.0 * lam          # intercept is unpenalised
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, grad, rcond=None)[0]

        # Backtracking line search against the CURRENT objective, so a step is
        # only taken when it genuinely improves the fit.
        t, improved = 1.0, False
        for _ in range(40):
            cand_ll = _penalised_loglik(X, y, offset, beta + t * step, lam)
            if np.isfinite(cand_ll) and cand_ll > ll:
                improved = True
                break
            t *= 0.5
        if not improved:
            return beta, it + 1, ll, True      # no ascent direction left: at the optimum

        beta = beta + t * step
        new_ll = cand_ll
        if abs(new_ll - ll) < tol * (abs(ll) + 1):
            return beta, it + 1, new_ll, True
        ll = new_ll

    return beta, max_iter, ll, False


def clustered_se(X: np.ndarray, y: np.ndarray, offset: np.ndarray,
                 beta: np.ndarray, clusters: np.ndarray) -> np.ndarray:
    """Sandwich standard errors clustered by spell.

    Landmark rows from one spell are strongly correlated, so the model based
    standard errors would be far too small and every coefficient would look
    significant.
    """
    eta = np.clip(X @ beta, -30, 30)
    mu = offset * np.exp(eta)
    bread = np.linalg.pinv(X.T @ (X * mu[:, None]))

    resid = (y - mu)[:, None] * X
    uniq, inverse = np.unique(clusters, return_inverse=True)
    summed = np.zeros((len(uniq), X.shape[1]))
    np.add.at(summed, inverse, resid)
    meat = summed.T @ summed

    cov = bread @ meat @ bread
    return np.sqrt(np.maximum(np.diag(cov), 0))


def load_window(con, lo: str | None, hi: str, smart_cols: list[str]) -> pd.DataFrame:
    excl = ", ".join("'" + m.replace("'", "''") + "'" for m in EXCLUDE_MODELS)
    where = [f"landmark < DATE '{hi}'", f"model NOT IN ({excl})"]
    if lo is not None:
        where.append(f"landmark >= DATE '{lo}'")
    return con.execute(
        f"""
        SELECT
            spell_key, model,
            CAST(poh_at_landmark / {BAND_HOURS} AS INTEGER) * {BAND_HOURS} AS age_band,
            t_days, status,
            fail_{HORIZON_DAYS}d AS fail,
            LEAST(t_days, {HORIZON_DAYS}) AS expo,
            {", ".join(smart_cols)}
        FROM landmarks
        WHERE {" AND ".join(where)}
        """
    ).df()


def plot_coefficients(coef: pd.DataFrame, path: Path) -> None:
    d = coef.sort_values("hazard_ratio")
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(d) + 2))
    y = np.arange(len(d))
    ax.errorbar(d["hazard_ratio"], y,
                xerr=[d["hazard_ratio"] - d["hr_lo"], d["hr_hi"] - d["hazard_ratio"]],
                fmt="o", ms=5, lw=1.4, color="#1F3A5F", ecolor="#7A7A7A", capsize=3)
    ax.axvline(1.0, color="#B3412C", lw=1, ls="--")
    ax.set_yticks(y)
    ax.set_yticklabels(d["feature"], fontsize=8)
    ax.set_xscale("log")
    ax.set_xlabel("hazard ratio per standard deviation (log scale)")
    ax.set_title("B2 SMART effects, fold 3, clustered by spell")
    ax.grid(alpha=0.25, lw=0.6, axis="x")
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

    smart_cols = ([f"smart_{n}" for n in sorted(set(COUNT_COLS + LEVEL_COLS))]
                  + [f"d30_smart_{n}" for n in DELTA_COLS])

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(
        f"CREATE OR REPLACE VIEW landmarks AS "
        f"SELECT * FROM read_parquet('{(tables / 'landmarks.parquet').as_posix()}')"
    )

    def design(df, b1, mu=None, sd=None, keep=None):
        Xr, names = build_features(df)
        Xr, names, keep, dropped = drop_redundant(Xr, names, keep)
        if dropped:
            for nm, why in dropped:
                print(f"    dropping {nm}: {why}")
        Xs, mu, sd = standardise(Xr, mu, sd)
        X = np.column_stack([np.ones(len(Xs)), Xs])
        off = (np.maximum(b1["h_b1"].to_numpy(float), 1e-12)
               * np.maximum(df["expo"].to_numpy(float), 1e-6))
        return X, off, df["fail"].to_numpy(float), names, mu, sd, keep

    rows, pooled, coef_last, lam_rows = [], [], None, []
    for name, val_start, test_start, test_end in FOLDS:
        print(f"\n{name}: train < {val_start}, validate to {test_start}, "
              f"test {test_start} to {test_end}")
        inner = load_window(con, None, val_start, smart_cols)
        val = load_window(con, val_start, test_start, smart_cols)
        train = load_window(con, None, test_start, smart_cols)
        test = load_window(con, test_start, test_end, smart_cols)
        print(f"  {len(train):,} train rows ({int(train['fail'].sum()):,} events), "
              f"{len(test):,} test rows ({int(test['fail'].sum()):,} events)")
        if test.empty or test["fail"].sum() == 0:
            continue

        # Choose the L2 penalty on the validation quarter, using a model fitted
        # only on data before it. The penalty is a hyperparameter, so it must not
        # be chosen on the test quarter.
        Xi, oi, yi, names, mu, sd, keep = design(inner, fit_predict(inner, inner))
        Xv, ov, yv, _, _, _, _ = design(val, fit_predict(inner, val), mu, sd, keep)
        best = None
        for lam in L2_GRID:
            b, it, _, ok = fit_poisson_offset(Xi, yi, oi, lam=lam)
            vll = _penalised_loglik(Xv, yv, ov, b, 0.0) / len(yv)
            print(f"    lambda {lam:<7g} converged={ok} iters={it:<3d} "
                  f"validation loglik/row {vll:.6e}")
            if ok and (best is None or vll > best[1]):
                best = (lam, vll)
        if best is None:
            print("  no penalty converged on the inner fit; skipping fold")
            continue
        lam = best[0]
        print(f"  selected lambda {lam:g}")
        lam_rows.append({"fold": name, "lambda": lam, "val_loglik_per_row": best[1]})

        # B1 baseline hazards for both windows. Training rows are scored by the
        # same fit they came from, which is how an offset model works: the SMART
        # coefficients describe deviation from that baseline.
        b1_train = fit_predict(train, train)
        b1_test = fit_predict(train, test)

        Xtr, off_tr, y_tr, names, mu, sd, keep = design(train, b1_train)

        cond = np.linalg.cond(Xtr.T @ Xtr)
        print(f"  design matrix condition number {cond:.3e}")
        if cond > 1e10:
            print("  WARNING: design matrix is near singular, coefficients are not "
                  "individually interpretable")

        beta, iters, ll, converged = fit_poisson_offset(Xtr, y_tr, off_tr, lam=lam)
        print(f"  Poisson fit: converged={converged} in {iters} Newton steps, "
              f"loglik {ll:,.1f}")
        if not converged:
            print("  ABORTING: the fit did not converge, so no result from this fold "
                  "is reportable.")
            return 1

        se = clustered_se(Xtr, y_tr, off_tr, beta, train["spell_key"].to_numpy())
        coef = pd.DataFrame({
            "feature": ["intercept"] + names,
            "beta": beta, "se": se,
            "z": beta / np.where(se > 0, se, np.nan),
        })
        coef["hazard_ratio"] = np.exp(coef["beta"])
        coef["hr_lo"] = np.exp(coef["beta"] - 1.96 * coef["se"])
        coef["hr_hi"] = np.exp(coef["beta"] + 1.96 * coef["se"])
        coef["fold"] = name
        coef_last = coef

        Xte, _, _, _, _, _ = design(test, b1_test, mu, sd, keep)[:6]
        h_b2 = np.maximum(b1_test["h_b1"].to_numpy(float), 1e-12) * \
            np.exp(np.clip(Xte @ beta, -30, 30))

        scored = b1_test.copy()
        scored["risk_b2"] = 1.0 - np.exp(-h_b2 * HORIZON_DAYS)
        pooled.append(scored)

        for label, col in (("B1 model and age", "risk_b1"), ("B2 plus SMART", "risk_b2")):
            m = ev.evaluate(scored, col)
            m.update({"fold": name, "model": label})
            rows.append(m)
            print(f"  {label:18s} brier={m['ipcw_brier']:.6f}  auc={m['ipcw_auc']:.4f}")

    if not rows:
        print("no folds produced results")
        return 1

    per_fold = pd.DataFrame(rows)[["fold", "model", "n_rows", "n_events",
                                   "mean_predicted", "ipcw_brier", "ipcw_auc"]]
    per_fold.to_csv(reports / "b2_metrics_by_fold.csv", index=False)
    print("\n--- b2_metrics_by_fold ---")
    with pd.option_context("display.width", 200):
        print(per_fold.to_string(index=False))

    if lam_rows:
        pd.DataFrame(lam_rows).to_csv(reports / "b2_penalty_selection.csv", index=False)

    if coef_last is not None:
        coef_last.to_csv(reports / "b2_coefficients.csv", index=False)
        print("\n--- b2_coefficients, final fold ---")
        print("Hazard ratio per standard deviation of the feature, holding drive model")
        print("and age fixed through the B1 offset. Standard errors clustered by spell.")
        with pd.option_context("display.width", 200, "display.float_format", "{:.4f}".format):
            print(coef_last[["feature", "hazard_ratio", "hr_lo", "hr_hi", "z"]]
                  .to_string(index=False))
        plot_coefficients(coef_last[coef_last["feature"] != "intercept"],
                          figures / "b2_coefficients.png")

    all_test = pd.concat(pooled, ignore_index=True)
    print(f"\npooled: {len(all_test):,} rows, {int(all_test['fail'].sum()):,} events")

    print(f"bootstrapping at spell level, {args.n_boot} resamples")
    pairs = []
    for metric in ("ipcw_auc", "ipcw_brier"):
        r = ev.bootstrap_paired_difference(all_test, "risk_b1", "risk_b2",
                                           metric, n_boot=args.n_boot)
        r.update({"metric": metric, "comparison": "B2 minus B1"})
        pairs.append(r)
    pair_df = pd.DataFrame(pairs)[["comparison", "metric", "difference",
                                   "lo", "hi", "excludes_zero"]]
    pair_df.to_csv(reports / "b2_paired_comparison.csv", index=False)
    print("\n--- b2_paired_comparison ---")
    with pd.option_context("display.width", 200):
        print(pair_df.to_string(index=False))

    cal = ev.calibration_table(all_test["risk_b2"].to_numpy(),
                               all_test["t_days"].to_numpy(),
                               all_test["status"].to_numpy())
    cal.to_csv(reports / "b2_calibration.csv", index=False)
    print("\n--- b2_calibration ---")
    with pd.option_context("display.width", 200):
        print(cal.to_string(index=False))

    # E2 verdict, against the criterion locked in DESIGN.md before this ran.
    auc_r = next(p for p in pairs if p["metric"] == "ipcw_auc")
    bri_r = next(p for p in pairs if p["metric"] == "ipcw_brier")
    auc_ok = auc_r["difference"] > 0 and auc_r["excludes_zero"]
    bri_ok = bri_r["difference"] < 0 and bri_r["excludes_zero"]
    b1_cal = pd.read_csv(reports / "b1_calibration_b1.csv") \
        if (reports / "b1_calibration_b1.csv").exists() else None
    spread_b2 = cal["ratio_obs_pred"].max() - cal["ratio_obs_pred"].min()
    spread_b1 = (b1_cal["ratio_obs_pred"].max() - b1_cal["ratio_obs_pred"].min()
                 if b1_cal is not None else np.nan)

    print("\n================ E2 verdict ================")
    print("Criterion locked in DESIGN.md section 9 before B2 was fitted:")
    print("  primary   IPCW AUC improves, paired interval excludes zero")
    print("  secondary IPCW Brier improves, paired interval excludes zero")
    print("  constraint calibration does not degrade")
    print(f"\n  AUC   {auc_r['difference']:+.5f}  [{auc_r['lo']:+.5f}, {auc_r['hi']:+.5f}]"
          f"   {'PASS' if auc_ok else 'FAIL'}")
    print(f"  Brier {bri_r['difference']:+.3e}  [{bri_r['lo']:+.3e}, {bri_r['hi']:+.3e}]"
          f"   {'PASS' if bri_ok else 'FAIL'}")
    print(f"  calibration decile spread: B1 {spread_b1:.3f}, B2 {spread_b2:.3f}")
    verdict = "E2 HOLDS" if (auc_ok and bri_ok) else "E2 FAILS"
    print(f"\n  {verdict}")
    if not (auc_ok and bri_ok):
        print("  Per DESIGN.md section 9 this goes in the first paragraph of the README.")

    # ---------------------------------------------------------------- oracle
    # DIAGNOSTIC ONLY, NOT A MODEL. Each model's hazard is rescaled by a single
    # constant chosen on the test rows themselves, so that predicted and observed
    # event counts match exactly. No model could know this constant in advance;
    # it uses the answer. The question it settles is narrow and worth settling:
    # is B2's failure purely one of overall level, or is its ranking also
    # producing shape error that a level correction cannot reach?
    #
    # Both models get the same treatment, so B2 is not handed an advantage B1 is
    # denied. AUC is unchanged by construction, since a common multiplier cannot
    # reorder anything.
    print("\n================ oracle level check (diagnostic, not a model) ================")
    print("Each model's hazard rescaled by one constant fitted on the test rows, so")
    print("predicted and observed counts match. This uses the answer and is reportable")
    print("only as a diagnostic. It isolates level error from shape error.")

    oracle_rows = []
    expo_te = all_test["expo"].to_numpy(float)
    observed = float(all_test["fail"].sum())
    for label, col in (("B1 model and age", "risk_b1"), ("B2 plus SMART", "risk_b2")):
        r = all_test[col].to_numpy(float)
        h = -np.log(np.clip(1.0 - r, 1e-15, 1.0)) / HORIZON_DAYS
        expected = float(np.sum(h * expo_te))
        c = observed / expected if expected > 0 else 1.0
        all_test[col + "_oracle"] = 1.0 - np.exp(-h * c * HORIZON_DAYS)
        m = ev.evaluate(all_test, col + "_oracle")
        cal_o = ev.calibration_table(all_test[col + "_oracle"].to_numpy(),
                                     all_test["t_days"].to_numpy(),
                                     all_test["status"].to_numpy())
        oracle_rows.append({
            "model": label, "level_factor": c,
            "ipcw_brier": m["ipcw_brier"], "ipcw_auc": m["ipcw_auc"],
            "calibration_spread": float(cal_o["ratio_obs_pred"].max()
                                        - cal_o["ratio_obs_pred"].min()),
        })
    oracle = pd.DataFrame(oracle_rows)
    oracle.to_csv(reports / "b2_oracle_level_check.csv", index=False)
    with pd.option_context("display.width", 200):
        print(oracle.to_string(index=False))

    o_pair = []
    for metric in ("ipcw_auc", "ipcw_brier"):
        r = ev.bootstrap_paired_difference(all_test, "risk_b1_oracle", "risk_b2_oracle",
                                           metric, n_boot=args.n_boot)
        r.update({"metric": metric, "comparison": "B2 minus B1, both at oracle level"})
        o_pair.append(r)
    o_pair_df = pd.DataFrame(o_pair)[["comparison", "metric", "difference",
                                      "lo", "hi", "excludes_zero"]]
    o_pair_df.to_csv(reports / "b2_oracle_paired.csv", index=False)
    print()
    with pd.option_context("display.width", 200):
        print(o_pair_df.to_string(index=False))

    ob = oracle.set_index("model")
    b2_better = (ob.loc["B2 plus SMART", "ipcw_brier"]
                 < ob.loc["B1 model and age", "ipcw_brier"])
    print("\n  Reading: if B2 now beats B1 on Brier and its calibration spread is")
    print("  comparable, the E2 failure was level only and the ranking is sound, which")
    print("  is the case section 10's level sweep is built for. If B2 still loses, the")
    print("  ranking itself carries shape error and B3 would inherit it.")
    print(f"\n  B2 beats B1 on Brier at oracle level: {b2_better}")

    pd.DataFrame([{
        "auc_difference": auc_r["difference"], "auc_lo": auc_r["lo"], "auc_hi": auc_r["hi"],
        "auc_pass": auc_ok,
        "brier_difference": bri_r["difference"], "brier_lo": bri_r["lo"],
        "brier_hi": bri_r["hi"], "brier_pass": bri_ok,
        "calibration_spread_b1": spread_b1, "calibration_spread_b2": spread_b2,
        "verdict": verdict,
        "oracle_b2_beats_b1_brier": b2_better,
    }]).to_csv(reports / "b2_e2_verdict.csv", index=False)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
