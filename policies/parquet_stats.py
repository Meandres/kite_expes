#!/usr/bin/env python3
"""Column-chunk statistics for a directory of Parquet files.

The column chunk is the unit uCache stores, so its size distribution decides
how many objects a cache holds and how many requests a scan issues. That
distribution is set by the *writer's* row-group size, not by anything the
reader does, which makes it worth looking at directly.

Reads footers only (`parquet_metadata`), so it is cheap even on 25 GB files.
"""

import argparse
import csv
import io
import math
import os
import subprocess
import sys

QUERY = """
SELECT file_name, row_group_id, path_in_schema, compression,
       row_group_num_rows, total_compressed_size, total_uncompressed_size
FROM parquet_metadata({files});
"""


def fmt(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def describe(vals, label, indent=""):
    v = sorted(vals)
    total = sum(v)
    print(f"{indent}{label}: {len(v):,} chunks, {fmt(total)} total")
    print(
        f"{indent}  min {fmt(v[0])}   p25 {fmt(pct(v,25))}   median {fmt(pct(v,50))}"
        f"   p75 {fmt(pct(v,75))}   p95 {fmt(pct(v,95))}   max {fmt(v[-1])}"
    )
    print(f"{indent}  mean {fmt(total/len(v))}")


def histogram(vals, width=54):
    """Log2 buckets: chunk sizes span several orders of magnitude."""
    buckets = {}
    for v in vals:
        b = 0 if v <= 0 else int(math.floor(math.log2(v)))
        buckets[b] = buckets.get(b, 0) + 1
    lo, hi = min(buckets), max(buckets)
    peak = max(buckets.values())
    total = len(vals)
    print(f"{'size range':>22}  {'count':>9}  {'share':>6}")
    for b in range(lo, hi + 1):
        c = buckets.get(b, 0)
        bar = "#" * int(round(width * c / peak)) if c else ""
        rng = f"{fmt(2**b)}-{fmt(2**(b+1))}"
        print(f"{rng:>22}  {c:>9,}  {100*c/total:>5.1f}%  {bar}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", help="directory of .parquet files, or a single file")
    p.add_argument("--duckdb", default=os.environ.get("DUCKDB_CACHE_HTTPFS", "duckdb"))
    p.add_argument("--threads", type=int, default=2,
                   help="kept low so this can run beside a benchmark")
    p.add_argument("--per-column", default="lineitem",
                   help="file stem to break down by column ('' to skip)")
    args = p.parse_args()

    if os.path.isdir(args.path):
        files = sorted(
            os.path.join(args.path, f)
            for f in os.listdir(args.path)
            if f.endswith(".parquet")
        )
    else:
        files = [args.path]
    if not files:
        sys.exit(f"no .parquet files under {args.path}")

    lst = "[" + ", ".join(f"'{f}'" for f in files) + "]"
    sql = f"SET threads={args.threads};\n" + QUERY.format(files=lst)
    proc = subprocess.run(
        [args.duckdb, "-csv", "-init", "/dev/null", "-c", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or "Error" in proc.stdout[:400]:
        sys.stderr.write(proc.stdout[:2000] + proc.stderr[:2000])
        sys.exit("parquet_metadata failed")

    rows = list(csv.DictReader(io.StringIO(proc.stdout)))
    if not rows:
        sys.exit("no metadata rows returned")

    by_file, rowgroups, compressions = {}, {}, {}
    for r in rows:
        f = os.path.basename(r["file_name"])
        by_file.setdefault(f, []).append(r)
        rowgroups.setdefault(f, set()).add(r["row_group_id"])
        compressions[r["compression"]] = compressions.get(r["compression"], 0) + 1

    print("=" * 78)
    print(f"PER FILE  ({args.path})")
    print("=" * 78)
    hdr = f"{'file':<20}{'row groups':>11}{'rows/group':>12}{'columns':>9}{'chunks':>9}{'avg chunk':>12}"
    print(hdr)
    print("-" * len(hdr))
    allsizes = []
    for f, rs in sorted(by_file.items(), key=lambda kv: -sum(int(x["total_compressed_size"]) for x in kv[1])):
        sizes = [int(x["total_compressed_size"]) for x in rs]
        allsizes += sizes
        ngroups = len(rowgroups[f])
        ncols = len(rs) // ngroups if ngroups else 0
        rpg = int(rs[0]["row_group_num_rows"])
        print(f"{f:<20}{ngroups:>11,}{rpg:>12,}{ncols:>9}{len(sizes):>9,}{fmt(sum(sizes)/len(sizes)):>12}")

    print()
    print("=" * 78)
    print("COLUMN CHUNK SIZE (compressed)")
    print("=" * 78)
    describe(allsizes, "all files")
    print(f"  compression: {', '.join(f'{k} x{v:,}' for k, v in compressions.items())}")
    print()
    histogram(allsizes)

    if args.per_column:
        target = [f for f in by_file if f.startswith(args.per_column)]
        if target:
            f = target[0]
            print()
            print("=" * 78)
            print(f"PER COLUMN  ({f})")
            print("=" * 78)
            cols = {}
            for r in by_file[f]:
                cols.setdefault(r["path_in_schema"], []).append(
                    (int(r["total_compressed_size"]), int(r["total_uncompressed_size"]))
                )
            hdr = f"{'column':<18}{'chunks':>8}{'total':>12}{'avg chunk':>12}{'max chunk':>12}{'ratio':>8}"
            print(hdr)
            print("-" * len(hdr))
            for c, v in sorted(cols.items(), key=lambda kv: -sum(x[0] for x in kv[1])):
                comp = [x[0] for x in v]
                unc = sum(x[1] for x in v)
                tot = sum(comp)
                print(f"{c:<18}{len(comp):>8,}{fmt(tot):>12}{fmt(tot/len(comp)):>12}"
                      f"{fmt(max(comp)):>12}{unc/tot if tot else 0:>8.2f}")


if __name__ == "__main__":
    main()
