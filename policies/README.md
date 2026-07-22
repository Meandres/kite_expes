# S3 caching for DuckDB

Three configurations, one extension
([Meandres/duck-read-cache-fs](https://github.com/Meandres/duck-read-cache-fs)),
local files:

| config | what it is |
|---|---|
| `nocache` | no caching (`cache_httpfs_type='noop'`) |
| `cache_httpfs` | fixed-size blocks, LRU (the extension as shipped) |
| `ucache` | one cache entry per Parquet column chunk, selectable eviction policy |

All three read the same local Parquet and pay the same simulated remote cost on
every cache miss:

```
delay_us = sim_latency_us + bytes / (sim_bandwidth_gbps * 1000)
```

The default `1400 / 0.7` is calibrated against MinIO served over a 100 GbE link
and validated end-to-end against that setup (uncached cold +3.7%, cached cold
+7%, cached warm −0.3%). Real S3 is around `20000–40000` µs. Known limits: sleeps
overshoot by ~100 µs/request (conservative), and there is no shared-wire cap.

## Running

```bash
nix develop .#policies
```

Every run takes a **workload** (a `.sql` file with one or more queries — use
`{source}` as the data-directory placeholder — or a directory of them) and a
**cache size as a percentage of the dataset** (total `.parquet` bytes under
`DATA`). Variables are passed through the environment:

```bash
WORKLOAD=queries/tpch/q06.sql CACHE_PCT=10 just policies::compare
SF=100 CACHE_PCT=25 REPEAT=2   just policies::sweep-chunk-size
```

| variable | default | |
|---|---|---|
| `WORKLOAD` | `queries/tpch` | .sql file or directory |
| `DATA` / `SF` | `/scratch/ilya/tpch10` | dataset directory |
| `CACHE_PCT` | `10` | cache capacity, % of dataset bytes |
| `POLICY` | `lru` | lru \| fifo \| clock \| mru |
| `LATENCY_US` / `BANDWIDTH` | `1400` / `0.7` | simulated miss cost |
| `BLOCK_SIZE` | `524288` | fixed-chunking block size |
| `REPEAT` / `THREADS` | `3` / `16` | iterations (0 = cold, last = warm) |

## The sweeps

```bash
just policies::sweep-cache-size   # CACHE_PCTS list, % of the dataset
just policies::sweep-chunk-size   # fixed blocks (BLOCK_SIZES list) vs dynamic (column chunks)
just policies::sweep-penalty      # PENALTIES list of latency_us:bandwidth_gbps pairs
just policies::sweep-policy       # POLICIES list; extend after adding policies to the extension
```

Each holds everything constant except the swept axis. Override the swept lists
through `CACHE_PCTS`, `BLOCK_SIZES`, `PENALTIES`, `POLICIES`.

Adding an eviction policy: implement `EvictionPolicy`
(`src/include/policy/*.hpp` in the extension, one file per policy), register it
in `src/policy/eviction_policy.cpp`, rebuild, then add its name to `POLICIES`.
The `OnInsert(key, size, cost_us)` hook receives the entry size and fetch cost.

## Reading the output

`summarize.py` prints one row per run: cold/warm seconds, hits, misses,
evictions, and the min/max cache entry size. It warns when total evictions are
zero — the cache held the whole working set, so such a run says nothing about
replacement. If min equals max on a `ucache`
run, entries are still on a fixed grid — the column-chunk layout was not
registered. `--per-group` breaks a `per_file` run down by policy group.

`just policies::parquet-stats` shows the column-chunk size distribution of a
dataset — the thing `sweep-chunk-size` is testing against.

## Notes

- The workload queries use `{source}/table.parquet` paths; `q15` is excluded
  from `queries/tpch/` (its generated form references a non-existent
  `revenue.parquet`).
- Settings are read once when the extension's cache is first built; the runner
  sets everything before the first read, but bear it in mind in ad-hoc shells.
