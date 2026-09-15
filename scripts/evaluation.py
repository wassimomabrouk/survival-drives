"""Evaluation metrics for landmark predictions, shared by every model in the ladder.

Each row is one spell observed at one landmark, carrying:

    t_days   days from the landmark to whichever came first, exit or the horizon
    status   1 if that exit was a failure, 0 otherwise
    risk     the model's predicted P(failure within the horizon)

The complication is censoring inside the horizon. A drive removed on day 12 of a
30 day window is not a survivor and is not a failure, and scoring it as either
would bias the result. Removals outnumber failures roughly four to one here, so
this is not a rounding error. Every metric below therefore uses inverse
probability of censoring weights (IPCW), with the censoring distribution
estimated by Kaplan-Meier on the same rows.

Per DESIGN.md section 8 the headline metric is the IPCW Brier score, not
concordance. Concordance rewards correct ranking and says nothing about whether
the probabilities themselves are usable, and the decision layer in section 10
needs calibrated probabilities rather than a ranking.

Uncertainty is bootstrapped at spell level, never row level, because one spell
contributes many correlated landmark rows and resampling rows would understate
the variance badly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HORIZON_DAYS = 30


def censoring_km(t_days: np.ndarray, status: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Kaplan-Meier of the CENSORING distribution, G(t).

    Same estimator as for survival, with the event indicator flipped: here an
    exit that is not a failure is the 'event' whose distribution we need.
    """
    order = np.argsort(t_days, kind="mergesort")
    t, s = t_days[order], status[order]
    times = np.unique(t)
    n = len(t)
    g, surv = [], 1.0
    at_risk = n
    for tt in times:
        in_bin = t == tt
        n_cens = int(np.sum(in_bin & (s == 0)))
        if at_risk > 0 and n_cens > 0:
            surv *= 1.0 - n_cens / at_risk
        g.append(surv)
        at_risk -= int(np.sum(in_bin))
    return times, np.array(g)


def _g_at(times: np.ndarray, g: np.ndarray, query: np.ndarray, floor: float = 1e-3) -> np.ndarray:
    """G evaluated just before each query time, floored so weights cannot explode."""
    idx = np.searchsorted(times, query, side="left") - 1
    idx = np.clip(idx, 0, len(g) - 1)
    return np.maximum(g[idx], floor)


def ipcw_weights(t_days: np.ndarray, status: np.ndarray, horizon: int = HORIZON_DAYS):
    """Weights and case mask for the IPCW estimators.

    Two groups contribute. Rows that failed inside the horizon, weighted by the
    inverse of G at their failure time. Rows that survived the whole horizon,
    weighted by the inverse of G at the horizon. Rows censored inside the horizon
    contribute nothing directly; they are represented through the weights on the
    others.
    """
    times, g = censoring_km(t_days, status)
    failed = (status == 1) & (t_days <= horizon)
    survived = t_days >= horizon
    w = np.zeros(len(t_days))
    w[failed] = 1.0 / _g_at(times, g, t_days[failed])
    w[survived] = 1.0 / _g_at(times, g, np.full(int(survived.sum()), horizon))
    return w, failed, survived


def ipcw_brier(risk: np.ndarray, t_days: np.ndarray, status: np.ndarray,
               horizon: int = HORIZON_DAYS) -> float:
    """IPCW Brier score at the horizon. Lower is better."""
    w, failed, survived = ipcw_weights(t_days, status, horizon)
    contrib = np.zeros(len(risk))
    contrib[failed] = (1.0 - risk[failed]) ** 2
    contrib[survived] = (0.0 - risk[survived]) ** 2
    return float(np.sum(w * contrib) / len(risk))


def ipcw_auc(risk: np.ndarray, t_days: np.ndarray, status: np.ndarray,
             horizon: int = HORIZON_DAYS) -> float:
    """Cumulative/dynamic time dependent AUC at the horizon, IPCW weighted.

    The probability that a randomly chosen drive failing inside the horizon is
    ranked above a randomly chosen drive surviving it, with both groups weighted
    to correct for censoring.
    """
    w, failed, survived = ipcw_weights(t_days, status, horizon)
    if failed.sum() == 0 or survived.sum() == 0:
        return float("nan")

    rc, wc = risk[failed], w[failed]
    rs, ws = risk[survived], w[survived]

    # Weighted Mann-Whitney over the sorted control scores, so the cost is
    # n log n rather than the quadratic pairwise comparison.
    order = np.argsort(rs, kind="mergesort")
    rs_s, ws_s = rs[order], ws[order]
    cum = np.concatenate([[0.0], np.cumsum(ws_s)])
    total_ctrl = cum[-1]

    lo = np.searchsorted(rs_s, rc, side="left")
    hi = np.searchsorted(rs_s, rc, side="right")
    below = cum[lo]                      # controls strictly below the case
    ties = cum[hi] - cum[lo]             # controls tied with the case
    num = float(np.sum(wc * (below + 0.5 * ties)))
    return num / (float(np.sum(wc)) * total_ctrl)


