#!/usr/bin/env python3
"""Run one (cache configuration, workload) pair and write a JSON result.

Configurations, all reading local Parquet through cache_httpfs with a simulated
remote miss cost (delay_us = sim_latency_us + bytes / (sim_bandwidth_gbps * 1000)):

  nocache        cache_httpfs_type='noop'
  cache_httpfs   fixed-size blocks, LRU (the extension as shipped)
  ucache         column-chunk entries, selectable eviction policy/scope
"""

import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

TIMER_RE = re.compile(r"^Run Time \(s\): real ([0-9.]+)")
MARKER = "@@Q"


def collect_queries(spec):
    """spec: directory (all *.sql inside, sorted), glob, or explicit files."""
    paths = []
    for entry in spec:
        if os.path.isdir(entry):
            paths += sorted(
                os.path.join(entry, f) for f in os.listdir(entry) if f.endswith(".sql")
            )
        else:
            paths += sorted(glob.glob(entry)) if any(c in entry for c in "*?[") else [entry]
    if not paths:
        sys.exit(f"no .sql files found in {spec}")
    return paths


def dataset_bytes(data_dir):
    files = glob.glob(os.path.join(data_dir, "*.parquet"))
    if not files:
        sys.exit(f"no .parquet files under {data_dir}")
    return sum(os.path.getsize(f) for f in files)


