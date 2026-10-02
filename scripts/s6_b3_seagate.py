"""B3: do vendor specific SMART attributes add anything beyond the universal ones?

This is research question RQ2 and pre-committed expectation E3, both in DESIGN.md
and both locked before this script existed.

Section 0 established that attributes 187, 188, 190, 241 and 242 exist only on
Seagate firmware, while 5, 9, 12, 193, 194, 197 and 198 are reported by every
manufacturer in the fleet. Attributes 187 and 197 are among the most predictive
in the published drive failure literature, so the fleet wide model in B2 is
working without two of the five attributes usually considered canonical. This
script measures what that costs.

Three arms, all fitted on the SAME Seagate cohort so the comparison isolates the
attributes rather than the population:

  U     universal attributes only, the B2 feature set restricted to Seagate
  U+187 universal plus reported uncorrectable errors alone
  FULL  universal plus all five Seagate only attributes

U against FULL answers RQ2. U+187 against FULL tests the directional half of E3,
that 187 carries most of any gain while 188, 190, 241 and 242 contribute little.

Note that attribute 197 is in the universal set and therefore in every arm. It
cannot contribute anything incremental here, whatever the literature says about
its predictive value in general.

Everything else follows B2: a piecewise exponential hazard model with B1 as the
offset, the L2 penalty chosen on a validation quarter, standard errors clustered
by spell, and metrics reported both raw and at oracle level. The oracle rescales
each arm's hazard by one constant fitted on the test rows, which uses the answer
and is therefore a diagnostic only. It is carried over from B2 because B2 showed
a residual calibration shape problem that all these arms share, and reporting
only raw Brier would compare the arms on a metric contaminated by a defect common
to them.

Usage:

    py scripts/s6_b3_seagate.py --tables data/tables --reports reports --figures figures
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
from s5_b2_smart import (  # noqa: E402
    L2_GRID, _penalised_loglik, clustered_se, drop_redundant, fit_poisson_offset,
    standardise,
)

# Universal attributes, identical to B2's feature set.
U_COUNT = [5, 12, 193, 197, 198]
U_LEVEL = [194]
U_DELTA = [5, 12, 193, 197, 198]

# Seagate only. 190 is an airflow temperature difference, so a level; the rest
# are counters.
S_COUNT = [187, 188, 241, 242]
S_LEVEL = [190]
S_DELTA = [187, 188, 241, 242]

ARMS = {
    "U": ([], [], []),
    "U+187": ([187], [], [187]),
    "FULL": (S_COUNT, S_LEVEL, S_DELTA),
}


def build_features(df: pd.DataFrame, extra_count, extra_level, extra_delta):
    """Design matrix for one arm. Same transforms as B2, plus the arm's extras.

    Preallocated and written column by column, for the memory reason documented
    on the B2 version of this function.
    """
    counts = U_COUNT + list(extra_count)
    levels = U_LEVEL + list(extra_level)
    deltas = U_DELTA + list(extra_delta)
    names = ([f"log1p_smart_{n}" for n in counts]
             + [f"smart_{n}" for n in levels]
             + [f"log1p_rise_smart_{n}" for n in deltas])
    X = np.empty((len(df), len(names)))
    j = 0
    for n in counts:
        np.log1p(np.maximum(df[f"smart_{n}"].fillna(0).to_numpy(float), 0), out=X[:, j])
        j += 1
    for n in levels:
        v = df[f"smart_{n}"].to_numpy(float)
        med = float(np.nanmedian(v)) if np.any(np.isfinite(v)) else 0.0
        X[:, j] = np.nan_to_num(v, nan=med)
        j += 1
    for n in deltas:
        d = df[f"d30_smart_{n}"].fillna(0).to_numpy(float)
        np.log1p(np.maximum(d, 0), out=X[:, j])
        j += 1
    return X, names


def load_window(con, lo: str | None, hi: str, smart_cols: list[str]) -> pd.DataFrame:
    where = [f"landmark < DATE '{hi}'", "manufacturer = 'Seagate'"]
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


def design(df, b1, arm, mu=None, sd=None, keep=None, verbose=False):
    ec, el, ed = ARMS[arm]
    Xr, names = build_features(df, ec, el, ed)
    Xr, names, keep, dropped = drop_redundant(Xr, names, keep)
    if verbose and dropped:
        for nm, why in dropped:
            print(f"    {arm}: dropping {nm}, {why}")
    Xs, mu, sd = standardise(Xr, mu, sd)
    X = np.empty((len(Xs), Xs.shape[1] + 1))
    X[:, 0] = 1.0
    X[:, 1:] = Xs
    del Xs, Xr
    off = (np.maximum(b1["h_b1"].to_numpy(float), 1e-12)
           * np.maximum(df["expo"].to_numpy(float), 1e-6))
    return X, off, df["fail"].to_numpy(float), names, mu, sd, keep


def fit_arm(inner, val, train, test, arm):
    """Select the penalty on validation, fit on train, score test. Returns hazards."""
    Xi, oi, yi, _, mu, sd, keep = design(inner, fit_predict(inner, inner), arm)
    Xv, ov, yv, _, _, _, _ = design(val, fit_predict(inner, val), arm, mu, sd, keep)
    best = None
    for lam in L2_GRID:
        b, _, _, ok = fit_poisson_offset(Xi, yi, oi, lam=lam)
        if not ok:
            continue
        vll = _penalised_loglik(Xv, yv, ov, b, 0.0) / len(yv)
        if best is None or vll > best[1]:
            best = (lam, vll)
    if best is None:
        return None
    lam = best[0]

    b1_train, b1_test = fit_predict(train, train), fit_predict(train, test)
    Xtr, off_tr, y_tr, names, mu, sd, keep = design(train, b1_train, arm, verbose=True)
    cond = float(np.linalg.cond(Xtr.T @ Xtr))
    beta, iters, ll, converged = fit_poisson_offset(Xtr, y_tr, off_tr, lam=lam)
    if not converged:
        return None

    se = clustered_se(Xtr, y_tr, off_tr, beta, train["spell_key"].to_numpy())
    coef = pd.DataFrame({
        "arm": arm, "feature": ["intercept"] + names,
        "hazard_ratio": np.exp(beta),
        "hr_lo": np.exp(beta - 1.96 * se), "hr_hi": np.exp(beta + 1.96 * se),
        "z": beta / np.where(se > 0, se, np.nan),
    })

    Xte = design(test, b1_test, arm, mu, sd, keep)[0]
    h = (np.maximum(b1_test["h_b1"].to_numpy(float), 1e-12)
         * np.exp(np.clip(Xte @ beta, -30, 30)))
    return {"lam": lam, "iters": iters, "loglik": ll, "cond": cond,
            "coef": coef, "hazard": h, "b1_test": b1_test}


def oracle_rescale(df: pd.DataFrame, col: str) -> np.ndarray:
    """One constant fitted on the test rows so predicted and observed counts match.

    Uses the answer. Diagnostic only, never a reported model.
    """
    r = df[col].to_numpy(float)
    h = -np.log(np.clip(1.0 - r, 1e-15, 1.0)) / HORIZON_DAYS
    expected = float(np.sum(h * df["expo"].to_numpy(float)))
    c = float(df["fail"].sum()) / expected if expected > 0 else 1.0
    return 1.0 - np.exp(-h * c * HORIZON_DAYS)


def plot_incremental(coef: pd.DataFrame, path: Path) -> None:
    seagate_feats = [f for f in coef["feature"]
                     if any(str(n) in f for n in S_COUNT + S_LEVEL)]
    d = coef[coef["feature"].isin(seagate_feats)].sort_values("hazard_ratio")
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(8, 0.45 * len(d) + 2))
    y = np.arange(len(d))
    ax.errorbar(d["hazard_ratio"], y,
                xerr=[d["hazard_ratio"] - d["hr_lo"], d["hr_hi"] - d["hazard_ratio"]],
                fmt="o", ms=5, lw=1.4, color="#1F3A5F", ecolor="#7A7A7A", capsize=3)
    ax.axvline(1.0, color="#B3412C", lw=1, ls="--")
    ax.set_yticks(y)
    ax.set_yticklabels(d["feature"], fontsize=8)
    ax.set_xscale("log")
    ax.set_xlabel("hazard ratio per standard deviation (log scale)")
    ax.set_title("Seagate only attributes in the FULL arm, final fold")
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

    all_count = sorted(set(U_COUNT + U_LEVEL + S_COUNT + S_LEVEL))
    smart_cols = ([f"smart_{n}" for n in all_count]
                  + [f"d30_smart_{n}" for n in sorted(set(U_DELTA + S_DELTA))])

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(
        f"CREATE OR REPLACE VIEW landmarks AS "
        f"SELECT * FROM read_parquet('{(tables / 'landmarks.parquet').as_posix()}')"
    )

    rows, pooled, coef_last = [], [], {}
    for name, val_start, test_start, test_end in FOLDS:
        print(f"\n{name}: train < {test_start}, test {test_start} to {test_end}")
        inner = load_window(con, None, val_start, smart_cols)
        val = load_window(con, val_start, test_start, smart_cols)
        train = load_window(con, None, test_start, smart_cols)
        test = load_window(con, test_start, test_end, smart_cols)
        print(f"  Seagate cohort: {len(train):,} train rows "
              f"({int(train['fail'].sum()):,} events), {len(test):,} test rows "
              f"({int(test['fail'].sum()):,} events)")
        if test.empty or test["fail"].sum() == 0:
            continue

        scored = None
        for arm in ARMS:
            res = fit_arm(inner, val, train, test, arm)
            if res is None:
                print(f"  {arm:6s} FAILED to converge, aborting")
                return 1
            if scored is None:
                scored = res["b1_test"].copy()
            scored[f"risk_{arm}"] = 1.0 - np.exp(-res["hazard"] * HORIZON_DAYS)
            coef_last[arm] = res["coef"]
            print(f"  {arm:6s} lambda={res['lam']:<7g} cond={res['cond']:.2e} "
                  f"iters={res['iters']:<3d} loglik={res['loglik']:,.1f}")

        for arm in ARMS:
            m = ev.evaluate(scored, f"risk_{arm}")
            m.update({"fold": name, "arm": arm})
            rows.append(m)
            print(f"    {arm:6s} brier={m['ipcw_brier']:.6f}  auc={m['ipcw_auc']:.4f}")
        pooled.append(scored)

    if not rows:
        print("no folds produced results")
        return 1

    per_fold = pd.DataFrame(rows)[["fold", "arm", "n_rows", "n_events",
                                   "mean_predicted", "ipcw_brier", "ipcw_auc"]]
    per_fold.to_csv(reports / "b3_metrics_by_fold.csv", index=False)
    print("\n--- b3_metrics_by_fold ---")
    with pd.option_context("display.width", 200):
        print(per_fold.to_string(index=False))

    coefs = pd.concat(coef_last.values(), ignore_index=True)
    coefs.to_csv(reports / "b3_coefficients.csv", index=False)
    full = coef_last.get("FULL")
    if full is not None:
        seagate_only = full[full["feature"].str.contains(
            "|".join(str(n) for n in S_COUNT + S_LEVEL))]
        if seagate_only.empty:
            print("  (no Seagate only attributes survived duplicate removal)")
        print("\n--- b3_coefficients, Seagate only attributes, FULL arm, final fold ---")
        print("Hazard ratio per standard deviation, drive model and age held fixed by")
        print("the B1 offset. Collinearity among SMART counters means individual ratios")
        print("are not reliably interpretable; the predictive comparison below is.")
        with pd.option_context("display.width", 200, "display.float_format", "{:.4f}".format):
            print(seagate_only[["feature", "hazard_ratio", "hr_lo", "hr_hi", "z"]]
                  .to_string(index=False))
        # Coefficient forest plot removed. SMART counters are collinear, so the
        # individual hazard ratios are not reliably interpretable, and a forest
        # plot invites exactly that reading. The full table stays in
        # reports/, and the predictive comparison is the reportable result.

    all_test = pd.concat(pooled, ignore_index=True)
    print(f"\npooled Seagate: {len(all_test):,} rows, "
          f"{int(all_test['fail'].sum()):,} events")
    for arm in ARMS:
        all_test[f"risk_{arm}_oracle"] = oracle_rescale(all_test, f"risk_{arm}")

    print(f"bootstrapping at spell level, {args.n_boot} resamples")
    pairs = []
    comparisons = [("U", "FULL", "RQ2: all five Seagate attributes"),
                   ("U", "U+187", "attribute 187 alone"),
                   ("U+187", "FULL", "the other four beyond 187")]
    for a, b, label in comparisons:
        for suffix, level in (("", "raw"), ("_oracle", "oracle")):
            for metric in ("ipcw_auc", "ipcw_brier"):
                r = ev.bootstrap_paired_difference(
                    all_test, f"risk_{a}{suffix}", f"risk_{b}{suffix}",
                    metric, n_boot=args.n_boot)
                r.update({"comparison": label, "level": level, "metric": metric})
                pairs.append(r)
    pair_df = pd.DataFrame(pairs)[["comparison", "level", "metric",
                                   "difference", "lo", "hi", "excludes_zero"]]
    pair_df.to_csv(reports / "b3_paired_comparisons.csv", index=False)
    print("\n--- b3_paired_comparisons ---")
    with pd.option_context("display.width", 220):
        print(pair_df.to_string(index=False))

    def get(label, level, metric):
        return next(p for p in pairs if p["comparison"] == label
                    and p["level"] == level and p["metric"] == metric)

    rq2_auc = get("RQ2: all five Seagate attributes", "raw", "ipcw_auc")
    a187_auc = get("attribute 187 alone", "raw", "ipcw_auc")
    rest_auc = get("the other four beyond 187", "raw", "ipcw_auc")

    auc_ok = rq2_auc["difference"] > 0 and rq2_auc["excludes_zero"]
    share = (a187_auc["difference"] / rq2_auc["difference"]
             if rq2_auc["difference"] != 0 else float("nan"))

    print("\n================ E3 verdict ================")
    print("Criterion locked in DESIGN.md section 9 before B3 was fitted:")
    print("  primary     IPCW AUC improves, paired interval excludes zero")
    print("  secondary   IPCW Brier reported raw and at oracle level, no threshold")
    print("  directional 187 carries most of the gain, the other four contribute little")
    print(f"\n  AUC, FULL minus U      {rq2_auc['difference']:+.5f} "
          f"[{rq2_auc['lo']:+.5f}, {rq2_auc['hi']:+.5f}]  "
          f"{'PASS' if auc_ok else 'FAIL'}")
    print(f"  AUC, 187 alone         {a187_auc['difference']:+.5f} "
          f"[{a187_auc['lo']:+.5f}, {a187_auc['hi']:+.5f}]")
    print(f"  AUC, other four        {rest_auc['difference']:+.5f} "
          f"[{rest_auc['lo']:+.5f}, {rest_auc['hi']:+.5f}]")
    print(f"\n  share of the total AUC gain attributable to 187: {share:.1%}")

    directional = share > 0.5 and not rest_auc["excludes_zero"]
    print(f"  directional claim holds: {directional}")
    verdict = "E3 HOLDS" if auc_ok else "E3 FAILS"
    print(f"\n  {verdict}")
    print("\n  Interpretation for RQ2: this is the predictive value locked behind")
    print("  vendor firmware. A small or zero gain means the fleet wide model in B2")
    print("  sacrifices little by being restricted to universally reported attributes,")
    print("  which is a useful finding in its own right.")

    pd.DataFrame([{
        "auc_full_minus_u": rq2_auc["difference"],
        "auc_lo": rq2_auc["lo"], "auc_hi": rq2_auc["hi"], "auc_pass": auc_ok,
        "auc_187_alone": a187_auc["difference"],
        "auc_other_four": rest_auc["difference"],
        "share_from_187": share, "directional_holds": directional,
        "verdict": verdict,
    }]).to_csv(reports / "b3_e3_verdict.csv", index=False)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
