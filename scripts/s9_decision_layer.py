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
  3. Because the successor is not simulated, its failure risk and its service
     time are both omitted. The two push in opposite directions and the net
     direction was not measured; at k = 10 the policy removes about 1% of
     drive-years, so the effect is likely small. (An earlier version of this item
     said the omission favours aggressive policies and that their advantage was
     therefore understated, which contradicts itself. Corrected 2026-10-02.)
  4. Decisions are made at 28 day landmarks, not continuously.
  5. A replacement is credited with any failure the drive would have had later
     in the simulated window, not only within the 30 day horizon. Savings
     therefore grow with the length of the window simulated, and are quoted with
     it. `s11b_window_check.py` measures how much.

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


def threshold_grid(risk: np.ndarray) -> np.ndarray:
    """Candidate replacement thresholds tau.

    Two grids combined. Forty quantiles of predicted risk from the 90th to the
    99.999th percentile, dense where the rows are. And forty log-spaced values from
    the 90th percentile up to the largest predicted risk, dense in risk itself.
    The quantile grid alone left nothing between tau = 0.079 and 0.621 on the
    21-quarter run, which is exactly where the optimum sits at low cost ratios
    (the myopic threshold is 1/k), so conclusions for k of 5 and below depended
    on where the grid happened to have points. Added 2026-10-02 after an
    independent review; DESIGN.md section 13 logs it.
    """
    r = risk[np.isfinite(risk)]
    q = np.quantile(r, np.linspace(0.90, 0.99999, 40))
    lo, hi = float(np.quantile(r, 0.90)), float(r.max())
    g = np.geomspace(max(lo, 1e-9), hi, 40, endpoint=False)
    return np.unique(np.concatenate([q, g]))


def cost_rate(res: dict, k: float) -> float:
    """Cost in units of C_R, per drive operating year."""
    return (res["replacements"] + k * res["failures"]) / res["served_years"]


