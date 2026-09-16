"""Why are the B3 design matrices rank deficient?

B3 reported condition numbers of 1.5e33, 1.25e34 and 1.43e17 on the universal
arm. Anything above roughly 1e10 means the design is rank deficient and its
coefficients are not identified. The universal arm is the baseline in every RQ2
comparison, so this has to be resolved before the B3 result can be reported.

The prime suspect is a feature that is constant within the Seagate cohort while
varying across the full fleet, most likely a 30 day rise column that is always
zero. `standardise` floors a zero standard deviation at 1.0, so a constant column
passes through as all zeros and is perfectly collinear with the intercept.

A secondary concern follows from it. The universal arm selected the largest
penalty on the grid in every fold while the full arm selected one of the
smallest, which is what a rank deficient design needs to stay stable. If that is
the cause, the two arms are being regularised very differently and part of the
measured gain could be less shrinkage rather than more information.

This script reports per column variance, rank and condition number for each arm,
on the same Seagate cohort B3 uses, so the cause is identified rather than
guessed at.

Usage:

    py scripts/s6b_design_check.py --tables data/tables --reports reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from s4_b1_baseline import FOLDS  # noqa: E402
from s6_b3_seagate import (  # noqa: E402
    ARMS, S_COUNT, S_DELTA, S_LEVEL, U_COUNT, U_DELTA, U_LEVEL,
    build_features, load_window,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default="data/tables")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args()

    tables, reports = Path(args.tables), Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)

    smart_cols = ([f"smart_{n}" for n in sorted(set(U_COUNT + U_LEVEL + S_COUNT + S_LEVEL))]
                  + [f"d30_smart_{n}" for n in sorted(set(U_DELTA + S_DELTA))])

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(
        f"CREATE OR REPLACE VIEW landmarks AS "
        f"SELECT * FROM read_parquet('{(tables / 'landmarks.parquet').as_posix()}')"
    )

    # The final fold's training window, the largest and the one whose coefficients
    # B3 reports.
    _, _, test_start, _ = FOLDS[-1]
    train = load_window(con, None, test_start, smart_cols)
    print(f"Seagate training rows to {test_start}: {len(train):,}")

    rows = []
    for arm, (ec, el, ed) in ARMS.items():
        X, names = build_features(train, ec, el, ed)
        sd = X.std(axis=0)
        for nm, s, mn, mx in zip(names, sd, X.min(axis=0), X.max(axis=0)):
            rows.append({"arm": arm, "feature": nm, "std": s, "min": mn, "max": mx,
                         "constant": bool(s < 1e-12)})

        Xs = np.column_stack([np.ones(len(X)),
                              (X - X.mean(axis=0)) / np.where(sd < 1e-12, 1.0, sd)])
        rank = int(np.linalg.matrix_rank(Xs.T @ Xs))
        cond = float(np.linalg.cond(Xs.T @ Xs))
        n_const = int((sd < 1e-12).sum())
        print(f"\n{arm}: {Xs.shape[1]} columns, rank {rank}, "
              f"deficiency {Xs.shape[1] - rank}, cond {cond:.2e}, "
              f"{n_const} constant column(s)")
        if n_const:
            print("  constant:", [nm for nm, s in zip(names, sd) if s < 1e-12])

        # Rank deficiency can also come from exact collinearity between two
        # varying columns, which a per column variance check would miss.
        keep = sd >= 1e-12
        if keep.sum() >= 2:
            Xk = (X[:, keep] - X[:, keep].mean(axis=0)) / sd[keep]
            C = np.corrcoef(Xk, rowvar=False)
            kn = [nm for nm, k in zip(names, keep) if k]
            iu = np.triu_indices_from(C, k=1)
            worst = np.argsort(-np.abs(C[iu]))[:3]
            for w in worst:
                i, j = iu[0][w], iu[1][w]
                if abs(C[i, j]) > 0.95:
                    print(f"  near collinear: {kn[i]} and {kn[j]}, r = {C[i, j]:+.4f}")

    det = pd.DataFrame(rows)
    det.to_csv(reports / "s6b_design_diagnostics.csv", index=False)

    const = det[det["constant"]]
    print("\n--- constant features by arm ---")
    if const.empty:
        print("none; the rank deficiency comes from collinearity rather than "
              "constant columns")
    else:
        with pd.option_context("display.width", 200):
            print(const[["arm", "feature", "std", "min", "max"]].to_string(index=False))

    # How often is each rise column exactly zero? A column that is zero on almost
    # every row carries nearly no information even when not strictly constant.
    print("\n--- share of rows where each 30 day rise is exactly zero ---")
    share = []
    for n in sorted(set(U_DELTA + S_DELTA)):
        v = train[f"d30_smart_{n}"].fillna(0).to_numpy(float)
        share.append({"feature": f"d30_smart_{n}",
                      "share_zero": float(np.mean(np.maximum(v, 0) == 0)),
                      "share_null": float(train[f"d30_smart_{n}"].isna().mean())})
    sh = pd.DataFrame(share)
    sh.to_csv(reports / "s6b_rise_sparsity.csv", index=False)
    with pd.option_context("display.width", 200, "display.float_format", "{:.4f}".format):
        print(sh.to_string(index=False))

    con.close()
    print(f"\nwritten to {reports.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
