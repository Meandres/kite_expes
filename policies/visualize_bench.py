#!/usr/bin/env python3
"""Visualize DuckDB benchmark results from block_access_log.csv and trace_result.csv.

Usage:
    python visualize_bench.py [--blocks PATH] [--trace PATH] [--smooth MS] [-o OUT]
"""

import argparse
import os
import sys

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.gridspec import GridSpec


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_data(block_log_path: str, trace_result_path: str):
    if not os.path.exists(block_log_path):
        sys.exit(f"Error: block access log not found: {block_log_path}")
    if not os.path.exists(trace_result_path):
        sys.exit(f"Error: trace result not found: {trace_result_path}")

    blocks = pd.read_csv(block_log_path)
    # timestamp_us → ms (float)
    blocks["timestamp_ms"] = blocks["timestamp_us"] / 1_000.0
    # ensure cache_hit is int
    blocks["cache_hit"] = blocks["cache_hit"].astype(int)

    trace = pd.read_csv(trace_result_path)
    return blocks, trace


# ---------------------------------------------------------------------------
# Hit-rate computation
# ---------------------------------------------------------------------------

def compute_hitrate_per_ms(blocks: pd.DataFrame) -> pd.DataFrame:
    """Return (file, ts_bin_ms, hits, total, hitrate) aggregated at 1 ms bins."""
    df = blocks.copy()
    df["ts_bin_ms"] = df["timestamp_ms"].astype(int)
    agg = (
        df.groupby(["file", "ts_bin_ms"])["cache_hit"]
        .agg(hits="sum", total="count")
        .reset_index()
    )
    agg["hitrate"] = agg["hits"] / agg["total"]
    return agg


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def print_stats(blocks: pd.DataFrame, hitrate_df: pd.DataFrame) -> None:
    sep = "=" * 72

    print(sep)
    print("BLOCK ACCESS STATISTICS PER FILE")
    print(sep)

    for fname in sorted(blocks["file"].unique()):
        fblocks = blocks[blocks["file"] == fname]
        total = len(fblocks)
        hits = fblocks["cache_hit"].sum()
        overall = hits / total if total else 0.0

        fhr = hitrate_df[hitrate_df["file"] == fname]["hitrate"]

        print(f"\n  {fname}")
        print(f"    Total block accesses : {total:,}")
        print(f"    Overall hit rate     : {overall:.3f}  ({hits:,} hits / {total:,} total)")
        print(f"    Per-ms hit rate:")
        print(f"      mean = {fhr.mean():.3f}")
        print(f"      std  = {fhr.std():.3f}")
        print(f"      min  = {fhr.min():.3f}")
        print(f"      p25  = {fhr.quantile(0.25):.3f}")
        print(f"      p50  = {fhr.quantile(0.50):.3f}")
        print(f"      p75  = {fhr.quantile(0.75):.3f}")
        print(f"      max  = {fhr.max():.3f}")

    print(f"\n{sep}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short_label(path: str) -> str:
    """Return '<parent_dir>/<basename>' to keep legend readable."""
    parts = path.rstrip("/").replace("\\", "/").split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1]


def _make_color_map(keys, cmap_name):
    keys = sorted(keys)
    cmap = plt.get_cmap(cmap_name, max(len(keys), 1))
    return {k: cmap(i) for i, k in enumerate(keys)}


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------

