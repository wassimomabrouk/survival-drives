"""Section 0, stage 1: ingest Backblaze Drive Stats quarterly archives into Parquet.

Disk conscious by design. A quarterly zip expands to roughly 12 GB of daily CSVs,
so this script never extracts more than a small batch of days at a time. For each
batch it extracts, reads with DuckDB, writes a compressed Parquet part, and clears
the scratch folder before moving on. Peak scratch use is about 1.5 GB regardless
of how many quarters are processed.

Two further reductions:

  * Only the columns needed for survival analysis are kept, roughly 16 out of 180.
  * Rows are sampled to one day in seven, except that every row carrying a failure
    flag is always kept so no events are lost. Daily resolution is unnecessary for
    a 30 day prediction horizon, and it multiplies storage sevenfold.

The cost of sampling is that a drive's power on hours at entry is known to within
seven days, about 168 hours, rather than exactly. That is immaterial against drive
lifetimes measured in tens of thousands of hours, but it is a stated limitation.

Layout expected:

    project/
      data/
        zips/        quarterly zips, can be deleted after each is ingested
        tmp/         scratch, created and cleared automatically
        parquet/     output, one subfolder of parts per quarter

Usage (PowerShell):

    py s0_ingest.py --zips data/zips --out data/parquet

Suggested workflow to keep disk use low: download one zip, run the script, delete
that zip, download the next. Re-running skips quarters already ingested.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

import duckdb

# SMART attributes kept for Section 0. smart_9_raw (power on hours) is the
# survival time scale and is not optional. The rest are the attributes with a
# documented association with drive failure, plus workload and temperature.
SMART_RAW_COLS = [
    5,    # reallocated sectors count
    9,    # power on hours, used as the time scale
    12,   # power cycle count
    187,  # reported uncorrectable errors
    188,  # command timeout
    190,  # airflow temperature difference
    193,  # load or unload cycle count
    194,  # temperature
    197,  # current pending sector count
    198,  # offline uncorrectable sector count
    241,  # total LBAs written
    242,  # total LBAs read
]

BASE_COLS = ["date", "serial_number", "model", "capacity_bytes", "failure"]

# Fixed anchor so the weekly sampling grid is identical across quarters and runs.
SAMPLE_ANCHOR = "2000-01-01"


def csv_members(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(
            m for m in zf.namelist()
            if m.lower().endswith(".csv")
            and "__MACOSX" not in m
            and not Path(m).name.startswith(".")
        )


def extract_batch(zip_path: Path, members: list[str], tmp_dir: Path) -> None:
    """Extract one batch of daily CSVs, flattened into a freshly cleared tmp_dir."""
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in members:
            target = tmp_dir / Path(member).name
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _reader(glob: str, all_varchar: bool) -> str:
    opts = [f"'{glob}'", "union_by_name = true", "sample_size = -1", "header = true"]
    if all_varchar:
        opts.append("all_varchar = true")
    return f"read_csv({', '.join(opts)})"


def describe_columns(con: duckdb.DuckDBPyConnection, glob: str, all_varchar: bool) -> set[str]:
    reader = _reader(glob, all_varchar)
    return {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {reader} LIMIT 0").fetchall()}


def build_projection(present: set[str]) -> str:
    parts = [
        "TRY_CAST(date AS DATE) AS date",
        "CAST(serial_number AS VARCHAR) AS serial_number",
        "CAST(model AS VARCHAR) AS model",
        "TRY_CAST(capacity_bytes AS BIGINT) AS capacity_bytes",
        "TRY_CAST(failure AS TINYINT) AS failure",
    ]
    for n in SMART_RAW_COLS:
        col = f"smart_{n}_raw"
        if col in present:
            parts.append(f"TRY_CAST({col} AS BIGINT) AS {col}")
        else:
            parts.append(f"CAST(NULL AS BIGINT) AS {col}")
    return ",\n                ".join(parts)


def sample_filter(sample_every: int) -> str:
    """Keep one day in N, and unconditionally keep every row with a failure flag."""
    if sample_every <= 1:
        return "TRUE"
    return (
        f"(TRY_CAST(failure AS TINYINT) = 1 "
        f"OR DATE_DIFF('day', DATE '{SAMPLE_ANCHOR}', TRY_CAST(date AS DATE)) "
        f"% {sample_every} = 0)"
    )


def ingest_quarter(
    con: duckdb.DuckDBPyConnection,
    zip_path: Path,
    tmp_dir: Path,
    out_dir: Path,
    batch_size: int,
    sample_every: int,
    force: bool,
) -> dict:
    label = zip_path.stem
    q_dir = out_dir / label
    done_marker = q_dir / "_COMPLETE"

    if done_marker.exists() and not force:
        print(f"  {label}: already ingested, skipping")
        return {"quarter": label, "status": "skipped", "rows": 0}

    if q_dir.exists():
        shutil.rmtree(q_dir)
    q_dir.mkdir(parents=True)

    members = csv_members(zip_path)
    if not members:
        print(f"  {label}: no CSV members found, skipping")
        return {"quarter": label, "status": "empty", "rows": 0}

    batches = [members[i:i + batch_size] for i in range(0, len(members), batch_size)]
    keep = sample_filter(sample_every)
    total_rows = 0
    absent_smart: set[str] = set()
    all_varchar = False

    for k, batch in enumerate(batches):
        extract_batch(zip_path, batch, tmp_dir)
        glob = (tmp_dir / "*.csv").as_posix()

        try:
            present = describe_columns(con, glob, all_varchar)
        except duckdb.Error:
            # Type unification can fail when SMART coverage differs between files
            # in the same batch. Reading everything as text and casting in the
            # projection is slower but always works.
            all_varchar = True
            present = describe_columns(con, glob, all_varchar)

        missing_base = [c for c in BASE_COLS if c not in present]
        if missing_base:
            raise RuntimeError(f"{label}: required columns absent: {missing_base}")

        absent_smart |= {f"smart_{n}_raw" for n in SMART_RAW_COLS if f"smart_{n}_raw" not in present}

        part_path = q_dir / f"part_{k:03d}.parquet"
        con.execute(
            f"""
            COPY (
                SELECT
                {build_projection(present)}
                FROM {_reader(glob, all_varchar)}
                WHERE {keep}
            ) TO '{part_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        total_rows += con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{part_path.as_posix()}')"
        ).fetchone()[0]

        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"    batch {k + 1}/{len(batches)} done, {total_rows:,} rows so far", end="\r")

    done_marker.write_text("ok\n", encoding="utf-8")
    size_gb = sum(p.stat().st_size for p in q_dir.glob("*.parquet")) / 1e9

    print(
        f"  {label}: {len(members)} daily files, {total_rows:,} rows kept, {size_gb:.2f} GB"
        + (f", absent in source: {sorted(absent_smart)}" if absent_smart else "")
    )
    return {"quarter": label, "status": "ok", "rows": total_rows, "gb": size_gb}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zips", default="data/zips", help="folder holding the quarterly zips")
    ap.add_argument("--out", default="data/parquet", help="folder for the Parquet output")
    ap.add_argument("--tmp", default="data/tmp", help="scratch folder, cleared between batches")
    ap.add_argument("--batch-size", type=int, default=10,
                    help="daily CSVs extracted at once, controls peak scratch use")
    ap.add_argument("--sample-every", type=int, default=7,
                    help="keep one day in N, failure rows always kept, 1 disables sampling")
    ap.add_argument("--memory-limit", default="6GB")
    ap.add_argument("--threads", type=int, default=0, help="0 leaves the DuckDB default")
    ap.add_argument("--force", action="store_true", help="rebuild quarters already ingested")
    args = ap.parse_args()

    zips_dir, out_dir, tmp_dir = Path(args.zips), Path(args.out), Path(args.tmp)

    if not zips_dir.is_dir():
        print(f"zip folder not found: {zips_dir}", file=sys.stderr)
        return 1
    zip_paths = sorted(zips_dir.glob("*.zip"))
    if not zip_paths:
        print(f"no zips found in {zips_dir}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    if args.threads:
        con.execute(f"SET threads = {args.threads}")

    print(f"ingesting {len(zip_paths)} quarters, batch size {args.batch_size} days, "
          f"sampling one day in {args.sample_every}")
    results = []
    try:
        for zp in zip_paths:
            results.append(
                ingest_quarter(con, zp, tmp_dir, out_dir, args.batch_size,
                               args.sample_every, args.force)
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        con.close()

    total = sum(r.get("rows", 0) for r in results)
    ok = sum(1 for r in results if r["status"] == "ok")
    on_disk = sum(p.stat().st_size for p in out_dir.rglob("*.parquet")) / 1e9
    print(f"\ndone: {ok} quarters ingested this run, {total:,} rows written")
    print(f"total parquet on disk: {on_disk:.2f} GB")
    print("the zips in data/zips can now be deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