def plot_value_vs_cost_ratio(best: pd.DataFrame, path: Path) -> None:
    """The README's first figure: what each policy is worth at each cost ratio.

    Each risk-based point is labelled with its saving and the threshold behind it,
    so the figure and the README's when-to-replace table read as one thing.
    Redrawn 2026-10-02 for readability; the numbers are unchanged.
    """
    navy, rust = "#1F3A5F", "#B3412C"

    def series(pol):
        sub = best[best["policy"] == pol].set_index("k")
        return [float(sub.loc[k, "vs_run_to_failure_pct"]) for k in K_GRID], \
               [float(sub.loc[k, "parameter"]) for k in K_GRID]

    risk_y, risk_tau = series("risk-based")
    age_y, _ = series("age-based")

    fig, ax = plt.subplots(figsize=(9, 5.8))
    lo = min(risk_y) - 30
    hi = max(age_y) + 8
    ax.axhspan(lo, 0, color=navy, alpha=0.08, lw=0)
    ax.axhline(0.0, color="#555555", lw=1.2, ls=":")
    ax.plot(K_GRID, age_y, "--", marker="o", ms=6, lw=2, color=rust,
            label="replace on age (best age threshold)")
    ax.plot(K_GRID, risk_y, "-", marker="o", ms=7, lw=2.4, color=navy,
            label="replace on predicted risk (best risk threshold)")
    for k, y, tau in zip(K_GRID, risk_y, risk_tau):
        if k == 1:
            continue
        ax.annotate(f"{y:+.0f}%\nreplace above {100 * tau:.{1 if tau < 0.1 else 0}f}%",
                    (k, y), xytext=(0, -26), textcoords="offset points",
                    ha="center", va="top", fontsize=8.5, color=navy)
    ax.text(1.0, 2.5, "never replacing early (run to failure)", fontsize=8.5,
            color="#555555", va="bottom")
    ax.text(60, -2.5, "below the dotted line: cheaper than never replacing early", fontsize=8.5,
            color=navy, ha="right", va="top")
    ax.set_xscale("log")
    ax.set_xticks(K_GRID)
    ax.set_xticklabels([f"{k}x" for k in K_GRID])
    ax.set_xlim(0.85, 65)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("how much an unplanned failure costs, in planned replacements")
    ax.set_ylabel("fleet cost compared with never replacing early (%)")
    ax.set_title("What replacing drives on predicted risk is worth, three held-out quarters")
    ax.grid(alpha=0.2, lw=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(fontsize=9, frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--figures", default="figures")
    ap.add_argument("--figure-only", action="store_true",
                    help="redraw s9_value_vs_cost_ratio.png from the saved "
                         "s9_best_policy_by_k.csv without rerunning the simulation")
    ap.add_argument("--risk-col", default="risk_m2",
                    help="which model's predictions to use; M2 is the best of the ladder")
    args = ap.parse_args()

    tables, reports, figures = Path(args.tables), Path(args.reports), Path(args.figures)
    reports.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)

    if args.figure_only:
        best = pd.read_csv(reports / "s9_best_policy_by_k.csv")
        plot_value_vs_cost_ratio(best, figures / "s9_value_vs_cost_ratio.png")
        print(f"redrew {figures / 's9_value_vs_cost_ratio.png'}")
        return 0

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

    tau_grid = threshold_grid(risk)
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
        # One simulation per threshold; the cost ratio only re-weights its counts.
        sims = [(tau, simulate(r_adj, r_adj > tau, t_days, fail, starts, ends))
                for tau in tau_grid]
        for k in K_GRID:
            sub = [(tau, cost_rate(res, k), res) for tau, res in sims]
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
    # The y-axis is deliberately clipped. Aggressive thresholds drive the cost
    # rate to 30x run-to-failure, which on a full scale squashes the entire region
    # where the decision is actually made into a flat line at the bottom.
    rb = sweep[sweep["policy"] == "risk-based"].sort_values("parameter")
    ab = sweep[sweep["policy"] == "age-based"].sort_values("parameter")

    def rel(frame, k):
        rtf = sweep.loc[sweep["policy"] == "run-to-failure", f"cost_rate_k{k}"].iloc[0]
        return frame[f"cost_rate_k{k}"] / rtf

    shades = {2: "#9CC3D5", 5: "#5B8FA8", 10: "#2F5F7A", 20: "#1F3A5F", 50: "#0F2436"}

    # 1. threshold sweep, zoomed to the decision region
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for k in (2, 5, 10, 20, 50):
        y = rel(rb, k)
        ax.plot(rb["parameter"], y, lw=1.8, color=shades[k], label=f"k = {k}")
        i = y.idxmin()
        ax.plot(rb.loc[i, "parameter"], y.loc[i], "o", ms=6, color=shades[k],
                markeredgecolor="white", markeredgewidth=1.2, zorder=5)
    ax.axhline(1.0, color="#B3412C", lw=1.3, ls="--", label="run-to-failure")
    ax.set_xscale("log")
    ax.set_ylim(0.4, 2.0)
    ax.set_xlabel("replacement threshold tau (predicted 30 day failure risk)")
    ax.set_ylabel("cost rate relative to run-to-failure")
    ax.set_title("Risk-based replacement: where the threshold pays, by cost ratio")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=9, frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(figures / "s9_cost_rate_vs_threshold.png", dpi=150)
    plt.close(fig)

    # 2. two panels, because tau and age are not comparable quantities and do not
    #    belong on a shared axis
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5.2), sharey=True)
    for k in (5, 10, 20):
        a1.plot(rb["parameter"], rel(rb, k), lw=1.9, color=shades[k], label=f"k = {k}")
        a2.plot(ab["parameter"] / 8760.0, rel(ab, k), lw=1.9, color=shades[k],
                label=f"k = {k}")
    for a, title, xl in ((a1, "Risk-based: replace when predicted risk > tau",
                          "tau (predicted 30 day failure risk)"),
                         (a2, "Age-based: replace when age > threshold",
                          "age threshold (years of power on time)")):
        a.axhline(1.0, color="#B3412C", lw=1.3, ls="--")
        a.set_xlabel(xl)
        a.set_title(title, fontsize=11)
        a.grid(alpha=0.25, lw=0.6)
    a1.set_xscale("log")          # risk spans orders of magnitude
    a2.set_xscale("linear")       # age does not, and log ticks read as 6 x 10^0
    a1.set_ylim(0.4, 2.5)
    a1.set_ylabel("cost rate relative to run-to-failure")
    a1.legend(fontsize=9, frameon=False, loc="upper right")
    a2.text(0.97, 0.06, "never falls below the line at any cost ratio",
            transform=a2.transAxes, fontsize=9.5, color="#B3412C", ha="right")
    fig.suptitle("Age alone is not a usable replacement signal; predicted risk is",
                 fontsize=12.5)
    fig.tight_layout()
    fig.savefig(figures / "s9_risk_vs_age_policy.png", dpi=150)
    plt.close(fig)

    # 3. the actual headline: best achievable cost rate against the cost ratio
    plot_value_vs_cost_ratio(best, figures / "s9_value_vs_cost_ratio.png")

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