def make_figure(
    blocks: pd.DataFrame,
    trace: pd.DataFrame,
    hitrate_df: pd.DataFrame,
    smooth_window_ms: int = 1000,
    output_path: str | None = None,
) -> None:
    query_ids = sorted(trace["query_id"].unique())
    file_paths = sorted(hitrate_df["file"].unique())

    query_colors = _make_color_map(query_ids, "tab20")
    file_colors = _make_color_map(file_paths, "Set1")

    # Global time axis (ms)
    t_min = min(trace["start_ms"].min(), float(hitrate_df["ts_bin_ms"].min()))
    t_max = max(trace["end_ms"].max(), float(hitrate_df["ts_bin_ms"].max()))

    # -----------------------------------------------------------------------
    # Layout
    # -----------------------------------------------------------------------
    n_clients = trace["client_id"].nunique()
    gantt_height = max(4, n_clients * 0.35)
    fig_h = gantt_height + 4.5
    fig = plt.figure(figsize=(18, fig_h))
    gs = GridSpec(
        2, 1, figure=fig,
        hspace=0.40,
        height_ratios=[gantt_height, 3.5],
        top=0.93, bottom=0.07,
    )
    ax_gantt = fig.add_subplot(gs[0])
    ax_hr = fig.add_subplot(gs[1])

    # -----------------------------------------------------------------------
    # Gantt chart
    # -----------------------------------------------------------------------
    client_ids = sorted(trace["client_id"].unique())

    for y, cid in enumerate(client_ids):
        cdf = trace[trace["client_id"] == cid]
        client_type = cdf["client_type"].iloc[0]
        for _, row in cdf.iterrows():
            color = query_colors[row["query_id"]]
            ax_gantt.barh(
                y,
                row["end_ms"] - row["start_ms"],
                left=row["start_ms"],
                height=0.75,
                color=color,
                edgecolor="none",
                alpha=0.85,
            )

    ax_gantt.set_yticks(range(len(client_ids)))
    ax_gantt.set_yticklabels(
        [f"c{cid} ({trace[trace['client_id']==cid]['client_type'].iloc[0]})"
         for cid in client_ids],
        fontsize=7,
    )
    ax_gantt.set_xlim(t_min, t_max)
    ax_gantt.set_ylim(-0.6, len(client_ids) - 0.4)
    ax_gantt.invert_yaxis()
    ax_gantt.set_xlabel("Time (ms)", fontsize=9)
    ax_gantt.set_title("Query Execution Timeline", fontsize=11, fontweight="bold")
    ax_gantt.grid(axis="x", linestyle=":", linewidth=0.5, alpha=0.6)

    legend_patches = [
        mpatches.Patch(color=query_colors[qid], label=qid)
        for qid in query_ids
    ]
    ax_gantt.legend(
        handles=legend_patches,
        loc="upper right",
        fontsize=7,
        ncol=max(1, len(query_ids) // 6 + 1),
        title="Query",
        title_fontsize=8,
    )

    # -----------------------------------------------------------------------
    # Hit-rate line plot
    # -----------------------------------------------------------------------
    t_bins = np.arange(int(t_min), int(t_max) + 2)  # integer ms bins

    for fpath in file_paths:
        fdf = (
            hitrate_df[hitrate_df["file"] == fpath]
            .set_index("ts_bin_ms")["hitrate"]
            .reindex(t_bins)  # NaN where no data
        )
        # Rolling mean over NaN-sparse series; min_periods=1 to avoid NaN edges
        smoothed = fdf.rolling(
            window=smooth_window_ms, center=True, min_periods=1
        ).mean()

        ax_hr.plot(
            t_bins,
            smoothed.values,
            label=_short_label(fpath),
            color=file_colors[fpath],
            linewidth=1.5,
        )

    ax_hr.set_xlim(t_min, t_max)
    ax_hr.set_ylim(-0.05, 1.05)
    ax_hr.set_xlabel("Time (ms)", fontsize=9)
    ax_hr.set_ylabel("Hit Rate", fontsize=9)
    ax_hr.set_title(
        f"Cache Hit Rate per File  (rolling window = {smooth_window_ms} ms)",
        fontsize=11,
        fontweight="bold",
    )
    ax_hr.axhline(0.5, color="gray", linestyle="--", linewidth=0.7, alpha=0.4)
    ax_hr.grid(axis="both", linestyle=":", linewidth=0.5, alpha=0.6)
    ax_hr.legend(loc="lower right", fontsize=7, title="File", title_fontsize=8)

    # -----------------------------------------------------------------------
    # Save or show
    # -----------------------------------------------------------------------
    fig.savefig(output_path, bbox_inches="tight")
    print(f"Figure saved to {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Visualize DuckDB block cache and query execution results."
    )
    parser.add_argument("--blocks", default="block_access_log.csv",
                        help="Path to block_access_log.csv (default: %(default)s)")
    parser.add_argument("--trace", default="trace_result.csv",
                        help="Path to trace_result.csv (default: %(default)s)")
    parser.add_argument("--smooth", type=int, default=1000, metavar="MS",
                        help="Rolling window in ms for hit-rate smoothing (default: %(default)s)")
    parser.add_argument("-o", "--output", default="bench_results.pdf",
                        help="Output PDF path (default: %(default)s)")
    args = parser.parse_args()

    blocks, trace = load_data(args.blocks, args.trace)
    hitrate_df = compute_hitrate_per_ms(blocks)
    print_stats(blocks, hitrate_df)
    make_figure(blocks, trace, hitrate_df,
                smooth_window_ms=args.smooth,
                output_path=args.output)


if __name__ == "__main__":
    main()
