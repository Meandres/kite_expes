# S3 caching for DuckDB: nocache vs cache_httpfs (fixed blocks) vs uCache
# (column-chunk entries). Local Parquet + simulated remote miss cost.
#
# Specify a workload (a .sql file with one or more queries, or a directory) and
# a cache size as a % of the dataset:
#
#   WORKLOAD=queries/tpch/q06.sql CACHE_PCT=10 just policies::compare
#   SF=100 CACHE_PCT=25 just policies::sweep-chunk-size
#
# Variables are set through the ENVIRONMENT (just parses `var=value` after a
# module recipe as a recipe argument, not an override).

here         := source_directory()
duckdb       := env("DUCKDB_CACHE_HTTPFS", "duckdb")

sf           := env("SF", "10")
data         := env("DATA", "/scratch/ilya/tpch" + sf)
_wraw        := env("WORKLOAD", here + "/queries/tpch")
# Relative workload paths are taken from where just was invoked.
workload     := if _wraw =~ '^/' { _wraw } else { invocation_directory() / _wraw }
results      := env("RESULTS", here + "/results")

threads      := env("THREADS", "16")
repeat       := env("REPEAT", "3")
cache_pct    := env("CACHE_PCT", "10")
block_size   := env("BLOCK_SIZE", "524288")
policy       := env("POLICY", "lru")
scope        := env("SCOPE", "global")
file_policies := env("FILE_POLICIES", "")
chunking     := env("CHUNKING", "")
# 1400:0.7 = MinIO over 100 GbE (measured); 20000-40000 us ~= real S3.
latency_us   := env("LATENCY_US", "1400")
bandwidth    := env("BANDWIDTH", "0.7")
drop_caches  := env("DROP_CACHES", "false")

runner := "python3 " + here + "/run_experiment.py"
# Workload name for result files: basename without extension.
wname := trim_end_match(file_name(workload), ".sql")

_flags := "--data " + data + " --queries " + workload + \
          " --duckdb " + duckdb + " --threads " + threads + " --repeat " + repeat + \
          " --cache-pct " + cache_pct + " --block-size " + block_size + \
          " --sim-latency-us " + latency_us + " --sim-bandwidth-gbps " + bandwidth + \
          (if chunking != "" { " --chunking " + chunking } else { "" }) + \
          (if drop_caches == "true" { " --drop-page-cache" } else { "" })

default:
    @just --list policies

list-data:
    @du -sh /scratch/*/tpch* 2>/dev/null || echo "no TPC-H datasets found"

# One configuration: nocache | cache_httpfs | ucache.
run-one config name=wname:
    @mkdir -p {{results}}
    {{runner}} --config {{config}} --workload "{{name}}" \
        --out {{results}}/{{name}}__{{config}}.json \
        --policy {{policy}} --scope {{scope}} \
        {{ if file_policies != "" { "--file-policies '" + file_policies + "'" } else { "" } }} \
        {{_flags}}

compare: (run-one "nocache") (run-one "cache_httpfs") (run-one "ucache")
    @python3 {{here}}/summarize.py "{{results}}/{{wname}}__*.json"

# Sweep 0: cache size, as % of the dataset. uCache with everything else fixed.
cache_pcts := env("CACHE_PCTS", "5 10 25 50 100")
sweep-cache-size:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{results}}
    for pct in {{cache_pcts}}; do
        {{runner}} --config ucache --workload "{{wname}}_c${pct}pct" \
            --out "{{results}}/{{wname}}_c${pct}pct__ucache.json" \
            --policy {{policy}} --scope {{scope}} \
            --data {{data}} --queries {{workload}} --duckdb {{duckdb}} \
            --threads {{threads}} --repeat {{repeat}} --cache-pct "${pct}" \
            --block-size {{block_size}} \
            --sim-latency-us {{latency_us}} --sim-bandwidth-gbps {{bandwidth}}
    done
    python3 {{here}}/summarize.py "{{results}}/{{wname}}_c*pct__ucache.json"

# Sweep 1: cache entry size. Fixed blocks at several sizes vs dynamic
# (= entries follow the Parquet column chunks). Policy held fixed.
block_sizes := env("BLOCK_SIZES", "131072 262144 524288 1048576 2097152 4194304")
sweep-chunk-size:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{results}}
    for bs in {{block_sizes}}; do
        kb=$((bs / 1024))
        {{runner}} --config ucache --workload "{{wname}}_bs${kb}KB" \
            --out "{{results}}/{{wname}}_bs${kb}KB__ucache.json" \
            --policy {{policy}} --scope global --chunking fixed \
            --data {{data}} --queries {{workload}} --duckdb {{duckdb}} \
            --threads {{threads}} --repeat {{repeat}} --cache-pct {{cache_pct}} \
            --block-size "${bs}" \
            --sim-latency-us {{latency_us}} --sim-bandwidth-gbps {{bandwidth}}
    done
    {{runner}} --config ucache --workload "{{wname}}_dynamic" \
        --out "{{results}}/{{wname}}_dynamic__ucache.json" \
        --policy {{policy}} --scope global --chunking column_chunk \
        --data {{data}} --queries {{workload}} --duckdb {{duckdb}} \
        --threads {{threads}} --repeat {{repeat}} --cache-pct {{cache_pct}} \
        --block-size {{block_size}} \
        --sim-latency-us {{latency_us}} --sim-bandwidth-gbps {{bandwidth}}
    python3 {{here}}/summarize.py "{{results}}/{{wname}}_bs*__ucache.json" \
        "{{results}}/{{wname}}_dynamic__ucache.json"

# Sweep 2: simulated remote penalty, as latency_us:bandwidth_gbps pairs.
# All three configurations at each point.
penalties := env("PENALTIES", "1400:0.7 10000:0.5 40000:0.5")
sweep-penalty:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{results}}
    for pen in {{penalties}}; do
        lat="${pen%%:*}"; bw="${pen##*:}"
        for cfg in nocache cache_httpfs ucache; do
            {{runner}} --config "$cfg" --workload "{{wname}}_l${lat}_b${bw}" \
                --out "{{results}}/{{wname}}_l${lat}_b${bw}__${cfg}.json" \
                --policy {{policy}} --scope {{scope}} \
                --data {{data}} --queries {{workload}} --duckdb {{duckdb}} \
                --threads {{threads}} --repeat {{repeat}} --cache-pct {{cache_pct}} \
                --block-size {{block_size}} \
                --sim-latency-us "${lat}" --sim-bandwidth-gbps "${bw}"
        done
    done
    python3 {{here}}/summarize.py "{{results}}/{{wname}}_l*.json"

# Sweep 3: eviction policy, uCache with everything else held fixed.
# Add new policies to the extension, then extend this list.
policies_list := env("POLICIES", "lru fifo clock mru")
sweep-policy:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{results}}
    for p in {{policies_list}}; do
        {{runner}} --config ucache --workload "{{wname}}_${p}" \
            --out "{{results}}/{{wname}}_${p}__ucache.json" \
            --policy "${p}" --scope {{scope}} {{_flags}}
    done
    python3 {{here}}/summarize.py "{{results}}/{{wname}}_*__ucache.json"

# Column-chunk size distribution of a dataset.
parquet-stats dir=data:
    python3 {{here}}/parquet_stats.py {{dir}} --duckdb {{duckdb}}

summarize pattern=(results + "/*.json"):
    python3 {{here}}/summarize.py "{{pattern}}"

summarize-groups pattern=(results + "/*.json"):
    python3 {{here}}/summarize.py "{{pattern}}" --per-group

clean-results:
    rm -rf {{results}}
