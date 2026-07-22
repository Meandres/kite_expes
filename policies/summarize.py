#!/usr/bin/env python3
"""Print a comparison table from the JSON results of run_experiment.py."""

import argparse
import glob
import json
import os

CONFIG_ORDER = {"nocache": 0, "cache_httpfs": 1, "ucache": 2}


def fmt_bytes(n):
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def fmt_secs(x):
    return "-" if x is None else f"{x:.2f}"


def print_table(headers, rows, aligns=None, sep="  "):
    """Columns sized to their widest cell; workload names vary a lot in length."""
    aligns = aligns or [">"] * len(headers)
    widths = [
        max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
        for i, h in enumerate(headers)
    ]
    line = sep.join(f"{h:{a}{w}}" for h, a, w in zip(headers, aligns, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print(sep.join(f"{str(c):{a}{w}}" for c, a, w in zip(r, aligns, widths)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("results", nargs="+", help="JSON files or globs")
    p.add_argument("--per-group", action="store_true",
                   help="also break ucache runs down by policy group")
    args = p.parse_args()

    paths = []
    for r in args.results:
        paths += sorted(glob.glob(r)) if any(c in r for c in "*?[") else [r]
    runs = [json.load(open(p)) for p in paths if os.path.exists(p)]
    runs = [r for r in runs if isinstance(r, dict) and "workload" in r and "config" in r]
    if not runs:
        raise SystemExit("no result files found")
    runs.sort(key=lambda r: (r["workload"], CONFIG_ORDER.get(r["config"], 9)))

    headers = ["workload", "config", "store", "chunking", "policy", "scope", "cache",
               "cold s", "warm s", "hits", "misses", "evict", "entry min", "entry max"]
    aligns = ["<", "<", "<", "<", "<", "<"] + [">"] * 8
    rows = []
    by_workload = {}
    for r in runs:
        groups = r.get("policy_stats") or []
        hits = sum(g["hits"] for g in groups) or (r.get("data_cache_hits") or 0)
        misses = sum(g["misses"] for g in groups) or (r.get("data_cache_misses") or 0)
        evictions = sum(g["evictions"] for g in groups) if groups else "-"
        mins = [g["min_entry_bytes"] for g in groups if g["min_entry_bytes"]]
        maxs = [g["max_entry_bytes"] for g in groups if g["max_entry_bytes"]]
        cache = fmt_bytes(r["cache_bytes"])
        if r.get("cache_pct"):
            cache = f"{cache} ({r['cache_pct']:g}%)"
        rows.append([
            r["workload"], r["config"], r.get("storage") or "-",
            r["chunking"] or "-", r["policy"] or "-",
            r["scope"] or "-", cache,
            fmt_secs(r["cold_seconds"]), fmt_secs(r["warm_seconds"]),
            hits, misses, evictions,
            fmt_bytes(min(mins)) if mins else "-",
            fmt_bytes(max(maxs)) if maxs else "-",
        ])
        by_workload.setdefault(r["workload"], {})[r["config"]] = r
    print_table(headers, rows, aligns)

    print()
    for workload, byconf in by_workload.items():
        base = byconf.get("nocache")
        for cfg in ("cache_httpfs", "ucache"):
            run = byconf.get(cfg)
            if base and run and base.get("warm_seconds") and run.get("warm_seconds"):
                sp = base["warm_seconds"] / run["warm_seconds"]
                print(f"{workload}: {cfg} warm speedup vs nocache = {sp:.2f}x")
        stock, ours = byconf.get("cache_httpfs"), byconf.get("ucache")
        if stock and ours and stock.get("warm_seconds") and ours.get("warm_seconds"):
            print(f"{workload}: ucache vs cache_httpfs = "
                  f"{stock['warm_seconds'] / ours['warm_seconds']:.2f}x")

    ucache_runs = [r for r in runs if r["config"] == "ucache"]
    total_evictions = sum(
        sum(g["evictions"] for g in (r.get("policy_stats") or [])) for r in ucache_runs
    )
    if ucache_runs and total_evictions == 0:
        print()
        print("WARNING: zero evictions across all runs -- the cache never filled, so "
              "these results cannot distinguish replacement policies. Lower --cache-bytes.")

    if args.per_group:
        group_rows = []
        for r in runs:
            for g in r.get("policy_stats") or []:
                total = g["hits"] + g["misses"]
                group_rows.append([
                    r["workload"], r["config"], g["policy_group"], g["policy"],
                    fmt_bytes(g["capacity_bytes"]), fmt_bytes(g["used_bytes"]),
                    g["entry_count"], g["hits"], g["misses"],
                    f"{100*g['hits']/total:.1f}%" if total else "-",
                    g["evictions"],
                ])
        if group_rows:
            print()
            print_table(
                ["workload", "config", "group", "policy", "capacity", "used",
                 "entries", "hits", "misses", "hit%", "evict"],
                group_rows,
                ["<", "<", "<", "<"] + [">"] * 7,
            )


if __name__ == "__main__":
    main()
