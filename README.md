# dsh-lmfdb — native LMFDB tools for DeepSeek Harness

Read-only access to the [LMFDB](https://www.lmfdb.org) (L-functions and Modular
Forms Database) from [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness),
as nine native `lmfdb_*` tools. The agent queries the mirror with **structured
filters**; no exposed tool asks it to write SQL.

This repository is a dsh version of
[AndrewVSutherland/lmfdb-mcp](https://github.com/AndrewVSutherland/lmfdb-mcp).
The MCP server (`server.py`) and the Cloud Run deployment (`Dockerfile`) come
from that project, and `server.py` is kept **unmodified** so the repository cannot
drift from upstream. This repository adds the native Harness layer described below.

## What this project adds

| Path | Role |
|:--|:--|
| `agent_tools.py` | The nine tools as a JSON-lines worker. Imports `server.py`, so the SQL guard, statement timeout, row caps and curation metadata are reused rather than reimplemented. |
| `dsh/lmfdb-tools/` | The DeepSeek Harness bundle that registers the tools and supervises a long-lived worker. |

Design points:

- **Hosted MCP first, mirror as fallback.** Every call goes to
  `https://mcp.lmfdb.org/mcp`; if that endpoint times out, refuses, or answers
  with an MCP-level error, the same call is re-run against the read-only
  PostgreSQL mirror through `server.py`. Set `primary: postgres` to skip the
  remote endpoint entirely.
- **No SQL in the agent's way.** Eight tools take ordinary arguments, and the
  ninth (`lmfdb_find`) takes a JSON filter object. `lmfdb_run_sql` is retired:
  its manifest entry is commented out in `agent_tools.py`, while the
  implementation stays in `server.py`.
- **One source of truth.** Everything safety-relevant lives in `server.py`,
  which this fork does not edit.

## The nine tools

| Tool | What it does |
|:--|:--|
| `lmfdb_overview` | Curated map of production tables by mathematical section, with the hub table and grain/join notes per section. |
| `lmfdb_list_tables` | List tables (row estimates, curation notes), optionally by name prefix (`ec`, `nf`, `g2c`, `mf`, `gps`, ...). |
| `lmfdb_search_knowls` | Full-text search over LMFDB documentation, for when you know the mathematics but not the column. |
| `lmfdb_describe_table` | Columns with types, nullability, documented meaning and typical array lengths. |
| `lmfdb_sample_rows` | A few random rows, to see the real value formats. |
| `lmfdb_find` | Rows from one table: `table` + `where` + `columns` + `limit`. The SQL-free lookup. |
| `lmfdb_count_rows` | `COUNT(*)` with the same structured `where`. |
| `lmfdb_table_stats` | count / distinct / min / max / avg / stddev for one column, optionally filtered. |
| `lmfdb_export_query` | Bulk export to a local CSV/JSONL file; accepts an optional `sql` argument for joins and aggregation. |

Everything is read-only.

## Structured filters (`where`)

`lmfdb_find`, `lmfdb_count_rows`, `lmfdb_table_stats` and `lmfdb_export_query`
accept the same JSON filter object:

```json
{"rank": 2}                                      // = 2
{"conductor": {"min": 16, "max": 128}}           // BETWEEN
{"st_group": {"in": ["USp(4)", "SU(2)xSU(2)"]}}   // IN
{"label": {"like": "%.a1"}}                      // LIKE
{"sha": null}                                    // IS NULL
{"torsion_primes": {"includes": 2}}              // 2 = ANY(col)  (array columns)
{"rank": {"gt": 1}, "cm": true}                  // several keys are ANDed
```

Operators: `eq ne gt gte lt lte min max in like ilike isnull notnull includes not`.
Column names are validated as identifiers and string values are escaped before
the predicate is assembled. A string that does **not** start with `{` is passed
through as a raw SQL predicate, which keeps the original MCP behaviour
available.

## Bulk export

`lmfdb_export_query` writes to disk instead of into the conversation:

```json
{"table": "g2c_curves",
 "where": "{\"st_group\": \"USp(4)\"}",
 "columns": "label,cond,mw_rank",
 "format": "csv",
 "max_rows": 50000}
```

```json
{"path": "D:\\MathProver\\lmfdb-exports\\20261113-100942.csv",
 "format": "csv", "row_count": 50000, "columns": [...], "bytes": 1234567,
 "truncated": false}
```

Read the file afterwards (pandas, `head`, ...) instead of pulling rows through
the model context.

## Architecture

```
DSH host (node)  ──stdio JSONL──▶  python agent_tools.py --serve
                                          ├─ primary:   MCP over HTTPS ──▶ mcp.lmfdb.org/mcp
                                          └─ fallback:  import server.py (unmodified)
                                                          ├─ SELECT-only guard, 120 s timeout, row caps
                                                          └─ psycopg2 ──▶ devmirror.lmfdb.xyz:5432
```

`lmfdb_find` / `lmfdb_export_query` build their SELECT locally and send it to
the primary through that server's `run_sql` tool, so both backends answer the
same question; the other seven tools map 1:1 onto the hosted tools.
A fallback logs one line to stderr (`primary MCP unavailable (...)`).

The worker is spawned on first use and restarted after a crash or a timeout, so
a dead mirror degrades to a per-call error instead of a broken tool.
`agent_tools.py` also runs standalone:

```
python agent_tools.py --dump-tools            # tool manifest as JSON
python agent_tools.py --call lmfdb_find '{"table":"nf_fields","where":"{\"conductor\":64}"}'
python agent_tools.py --serve                 # JSONL request/response loop
```

Backend selection is read from the environment at worker start:
`LMFDB_PRIMARY` (`mcp` | `postgres`), `LMFDB_MCP_URL`, `LMFDB_MCP_TIMEOUT_MS`.

## Install

Requirements: Python 3 with `psycopg2` (the worker talks to PostgreSQL) and a
DeepSeek Harness profile.

Install straight from GitHub — the repository root *is* the bundle, so a pnpm
git dependency resolves it:

```powershell
pip install psycopg2-binary
plugin_manager install_bundle  target=github:Gao-Leonhard-Euler/dsh-lmfdb
```

From a local checkout (live edits, no publish step):

```powershell
pnpm pack                                        # produces dsh-lmfdb-tools-1.0.0.tgz
plugin_manager install_bundle  target=file:D:/MathProver/lmfdb-mcp/dsh-lmfdb-tools-1.0.0.tgz
```

Either way the bundle adds a `lmfdb-tools` entry to the profile's
`cordis.patch.yml` and `dsh-lmfdb-tools` to `dsh.profile.bundles`. Call
`lmfdb_overview` or `lmfdb_find` to verify.

> If the plugin was previously installed from a directory under another name,
> restart DeepSeek Harness before installing — the running process caches
> package-name → entry resolution, and the first import after a rename can
> still point at the old path.

## Configuration

`cordis.patch.yml` at the package root (defaults in parentheses):

| Key | Meaning |
|:--|:--|
| `python` (`python`) | Interpreter that has `psycopg2`. Use an absolute path: the GUI process does not necessarily carry Python on `PATH`. |
| `root` (package root) | Directory containing `agent_tools.py` and `server.py`. |
| `exportDir` (`<home>/lmfdb-exports`) | Default destination for `lmfdb_export_query`; a GitHub install lives in `node_modules`, so the default is the user's home directory. |
| `timeoutMs` (300000) | Per-call limit; a timeout kills and restarts the worker. |
| `primary` (`mcp`) | `mcp` sends every call to the hosted server first and falls back to the mirror; `postgres` always uses the mirror. |
| `mcpUrl` (`https://mcp.lmfdb.org/mcp`) | Primary endpoint. |
| `mcpTimeoutMs` (15000) | Connect + call budget for one primary attempt; exceeding it triggers the fallback. |

The database connection defaults to the public read-only mirror
`devmirror.lmfdb.xyz:5432` / `lmfdb` / `lmfdb`, and is overridden by the same
environment variables `server.py` reads: `LMFDB_HOST`, `LMFDB_PORT`,
`LMFDB_DBNAME`, `LMFDB_USER`, `LMFDB_PASSWORD`, `LMFDB_MAX_ROWS`,
`LMFDB_DEFAULT_LIMIT`.

## Safety

- Only `SELECT` / `WITH` / `EXPLAIN` reach the database; the guard is
  `server.py`'s and is applied to every query the plugin builds.
- 120 s statement timeout, bounded rows and an approximate byte budget.
- Table and column names are validated against the identifier grammar, and
  filter values are escaped, so a structured filter cannot smuggle SQL.
- The mirror itself is read-only; writes fail at the database level regardless.
- No exposed tool requires SQL. Raw SQL remains reachable only through the
  optional `sql` argument of `lmfdb_export_query`.

## The upstream MCP server

`server.py` still works as the upstream MCP server: it serves the same nine
tools over Streamable HTTP (`python server.py`, `PORT`, default 8000) and the
`Dockerfile` builds it for Google Cloud Run. The DeepSeek Harness plugin above
does not use that path — it imports the module instead — but both surfaces share
the same tool bodies, so behaviour stays identical. Deployment instructions
(Claude connectors, Cloud Run, sandbox allowlists) live in the upstream project.

## Layout

```
lmfdb-mcp/                 # repository root == the installable bundle
├── package.json           # npm metadata + dsh.bundle.patch (git-URL installs use this)
├── cordis.patch.yml       # bundle patch used when installed from a git URL
├── index.js               # tool registration + worker supervision (the entry)
├── agent_tools.py         # this fork: nine tools + JSONL worker protocol
├── server.py              # upstream MCP server, unmodified
├── Dockerfile             # upstream Cloud Run image
├── requirements.txt       # upstream Python dependencies (mcp, psycopg2-binary, uvicorn)
└── dsh/lmfdb-tools/       # local-checkout bundle (re-exports the root entry)
    ├── index.js           # `export { name, inject, apply } from '../../index.js'`
    ├── cordis.patch.yml   # bundle patch with this machine's paths
    ├── package.json
    └── README.md
```

## Troubleshooting

| Symptom | Fix |
|:--|:--|
| `ImportError: DLL load failed while importing _psycopg` | pip had just written the wheel and Windows was still scanning the DLLs; retry a few seconds later. Confirm `site-packages\psycopg2_binary.libs\libpq-*.dll` exists. |
| `lmfdb worker exited` | The worker is restarted automatically; the message carries the tail of Python's stderr. Usually a wrong `python` path or a missing `psycopg2`. |
| `column "x" does not exist` | Column names differ from the mathematics (`ec_curvedata` uses `lmfdb_label`). Run `lmfdb_describe_table` first. |
| Tools missing after a profile edit | Re-apply the bundle: `plugin_manager set_plugin include:lmfdb-tools enabled=false`, then `enabled=true`. |

## License

GPL-3.0 (matching lmfdb-lite), inherited from
[AndrewVSutherland/lmfdb-mcp](https://github.com/AndrewVSutherland/lmfdb-mcp).