def preamble(args):
    # The extension builds its cache on first access and reads these settings at
    # that moment; setting them after the first read silently does nothing.
    lines = [
        ".timer on",
        ".mode csv",
        ".bail on",
        f"SET threads={args.threads};",
    ]
    if args.memory_limit:
        lines.append(f"SET memory_limit='{args.memory_limit}';")
    lines.append("SET enable_external_file_cache=false;")

    if args.config == "nocache":
        lines.append("SET cache_httpfs_type='noop';")
    else:
        lines.append("SET cache_httpfs_type='in_mem';")
        lines.append(f"SET cache_httpfs_cache_block_size={args.block_size};")
        if args.storage == "extension":
            blocks = max(1, args.cache_bytes // args.block_size)
            lines.append("SET cache_httpfs_in_mem_cache_storage='extension';")
            lines.append(f"SET cache_httpfs_max_in_mem_cache_block_count={blocks};")
        else:
            lines.append("SET cache_httpfs_in_mem_cache_storage='policy';")
            lines.append(f"SET cache_httpfs_in_mem_cache_bytes={args.cache_bytes};")
            lines.append(f"SET cache_httpfs_eviction_policy='{args.policy}';")
            lines.append(f"SET cache_httpfs_policy_scope='{args.scope}';")
            if args.file_policies:
                lines.append(f"SET cache_httpfs_file_policies='{args.file_policies}';")
        lines.append(f"SET cache_httpfs_chunking='{args.chunking}';")
        lines.append("SET cache_httpfs_in_mem_cache_block_timeout_millisec=86400000;")

    lines.append(f"SET cache_httpfs_sim_latency_us={args.sim_latency_us};")
    lines.append(f"SET cache_httpfs_sim_bandwidth_gbps={args.sim_bandwidth_gbps};")
    lines.append("SET cache_httpfs_profile_type='temp';")
    lines.append("SELECT cache_httpfs_wrap_cache_filesystem('LocalFileSystem');")
    return lines


def layout_registration(args):
    if args.config == "nocache" or args.chunking != "column_chunk":
        return []
    files = sorted(glob.glob(os.path.join(args.data, "*.parquet")))
    return [f"SELECT cache_httpfs_register_parquet_layout('{f}');" for f in files]


def build_script(args, queries, stats_path, access_path):
    lines = preamble(args) + layout_registration(args)
    for it in range(args.repeat):
        for path in queries:
            sql = open(path).read().replace("{source}", args.data.rstrip("/"))
            name = os.path.splitext(os.path.basename(path))[0]
            lines.append(f".print {MARKER} {name} {it}")
            lines.append(sql.strip())
    lines.append(
        f"COPY (SELECT * FROM cache_httpfs_policy_stats()) TO '{stats_path}' (FORMAT CSV, HEADER);"
    )
    lines.append(
        f"COPY (SELECT * FROM cache_httpfs_cache_access_info_query()) "
        f"TO '{access_path}' (FORMAT CSV, HEADER);"
    )
    return "\n".join(lines) + "\n"


def read_csv(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out = {}
            for k, v in row.items():
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    out[k] = v
            rows.append(out)
    return rows


def parse_timings(stdout):
    timings, current = [], None
    for line in stdout.splitlines():
        if line.startswith(f".print {MARKER}") or line.startswith(MARKER):
            parts = line.replace(".print ", "").split()
            if current:
                timings.append(current)
            current = {"query": parts[1], "iteration": int(parts[2]), "seconds": 0.0}
            continue
        m = TIMER_RE.match(line)
        if m and current:
            current["seconds"] += float(m.group(1))
    if current:
        timings.append(current)
    return timings


def drop_page_cache():
    try:
        subprocess.run(["sync"], check=True)
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
        return True
    except Exception:
        pass
    try:
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
            check=True,
            capture_output=True,
        )
        return True
    except Exception as e:
        sys.exit(f"could not drop the OS page cache ({e}); needs passwordless sudo")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, choices=["nocache", "cache_httpfs", "ucache"])
    p.add_argument("--workload", required=True, help="name used in the result file")
    p.add_argument("--queries", required=True, nargs="+",
                   help="a .sql file with one or more queries, a glob, or a directory")
    p.add_argument("--data", required=True, help="directory holding the parquet files")
    p.add_argument("--duckdb", default=os.environ.get("DUCKDB_CACHE_HTTPFS", "duckdb"))
    p.add_argument("--out", required=True)
    p.add_argument("--threads", type=int, default=16)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--memory-limit", default="")

    # Cache capacity: a percentage of the dataset size, or absolute bytes.
    p.add_argument("--cache-pct", type=float, default=None,
                   help="capacity as %% of total .parquet bytes under --data")
    p.add_argument("--cache-bytes", type=int, default=None)

    p.add_argument("--block-size", type=int, default=524288)
    p.add_argument("--storage", default="policy", choices=["policy", "extension"])
    p.add_argument("--chunking", default=None, choices=["fixed", "column_chunk"],
                   help="default: fixed for cache_httpfs, column_chunk for ucache")
    p.add_argument("--policy", default="lru")
    p.add_argument("--scope", default="global", choices=["global", "per_file"])
    p.add_argument("--file-policies", default="")

    # Simulated miss cost. 1400/0.7 is calibrated against MinIO over 100 GbE
    # (validated end-to-end within 4-7%); 20000-40000 us is the range for S3.
    p.add_argument("--sim-latency-us", type=int, default=1400)
    p.add_argument("--sim-bandwidth-gbps", type=float, default=0.7)

    p.add_argument("--drop-page-cache", action="store_true")
    args = p.parse_args()

    if args.chunking is None:
        args.chunking = "column_chunk" if args.config == "ucache" else "fixed"
    if args.config == "cache_httpfs":
        args.policy = "lru"
        args.scope = "global"

    data_size = dataset_bytes(args.data)
    if args.cache_bytes is None:
        pct = 10.0 if args.cache_pct is None else args.cache_pct
        args.cache_bytes = int(data_size * pct / 100.0)
    args.cache_pct = 100.0 * args.cache_bytes / data_size

    queries = collect_queries(args.queries)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    dropped = drop_page_cache() if args.drop_page_cache else None

    tmpdir = tempfile.mkdtemp(prefix="policies_")
    stats_p = os.path.join(tmpdir, "policy_stats.csv")
    access_p = os.path.join(tmpdir, "access.csv")
    script_p = os.path.join(tmpdir, "workload.sql")
    open(script_p, "w").write(build_script(args, queries, stats_p, access_p))

    started = time.time()
    proc = subprocess.run(
        [args.duckdb, "-init", "/dev/null"],
        stdin=open(script_p),
        capture_output=True,
        text=True,
    )
    wall = time.time() - started
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        sys.exit(f"duckdb exited {proc.returncode}")
    errors = [l for l in proc.stdout.splitlines() if "Error:" in l]
    if errors:
        sys.stderr.write("\n".join(errors[:20]) + "\n")
        sys.exit("workload produced errors")

    timings = parse_timings(proc.stdout)
    policy_stats = read_csv(stats_p)
    access_stats = read_csv(access_p)
    shutil.rmtree(tmpdir, ignore_errors=True)

    totals = {}
    for t in timings:
        totals.setdefault(t["iteration"], 0.0)
        totals[t["iteration"]] += t["seconds"]

    data_row = next((r for r in access_stats if r.get("cache_type") == "data"), {})
    result = {
        "config": args.config,
        "workload": args.workload,
        "queries": [os.path.basename(q) for q in queries],
        "data": args.data,
        "data_bytes": data_size,
        "threads": args.threads,
        "repeat": args.repeat,
        "cache_bytes": args.cache_bytes if args.config != "nocache" else 0,
        "cache_pct": round(args.cache_pct, 2) if args.config != "nocache" else 0,
        "block_size": args.block_size,
        "storage": args.storage if args.config != "nocache" else None,
        "chunking": args.chunking if args.config != "nocache" else None,
        "policy": args.policy if args.config != "nocache" else None,
        "scope": args.scope if args.config != "nocache" else None,
        "file_policies": args.file_policies or None,
        "sim_latency_us": args.sim_latency_us,
        "sim_bandwidth_gbps": args.sim_bandwidth_gbps,
        "page_cache_dropped": dropped,
        "wall_seconds": wall,
        "seconds_per_iteration": [totals[i] for i in sorted(totals)],
        "cold_seconds": totals.get(0),
        "warm_seconds": totals.get(max(totals)) if len(totals) > 1 else None,
        "timings": timings,
        "policy_stats": policy_stats,
        "data_cache_hits": data_row.get("cache_hit_count"),
        "data_cache_misses": data_row.get("cache_miss_count"),
        "bytes_from_misses": data_row.get("bytes_read_from_misses (data cache)"),
    }
    json.dump(result, open(args.out, "w"), indent=2)

    warm = result["warm_seconds"]
    print(
        f"{args.config:13s} {args.workload:26s} "
        f"cold={result['cold_seconds']:.2f}s "
        f"warm={'-' if warm is None else format(warm, '.2f')}s "
        f"chunking={result['chunking']} policy={result['policy']} "
        f"cache={args.cache_bytes/1e9:.2f}GB ({result['cache_pct']}%) -> {args.out}"
    )


if __name__ == "__main__":
    main()
