"""Decision layer: from predicted risk to a replacement policy.

This is section 10 of DESIGN.md and the point of the whole project. A calibrated
30 day failure probability is not itself useful to an operator. What is useful is
an answer to: given this drive's predicted risk today, replace it or leave it?

**The decision rule.** With C_R the cost of a planned replacement and C_F the cost
of an unplanned failure, the myopic comparison at a single decision point is

    cost of keeping   = p * C_F
    cost of replacing = C_R

so replace when p > C_R / C_F = 1/k, where k = C_F / C_R. The threshold is
therefore a direct function of the cost ratio. That rule ignores the remaining
useful life discarded by replacing early, so the policy actually simulated is the
thresholded form

    pi_tau:  replace drive i at landmark t if p_i(t) > tau

with tau searched over the held-out quarters rather than taken from the closed
form.

**Everything is reported in units of C_R**, never in euros. The cost ratio k is
not public and inventing a figure would be the least defensible number in the
project. Dividing through by C_R gives

    cost rate = (replacements + k * failures) / drive operating years

so the only free parameter is k, and it is swept.

**Three policies, so risk-based replacement is measured against real
alternatives** rather than against nothing:

  run-to-failure   never replace preemptively
  age-based        replace at a fixed power-on-hours threshold, swept
  risk-based       replace when predicted risk exceeds tau, swept

**Simulator assumptions, stated because the result is conditional on them:**

  1. A replaced drive leaves service and is not observed further. Its successor
     cannot be simulated, because no data exists for a drive that was never
     installed.
  2. The denominator is drive operating years actually served under the policy,
     so replacing early shrinks it. This is what charges a policy for the
     remaining useful life it discards.
  3. Replacement drives are assumed to carry no failure risk within the window.
     This favours aggressive policies, so any advantage found for risk-based
     replacement is if anything understated relative to a simulator that modelled
     infant mortality in replacements.
  4. Decisions are made at 28 day landmarks, not continuously.

**The causal caution that governs the wording.** No intervention took place. When
the policy replaces a drive that was later observed to fail, the correct statement
is that the policy would have removed it from service before its observed failure.
It is not that a failure was avoided: that counterfactual was never observed. All
such counts are labelled `projected_prevented`.

Usage:

    py scripts/s9_decision_layer.py --tables data/tables --reports reports --figures figures
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

LANDMARK_STEP_DAYS = 28
DAYS_PER_YEAR = 365.25

# Cost ratios swept. k = 1 means an unplanned failure costs no more than a planned
# swap, so no preemptive replacement can ever pay. k = 50 is an aggressive figure
# for a fleet where a failure triggers a rebuild and a window of reduced
# redundancy. The truth is somewhere inside and is not public.
K_GRID = [1, 2, 5, 10, 20, 50]

# Multipliers applied to every predicted risk, representing the fleet hazard level
# coming in below or above what the model expected. Measured quarter to quarter
# variation is about 21%, so plus or minus 20% brackets it.
LEVEL_GRID = [0.8, 1.0, 1.2]


def simulate(risk: np.ndarray, trigger: np.ndarray, t_days: np.ndarray,
             fail: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> dict:
    """Walk each spell forward under a policy and accumulate outcomes.

    Rows must already be sorted by (spell_key, landmark); `starts` and `ends`
    delimit each spell. Within a spell the drive serves until whichever comes
    first: the policy triggers, it fails, or its observations run out.
    """
    n_repl = n_fail = n_prevented = n_unnecessary = 0
    served_days = 0.0

    served_step = np.minimum(t_days, LANDMARK_STEP_DAYS)
    # A failure falls in the interval after landmark j when the drive is flagged
    # there and exits within the step, rather than in a later interval.
    fails_here = (fail == 1) & (t_days <= LANDMARK_STEP_DAYS)

    for s, e in zip(starts, ends):
        trig = trigger[s:e]
        fh = fails_here[s:e]
        step = served_step[s:e]
        td = t_days[s:e]

        ti = int(np.argmax(trig)) if trig.any() else -1
        fi = int(np.argmax(fh)) if fh.any() else -1

        if ti >= 0 and (fi < 0 or ti <= fi):
            n_repl += 1
            served_days += float(step[:ti].sum())
            if fi >= 0:
                n_prevented += 1          # would have left service before its failure
            else:
                n_unnecessary += 1        # no failure was observed for this drive
        elif fi >= 0:
            n_fail += 1
            served_days += float(step[:fi].sum()) + float(td[fi])
        else:
            served_days += float(step.sum())

    years = served_days / DAYS_PER_YEAR
    return {"replacements": n_repl, "failures": n_fail,
            "projected_prevented": n_prevented, "unnecessary": n_unnecessary,
            "served_years": years,
            "cost_per_year_base": n_repl / years if years > 0 else np.nan,
            "failures_per_year": n_fail / years if years > 0 else np.nan}


def cost_rate(res: dict, k: float) -> float:
    """Cost in units of C_R, per drive operating year."""
    return (res["replacements"] + k * res["failures"]) / res["served_years"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--figures", default="figures")
    ap.add_argument("--risk-col", default="risk_m2",
                    help="which model's predictions to use; M2 is the best of the ladder")
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    reports.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    path = tables / "predictions.parquet"
    if not path.exists():
        print(f"{path} not found. Re-run s8_m2_boosted.py, which now writes it.")
        return 1

    df = pd.read_parquet(path).sort_values(["spell_key", "landmark"], kind="mergesort")
    df = df.reset_index(drop=True)
    print(f"{len(df):,} held-out landmark rows, "
          f"{df['spell_key'].nunique():,} spells, {int(df['fail'].sum()):,} events")
    print(f"policy predictions from {args.risk_col}")

    keys = df["spell_key"].to_numpy()
    _, first = np.unique(keys, return_index=True)
    starts = np.sort(first)
    ends = np.append(starts[1:], len(df))

    risk = df[args.risk_col].to_numpy(float)
    poh = df["poh_at_landmark"].to_numpy(float)
    t_days = df["t_days"].to_numpy(float)
    fail = df["fail"].to_numpy(int)

    # ---------------------------------------------------------------- baseline
    base = simulate(risk, np.zeros(len(df), dtype=bool), t_days, fail, starts, ends)
    print(f"\nrun-to-failure: {base['failures']:,} failures over "
          f"{base['served_years']:,.0f} drive years "
          f"({base['failures_per_year'] * 100:.3f}% annualised)")

    # ------------------------------------------------------------ policy sweeps
    rows = []
    rows.append({"policy": "run-to-failure", "parameter": np.nan, **base})

    tau_grid = np.unique(np.quantile(risk, np.linspace(0.90, 0.99999, 40)))
    for tau in tau_grid:
        r = simulate(risk, risk > tau, t_days, fail, starts, ends)
        rows.append({"policy": "risk-based", "parameter": float(tau), **r})

    age_grid = np.quantile(poh[np.isfinite(poh)], np.linspace(0.50, 0.999, 30))
    for a in np.unique(age_grid):
        r = simulate(risk, poh > a, t_days, fail, starts, ends)
        rows.append({"policy": "age-based", "parameter": float(a), **r})

    sweep = pd.DataFrame(rows)
    for k in K_GRID:
        sweep[f"cost_rate_k{k}"] = sweep.apply(lambda r: cost_rate(r, k), axis=1)
    sweep.to_csv(reports / "s9_policy_sweep.csv", index=False)

    # ------------------------------------------------- best policy at each k
    best_rows = []
    for k in K_GRID:
        col = f"cost_rate_k{k}"
        rtf = sweep.loc[sweep["policy"] == "run-to-failure", col].iloc[0]
        for pol in ("age-based", "risk-based"):
            sub = sweep[sweep["policy"] == pol]
            b = sub.loc[sub[col].idxmin()]
            best_rows.append({
                "k": k, "policy": pol, "parameter": b["parameter"],
                "replacements": int(b["replacements"]), "failures": int(b["failures"]),
                "projected_prevented": int(b["projected_prevented"]),
                "unnecessary": int(b["unnecessary"]),
                "served_years": b["served_years"], "cost_rate": b[col],
                "vs_run_to_failure_pct": 100 * (b[col] - rtf) / rtf,
            })
        best_rows.append({
            "k": k, "policy": "run-to-failure", "parameter": np.nan,
            "replacements": 0, "failures": int(base["failures"]),
            "projected_prevented": 0, "unnecessary": 0,
            "served_years": base["served_years"], "cost_rate": rtf,
            "vs_run_to_failure_pct": 0.0,
        })
    best = pd.DataFrame(best_rows).sort_values(["k", "cost_rate"])
    best.to_csv(reports / "s9_best_policy_by_k.csv", index=False)

    print("\n--- s9_best_policy_by_k ---")
    print("Cost rate is in units of C_R per drive operating year, so only the ratio")
    print("k = C_F / C_R matters and no euro figure is invented. Negative")
    print("vs_run_to_failure_pct means the policy is cheaper than running to failure.")
    with pd.option_context("display.width", 240, "display.float_format", "{:.4f}".format):
        print(best.to_string(index=False))

    # ------------------------------------------------ sensitivity to the level
    sens = []
    for mult in LEVEL_GRID:
        r_adj = np.clip(risk * mult, 0, 1)
        for k in K_GRID:
            sub = []
            for tau in tau_grid:
                res = simulate(r_adj, r_adj > tau, t_days, fail, starts, ends)
                sub.append((tau, cost_rate(res, k), res))
            tau_b, cr_b, res_b = min(sub, key=lambda z: z[1])
            rtf = cost_rate(base, k)
            sens.append({"level_multiplier": mult, "k": k, "best_tau": tau_b,
                         "cost_rate": cr_b,
                         "vs_run_to_failure_pct": 100 * (cr_b - rtf) / rtf,
                         "replacements": res_b["replacements"],
                         "failures": res_b["failures"],
                         "projected_prevented": res_b["projected_prevented"]})
    sens_df = pd.DataFrame(sens)
    sens_df.to_csv(reports / "s9_level_sensitivity.csv", index=False)
    print("\n--- s9_level_sensitivity ---")
    print("Predicted risks scaled by 0.8 and 1.2, bracketing the measured 21% quarter")
    print("to quarter variation in the fleet failure rate. A recommendation that")
    print("survives this range is worth more than one tuned to a level nobody can")
    print("predict in advance (see the rejected recalibration, DESIGN.md amendments).")
    with pd.option_context("display.width", 240, "display.float_format", "{:.4f}".format):
        print(sens_df.to_string(index=False))

    # ------------------------------------------------------------------ figures
    fig, ax = plt.subplots(figsize=(9, 5.5))
    rb = sweep[sweep["policy"] == "risk-based"].sort_values("parameter")
    ab = sweep[sweep["policy"] == "age-based"].sort_values("parameter")
    for k, c in zip((2, 5, 10, 20), ("#9CC3D5", "#5B8FA8", "#2F5F7A", "#1F3A5F")):
        rtf = sweep.loc[sweep["policy"] == "run-to-failure", f"cost_rate_k{k}"].iloc[0]
        ax.plot(rb["parameter"], rb[f"cost_rate_k{k}"] / rtf, lw=1.7,
                color=c, label=f"k = {k}")
    ax.axhline(1.0, color="#B3412C", lw=1.2, ls="--", label="run-to-failure")
    ax.set_xscale("log")
    ax.set_xlabel("replacement threshold tau (predicted 30 day failure risk)")
    ax.set_ylabel("cost rate relative to run-to-failure")
    ax.set_title("Risk-based replacement: cost rate against threshold, by cost ratio")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=9, frameon=False)
    fig.tight_layout()
    fig.savefig(figures / "s9_cost_rate_vs_threshold.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5.5))
    for k, c in zip((5, 10, 20), ("#9CC3D5", "#2F5F7A", "#1F3A5F")):
        rtf = sweep.loc[sweep["policy"] == "run-to-failure", f"cost_rate_k{k}"].iloc[0]
        ax.plot(rb["parameter"], rb[f"cost_rate_k{k}"] / rtf, lw=1.8, color=c,
                label=f"risk-based, k = {k}")
    for k, c in zip((5, 10, 20), ("#E5B9AE", "#C77B69", "#B3412C")):
        rtf = sweep.loc[sweep["policy"] == "run-to-failure", f"cost_rate_k{k}"].iloc[0]
        ax.plot(ab["parameter"] / 8760.0, ab[f"cost_rate_k{k}"] / rtf, lw=1.4,
                ls="--", color=c, label=f"age-based, k = {k}")
    ax.axhline(1.0, color="grey", lw=1.0, ls=":")
    ax.set_xscale("log")
    ax.set_xlabel("policy parameter: tau (risk) or age in years (age-based)")
    ax.set_ylabel("cost rate relative to run-to-failure")
    ax.set_title("Risk-based against age-based replacement")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=8, frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(figures / "s9_risk_vs_age_policy.png", dpi=150)
    plt.close(fig)

    # ------------------------------------------------------------------ headline
    print("\n================ headline ================")
    any_win = False
    for k in K_GRID:
        sub = best[(best["k"] == k) & (best["policy"] == "risk-based")].iloc[0]
        agek = best[(best["k"] == k) & (best["policy"] == "age-based")].iloc[0]
        better = sub["vs_run_to_failure_pct"] < 0
        any_win = any_win or better
        print(f"  k={k:<3d} risk-based {sub['vs_run_to_failure_pct']:+7.2f}% vs "
              f"run-to-failure, age-based {agek['vs_run_to_failure_pct']:+7.2f}%"
              f"   {'risk-based pays' if better else 'no policy pays'}")
    if not any_win:
        print("\n  At no plausible cost ratio does preemptive replacement lower the cost")
        print("  rate. That is a real finding: at a 1.4% annual failure rate, the")
        print("  discarded remaining life outweighs the failures intercepted.")
    print("\n  All counts labelled projected_prevented are conditional on the simulator")
    print("  assumptions in this file's header. No intervention occurred, so no")
    print("  failure was observed to be avoided.")

    print(f"\nwritten to {reports.resolve()} and {figures.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
