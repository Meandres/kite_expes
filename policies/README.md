# TPC-H Policy Benchmark

Runs multi-client TPC-H workloads against a local DuckDB instance and records
query timings and block-cache access patterns.

## Build

```bash
# Requires: g++, nlohmann_json, DuckDB built at ../../duckdb_samuel
make
```

Override the DuckDB path if needed:
```bash
make DUCKDB_DIR=/path/to/duckdb
```

## Run

```bash
./run_bench <trace.json> [--threads N] [--memory SIZE]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--threads N` | DuckDB default | Number of DuckDB worker threads |
| `--memory SIZE` | 80% of RAM | Buffer manager limit, e.g. `4GB`, `512MB` |

Example:
```bash
./run_bench traces/scan_vs_compute.json --threads 16 --memory 8GB
```

Outputs:
- `trace_result.csv` — one row per completed query (client, timing)
- `block_access_log.csv` — one row per block access (file, block ID, cache hit)

## Trace format

A trace file has three sections:

```jsonc
{
  "queries": {
    "q1": { "sql_file": "queries/tpch/q01.sql", "variables": { "source": "/data/tpch100" } }
  },
  "client_types": {
    "scanner": {
      "queries": ["q1"],
      "order": "sequential",            // or "random"
      "inter_query_delay": { "distribution": "immediate" },
      "rounds": 0                       // 0 = run until time limit
    }
  },
  "simulation": {
    "clients": [{ "type": "scanner", "count": 4 }],
    "seed": 42,
    "max_duration_s": 120
  }
}
```

SQL files use `{source}` as a placeholder for the data directory.
See [TRACE_FORMAT.md](TRACE_FORMAT.md) for the full field reference.

Ready-made traces are in `traces/`:
- `scan_vs_compute.json` — 2 scan clients (continuous) vs 6 compute clients (1 s delay)
- `sample_mixed.json` — batch analysts, interactive explorers, heavy joiners

## Visualize

```bash
python visualize_bench.py [--blocks block_access_log.csv] [--trace trace_result.csv] \
                          [--smooth MS] [-o output.pdf]
```

Produces `bench_results.pdf` with:
1. Gantt chart of query execution per client (colored by query type)
2. Cache hit-rate over time per file (rolling window, default 1000 ms)

Also prints per-file hit-rate statistics to stdout.
