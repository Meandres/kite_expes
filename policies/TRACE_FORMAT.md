# Benchmark Trace Format

A trace file is a JSON document that fully describes a benchmark workload: which queries to run, how clients behave, and how many clients of each type to simulate.

Paths in `sql_file` are resolved relative to the directory containing the trace file's parent `policies/` directory (i.e. relative to the directory where the runner is invoked).

---

## Top-level structure

```json
{
  "queries":      { ... },
  "client_types": { ... },
  "simulation":   { ... }
}
```

---

## `queries`

A map from query ID to query definition. Each entry names a SQL file and provides variable substitutions to apply to it before execution.

```json
"queries": {
  "<query_id>": {
    "sql_file":  "<path to .sql file>",
    "variables": { "<placeholder>": "<value>", ... }
  }
}
```

### Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `sql_file` | string | yes | Path to the SQL file, relative to the runner's working directory |
| `variables` | object | no | Key-value pairs. Every occurrence of `{key}` in the SQL text is replaced with the corresponding value before the query is executed |

### Variable substitution

SQL files should use `{name}` as a placeholder for any value that changes between query definitions. The most common use is parameterising the data source directory:

**`queries/tpch/q01.sql`**
```sql
SELECT ...
FROM '{source}/lineitem.parquet'
WHERE ...
```

**Trace entry**
```json
"q01_tpch300": {
  "sql_file": "queries/tpch/q01.sql",
  "variables": { "source": "/scratch/ilya/tpch300" }
}
```

Multiple variables are supported. Substitution is applied in an unspecified order, so placeholders must not overlap.

### Example

```json
"queries": {
  "q01_small": {
    "sql_file": "queries/tpch/q01.sql",
    "variables": { "source": "/scratch/ilya/tpch1" }
  },
  "q18_large": {
    "sql_file": "queries/tpch/q18.sql",
    "variables": { "source": "/scratch/ilya/tpch300" }
  }
}
```

---

## `client_types`

A map from client type ID to a behavioural template. One type can be instantiated into many concurrent clients in the `simulation` section.

```json
"client_types": {
  "<client_type_id>": {
    "queries":             ["<query_id>", ...],
    "order":               "sequential" | "random",
    "inter_query_delay":   { ... },
    "rounds":              <integer>
  }
}
```

### Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `queries` | array of strings | yes | Ordered list of query IDs this client draws from |
| `order` | string | yes | `"sequential"` — step through the list in order; `"random"` — pick uniformly at random each time |
| `inter_query_delay` | object | yes | How long to wait after a query finishes before starting the next one (see below) |
| `rounds` | integer | yes | How many times to cycle through the query list. `0` means run until the simulation's `max_duration_s` is reached |

### `inter_query_delay`

Controls the think-time between queries (measured from when the previous query **finishes**).

#### `"immediate"` — no delay

```json
"inter_query_delay": { "distribution": "immediate" }
```

#### `"fixed"` — constant delay

```json
"inter_query_delay": {
  "distribution": "fixed",
  "value_ms": 500
}
```

#### `"uniform"` — random delay drawn from a uniform distribution

```json
"inter_query_delay": {
  "distribution": "uniform",
  "min_ms": 100,
  "max_ms": 1000
}
```

#### `"exponential"` — random delay drawn from an exponential distribution (models a Poisson arrival process)

```json
"inter_query_delay": {
  "distribution": "exponential",
  "mean_ms": 300
}
```

### Example

```json
"client_types": {
  "batch_analyst": {
    "queries": ["q01_large", "q06_large", "q12_large"],
    "order": "sequential",
    "inter_query_delay": { "distribution": "immediate" },
    "rounds": 3
  },
  "interactive_explorer": {
    "queries": ["q01_small", "q06_small", "q01_large"],
    "order": "random",
    "inter_query_delay": {
      "distribution": "uniform",
      "min_ms": 200,
      "max_ms": 1000
    },
    "rounds": 10
  }
}
```

---

## `simulation`

Specifies how many clients of each type to launch and global stopping conditions.

```json
"simulation": {
  "clients": [
    { "type": "<client_type_id>", "count": <integer> }
  ],
  "seed":           <integer>,
  "max_duration_s": <integer>
}
```

### Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `clients` | array | yes | List of `{ type, count }` entries. All clients across all entries start simultaneously at t=0 |
| `seed` | integer | no | RNG seed for reproducible random query order and delay sampling. Omit for a time-based seed |
| `max_duration_s` | integer | no | Hard wall-clock limit in seconds. Any client still running when the limit is reached is interrupted. Omit to let all clients run to completion |

### Example

```json
"simulation": {
  "clients": [
    { "type": "heavy_joiner",         "count": 1 },
    { "type": "batch_analyst",        "count": 2 },
    { "type": "interactive_explorer", "count": 5 }
  ],
  "seed": 42,
  "max_duration_s": 300
}
```

---

## Output

The runner emits one CSV row per completed query to stdout:

```
client_id, client_type, round, query_id, elapsed_ms, status
0, heavy_joiner, 0, q18_large, 4321.7, ok
1, batch_analyst, 0, q01_large, 812.3, ok
...
```

`status` is `ok` or `error:<message>`.

---

## Full example

See [`traces/sample_mixed.json`](traces/sample_mixed.json) for a worked example with 8 total clients across three behavioural types.