def calibration_table(risk: np.ndarray, t_days: np.ndarray, status: np.ndarray,
                      n_bins: int = 10, horizon: int = HORIZON_DAYS) -> pd.DataFrame:
    """Predicted against observed risk by predicted-risk decile.

    Observed risk in each bin is the IPCW estimate, so it is comparable with the
    predictions rather than biased downward by censored rows.
    """
    w, failed, survived = ipcw_weights(t_days, status, horizon)
    # Rank based bins, since predicted risks are heavily skewed toward zero and
    # equal width bins would put almost every row in the first one.
    ranks = pd.Series(risk).rank(method="first")
    bins = pd.qcut(ranks, q=n_bins, labels=False, duplicates="drop")

    rows = []
    for b in sorted(pd.unique(bins[~pd.isna(bins)])):
        m = bins == b
        num = float(np.sum(w[m] * failed[m]))
        den = float(np.sum(w[m] * (failed[m] | survived[m])))
        rows.append({
            "bin": int(b) + 1,
            "n": int(m.sum()),
            "mean_predicted": float(np.mean(risk[m])),
            "observed_ipcw": num / den if den > 0 else np.nan,
            "events_raw": int(failed[m].sum()),
        })
    out = pd.DataFrame(rows)
    out["ratio_obs_pred"] = out["observed_ipcw"] / out["mean_predicted"].replace(0, np.nan)
    return out


def evaluate(df: pd.DataFrame, risk_col: str, horizon: int = HORIZON_DAYS) -> dict:
    """All headline metrics for one prediction column on one set of rows."""
    risk = df[risk_col].to_numpy(dtype=float)
    t = df["t_days"].to_numpy(dtype=float)
    s = df["status"].to_numpy(dtype=int)
    return {
        "n_rows": len(df),
        "n_events": int(((s == 1) & (t <= horizon)).sum()),
        "mean_predicted": float(np.mean(risk)),
        "ipcw_brier": ipcw_brier(risk, t, s, horizon),
        "ipcw_auc": ipcw_auc(risk, t, s, horizon),
    }


def bootstrap_metric(df: pd.DataFrame, risk_col: str, metric: str = "ipcw_brier",
                     n_boot: int = 200, horizon: int = HORIZON_DAYS,
                     seed: int = 0) -> tuple[float, float]:
    """Percentile confidence interval, resampling SPELLS rather than rows.

    A spell appears at many landmarks and those rows are strongly correlated, so
    resampling rows would treat correlated observations as independent and give
    an interval far too narrow to be honest.
    """
    rng = np.random.default_rng(seed)
    keys = df["spell_key"].to_numpy()
    uniq, inverse = np.unique(keys, return_inverse=True)
    # Group rows by spell with a single sort rather than one scan per spell,
    # which would be quadratic and unusable at several hundred thousand spells.
    order = np.argsort(inverse, kind="mergesort")
    starts = np.searchsorted(inverse[order], np.arange(len(uniq)), side="left")
    ends = np.searchsorted(inverse[order], np.arange(len(uniq)), side="right")

    fn = {"ipcw_brier": ipcw_brier, "ipcw_auc": ipcw_auc}[metric]
    risk = df[risk_col].to_numpy(dtype=float)
    t = df["t_days"].to_numpy(dtype=float)
    s = df["status"].to_numpy(dtype=int)

    vals = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([order[starts[i]:ends[i]] for i in pick])
        try:
            vals.append(fn(risk[idx], t[idx], s[idx], horizon))
        except (ValueError, ZeroDivisionError):
            continue
    if not vals:
        return (float("nan"), float("nan"))
    return (float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5)))


def bootstrap_paired_difference(df: pd.DataFrame, risk_a: str, risk_b: str,
                                metric: str = "ipcw_brier", n_boot: int = 200,
                                horizon: int = HORIZON_DAYS, seed: int = 0) -> dict:
    """Confidence interval on the DIFFERENCE between two models, metric(b) - metric(a).

    Two models scored on the same rows produce highly correlated metrics, so
    comparing their marginal intervals is conservative: the intervals can overlap
    heavily while the difference is unambiguous. Scoring both models inside each
    resample and taking the difference removes the shared variation and is the
    comparison actually being asked for.

    Resampling is at spell level, as everywhere else, because a spell appears at
    many landmarks and its rows are not independent.
    """
    rng = np.random.default_rng(seed)
    keys = df["spell_key"].to_numpy()
    uniq, inverse = np.unique(keys, return_inverse=True)
    order = np.argsort(inverse, kind="mergesort")
    starts = np.searchsorted(inverse[order], np.arange(len(uniq)), side="left")
    ends = np.searchsorted(inverse[order], np.arange(len(uniq)), side="right")

    fn = {"ipcw_brier": ipcw_brier, "ipcw_auc": ipcw_auc}[metric]
    ra = df[risk_a].to_numpy(dtype=float)
    rb = df[risk_b].to_numpy(dtype=float)
    t = df["t_days"].to_numpy(dtype=float)
    s_ = df["status"].to_numpy(dtype=int)

    point = fn(rb, t, s_, horizon) - fn(ra, t, s_, horizon)
    diffs = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([order[starts[i]:ends[i]] for i in pick])
        try:
            diffs.append(fn(rb[idx], t[idx], s_[idx], horizon)
                         - fn(ra[idx], t[idx], s_[idx], horizon))
        except (ValueError, ZeroDivisionError):
            continue
    if not diffs:
        return {"difference": point, "lo": float("nan"), "hi": float("nan"),
                "excludes_zero": False}
    lo, hi = float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))
    return {"difference": point, "lo": lo, "hi": hi,
            "excludes_zero": bool(lo > 0 or hi < 0)}


def recalibration_factor(observed_events: float, expected_events: float) -> float:
    """Multiplicative correction for a hazard model whose overall level has drifted.

    Fitted on a held out validation window and applied to the test window. A
    factor below 1 means the model over-predicts, which happens when the fleet's
    failure rate declines and a model trained on older, higher hazard data
    carries that level forward. Shape across drives is left untouched; only the
    overall level moves, so discrimination is unchanged by construction and only
    calibration improves.
    """
    if expected_events <= 0:
        return 1.0
    return float(observed_events / expected_events)
