#!/usr/bin/env python3
"""Native DeepSeek Harness tool surface for the LMFDB mirror.

This is the Python half of ``dsh/lmfdb-tools``. It exposes eight of the nine
tools that ``server.py`` publishes over MCP, under the ``lmfdb_`` prefix, plus
``lmfdb_find`` — a structured row fetch that replaces raw SQL for lookups:

    no exposed tool requires the model to write SQL. ``lmfdb_run_sql`` still
    exists in ``server.py`` and stays reachable through the commented-out
    manifest entry below, but it is not registered with the agent.

Everything else takes structured arguments. In particular the ``where``
parameter of ``lmfdb_find`` / ``lmfdb_count_rows`` / ``lmfdb_table_stats`` /
``lmfdb_export_query`` accepts EITHER a JSON filter object or a raw SQL
predicate::

    {"rank": 2}                        -> "rank" = 2
    {"conductor": {"max": 1000}}       -> "conductor" <= 1000
    {"st_group": {"in": ["USp(4)"]}}   -> "st_group" IN ('USp(4)')
    {"label": {"like": "%.a1"}}        -> "label" LIKE '%.a1'

``server.py`` stays the single source of truth: metadata, the SQL guard, the
120 s statement timeout, the row caps and the nine tool bodies all live there
and are reused here unchanged.

Protocol (used by dsh/lmfdb-tools/index.js)::

    python agent_tools.py --dump-tools            -> {"tools": [...]}
    python agent_tools.py --call TOOL '{"a":1}'   -> {"exitCode","stdout","stderr"}
    python agent_tools.py --serve                 -> JSONL request/response loop
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class ToolError(Exception):
    """A user-facing argument error; reported as {"error": ...}, never a traceback."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_server = None


def srv():
    """Import server.py lazily so --dump-tools does not pay for fastapi/psycopg2."""
    global _server
    if _server is None:
        import server as module  # noqa: PLC0415  (deliberate lazy import)

        _server = module
    return _server


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def _ident(name: str) -> bool:
    """True for a safe unquoted SQL identifier (letters, digits, underscore)."""
    return bool(name) and isinstance(name, str) and name.replace("_", "").isalnum()


def _lit(value) -> str:
    """Render a Python value as an SQL literal (read-only mirror, still quoted)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


_OPS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "min": ">=",
    "max": "<=",
}

_OP_HELP = "eq/ne/gt/gte/lt/lte/min/max/in/like/ilike/isnull/notnull/includes/not"


def _column_predicate(column: str, spec) -> str:
    if not _ident(column):
        raise ToolError(f"invalid column name: {column!r}")
    quoted = f'"{column}"'
    if isinstance(spec, dict):
        parts = []
        for op, value in spec.items():
            key = str(op).lower()
            if key in _OPS:
                parts.append(f"{quoted} {_OPS[key]} {_lit(value)}")
            elif key == "in":
                if not isinstance(value, (list, tuple)) or not value:
                    raise ToolError(f'"{column}": "in" needs a non-empty list')
                parts.append(f"{quoted} IN ({', '.join(_lit(v) for v in value)})")
            elif key in ("like", "ilike"):
                operator = "LIKE" if key == "like" else "ILIKE"
                parts.append(f"{quoted} {operator} {_lit(value)}")
            elif key == "isnull":
                parts.append(f"{quoted} IS {'NULL' if value else 'NOT NULL'}")
            elif key == "notnull":
                parts.append(f"{quoted} IS {'NOT NULL' if value else 'NULL'}")
            elif key == "includes":
                parts.append(f"{_lit(value)} = ANY({quoted})")
            elif key == "not":
                parts.append(f"NOT ({quoted} = {_lit(value)})")
            else:
                raise ToolError(f'unknown operator "{op}" for column "{column}"; use {_OP_HELP}')
        return " AND ".join(parts) if parts else "TRUE"
    if spec is None:
        return f"{quoted} IS NULL"
    return f"{quoted} = {_lit(spec)}"


def build_where(where) -> str:
    """Normalise a ``where`` argument to a SQL predicate ('' when absent).

    A string starting with ``{`` is parsed as JSON filters and compiled to a
    safe conjunction; anything else is passed through as a SQL predicate so
    power users keep the original MCP behaviour.
    """
    if where is None:
        return ""
    text = str(where).strip()
    if not text:
        return ""
    if not text.startswith("{"):
        return text
    try:
        filters = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f'where is not valid JSON ({exc}); expected e.g. {{"rank": 2}} '
            f'or {{"conductor": {{"max": 1000}}}}'
        ) from exc
    if not isinstance(filters, dict) or not filters:
        raise ToolError('where JSON must be a non-empty object, e.g. {"rank": 2}')
    return " AND ".join(_column_predicate(col, spec) for col, spec in filters.items())


def _table(args) -> str:
    table = str(args.get("table_name") or "").strip()
    if not _ident(table):
        raise ToolError('table_name is required and must look like "ec_curvedata"')
    return table


def _server_call(fn_name: str, *call_args):
    """Call a server.py tool body; it already returns a JSON string."""
    return getattr(srv(), fn_name)(*call_args)


# ---------------------------------------------------------------------------
# lmfdb_export_query: structured (or SQL) query streamed to a local file
# ---------------------------------------------------------------------------

_EXPORT_FORMATS = ("csv", "jsonl")


def export_query(args) -> str:
    sql = str(args.get("sql") or "").strip()
    fmt = str(args.get("format") or "csv").strip().lower()
    if fmt not in _EXPORT_FORMATS:
        raise ToolError('format must be "csv" or "jsonl"')
    try:
        max_rows = int(args.get("max_rows") or 100000)
    except (TypeError, ValueError) as exc:
        raise ToolError("max_rows must be an integer") from exc
    max_rows = max(1, min(max_rows, 200000))

    if not sql:
        table = str(args.get("table") or args.get("table_name") or "").strip()
        if not _ident(table):
            raise ToolError('table is required and must be a valid table name, e.g. "g2c_curves"')
        raw_columns = str(args.get("columns") or "*").strip()
        if raw_columns == "*":
            column_sql = "*"
            out_columns = None
        else:
            names = [c.strip() for c in raw_columns.split(",") if c.strip()]
            if not names or not all(_ident(c) for c in names):
                raise ToolError("columns must be a comma-separated list of column names or \"*\"")
            column_sql = ", ".join(f'"{c}"' for c in names)
            out_columns = names
        clause = build_where(args.get("where"))
        sql = f'SELECT {column_sql} FROM "{table}"'
        if clause:
            sql += f" WHERE {clause}"
        sql += f" LIMIT {max_rows}"

    result = srv().run_query(sql, limit=max_rows)
    if "error" in result:
        return _js(result)

    rows = result.get("rows") or []
    columns = out_columns or result.get("columns") or []

    out_dir = str(args.get("out_dir") or os.environ.get("LMFDB_EXPORT_DIR") or "").strip()
    if not out_dir:
        out_dir = os.path.join(os.getcwd(), "lmfdb-exports")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    path = os.path.join(out_dir, f"lmfdb-{datetime.now():%Y%m%d-%H%M%S}.{fmt}")
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    else:
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    return _js(
        {
            "path": path,
            "format": fmt,
            "row_count": len(rows),
            "columns": columns,
            "bytes": os.path.getsize(path),
            "truncated": bool(result.get("truncated")),
            "note": "Data is already on disk: read it with pandas/head instead of re-querying.",
        }
    )


# ---------------------------------------------------------------------------
# lmfdb_find: structured row fetch (the SQL-free replacement for run_sql)
# ---------------------------------------------------------------------------

_FIND_DEFAULT_LIMIT = 20
_FIND_MAX_LIMIT = 1000


def find_rows(args) -> str:
    """SELECT columns FROM table WHERE <compiled filters> LIMIT n.

    This is deliberately narrower than run_sql: one table, one conjunction of
    filters, no joins or aggregation. That covers the overwhelming majority of
    "look up this object" queries without the model writing SQL.
    """
    table = str(args.get("table") or args.get("table_name") or "").strip()
    if not _ident(table):
        raise ToolError('table is required and must be a valid table name, e.g. "nf_fields"')

    raw_columns = str(args.get("columns") or "*").strip()
    if raw_columns == "*":
        column_sql = "*"
    else:
        names = [c.strip() for c in raw_columns.split(",") if c.strip()]
        if not names or not all(_ident(c) for c in names):
            raise ToolError('columns must be a comma-separated list of column names or "*"')
        column_sql = ", ".join(f'"{c}"' for c in names)

    clause = build_where(args.get("where"))
    try:
        limit = int(args.get("limit") or _FIND_DEFAULT_LIMIT)
    except (TypeError, ValueError) as exc:
        raise ToolError("limit must be an integer") from exc
    limit = max(1, min(limit, _FIND_MAX_LIMIT))

    sql = f'SELECT {column_sql} FROM "{table}"'
    if clause:
        sql += f" WHERE {clause}"
    sql += f" LIMIT {limit}"
    return _js(srv().run_query(sql, limit=limit))


# ---------------------------------------------------------------------------
# Tool manifest
#
# Eight tools are the SQL-free surface over server.py; lmfdb_find adds the
# structured row fetch that server.py only offered through run_sql.
# ---------------------------------------------------------------------------

MANIFEST = [
    {
        "name": "lmfdb_overview",
        "description": (
            "Curated map of the LMFDB: every production table grouped by mathematical "
            "section, with the hub table and grain/join notes per section. Call this "
            "first when you do not yet know which table holds an object."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "run": lambda args: _server_call("overview"),
    },
    {
        "name": "lmfdb_list_tables",
        "description": (
            "List LMFDB tables (row counts and curation notes), optionally filtered by "
            "name prefix (ec, nf, g2c, mf, gps, ...). Use it to find the exact table name "
            "before calling lmfdb_describe_table."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": 'Table-name prefix, e.g. "g2c". Empty lists everything.'},
                "status": {"type": "string", "description": 'Curation status filter; default "production".'},
            },
            "required": [],
        },
        "run": lambda args: _server_call(
            "list_tables",
            str(args.get("prefix") or ""),
            str(args.get("status") or "production"),
        ),
    },
    {
        "name": "lmfdb_search_knowls",
        "description": (
            "Full-text search over LMFDB documentation (knowls) for tables and columns. "
            "Use it when you know the mathematics (e.g. 'Sato-Tate group', 'Frobenius "
            "traces') but not which column stores it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "limit": {"type": "number", "description": "Max knowls to return (default 20)."},
            },
            "required": ["query"],
        },
        "run": lambda args: _server_call(
            "search_knowls",
            str(args.get("query") or ""),
            int(args.get("limit") or 20),
        ),
    },
    {
        "name": "lmfdb_describe_table",
        "description": (
            "Show the columns of one table with types, nullability, documented meaning "
            "(from LMFDB knowls) and typical array lengths. Call this before writing any "
            "filter: column names differ from the mathematical names (e.g. ec_curvedata "
            "has lmfdb_label and Clabel, not label)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": 'Table to describe, e.g. "nf_fields".'},
            },
            "required": ["table_name"],
        },
        "run": lambda args: _server_call("describe_table", _table(args)),
    },
    {
        "name": "lmfdb_sample_rows",
        "description": (
            "Return a few random rows of a table so you can see real value formats "
            "(labels, arrays, high-precision strings) before filtering on them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": 'Table to sample, e.g. "g2c_curves".'},
                "n": {"type": "number", "description": "Rows to return (default 5, max 50)."},
            },
            "required": ["table_name"],
        },
        "run": lambda args: _server_call("sample_rows", _table(args), int(args.get("n") or 5)),
    },
    # ---------------------------------------------------------------------
    # lmfdb_run_sql — RETIRED, kept for reference (hidden from the agent).
    #
    # Raw SQL is no longer exposed: lmfdb_find covers row lookups, and
    # lmfdb_export_query still accepts an explicit `sql` argument for joins
    # and bulk work. The implementation below is untouched and lives in
    # server.py (SELECT-only guard, 120 s timeout, row caps); uncomment this
    # block to bring the tool back.
    #
    # {
    #     "name": "lmfdb_run_sql",
    #     "description": (
    #         "LAST RESORT: run a read-only SELECT against the LMFDB mirror. Only SELECT / "
    #         "WITH / EXPLAIN are allowed. Prefer lmfdb_find, lmfdb_count_rows, "
    #         "lmfdb_table_stats or lmfdb_export_query with structured filters; reach for "
    #         "SQL only for joins, GROUP BY or expressions those tools cannot express."
    #     ),
    #     "inputSchema": {
    #         "type": "object",
    #         "properties": {
    #             "sql": {"type": "string", "description": "A SELECT query."},
    #             "limit": {"type": "number", "description": "Max rows (default 100, max 100000)."},
    #         },
    #         "required": ["sql"],
    #     },
    #     "run": lambda args: _server_call(
    #         "run_sql",
    #         str(args.get("sql") or ""),
    #         int(args.get("limit") or 100),
    #     ),
    # },
    # ---------------------------------------------------------------------
    {
        "name": "lmfdb_count_rows",
        "description": (
            "Count rows in a table, optionally filtered. The filter is structured, not "
            "SQL: where='{\"rank\": 2}' or '{\"conductor\": {\"max\": 1000}}' or "
            "'{\"st_group\": {\"in\": [\"USp(4)\",\"SU(2)xSU(2)\"]}}'. Omit where to count "
            "the whole table."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": 'Table to count, e.g. "g2c_curves".'},
                "where": {
                    "type": "string",
                    "description": (
                        'JSON filter object, e.g. {"rank": 2} or {"conductor": {"min": 16, '
                        '"max": 128}}. A raw SQL predicate is still accepted for compatibility.'
                    ),
                },
            },
            "required": ["table_name"],
        },
        "run": lambda args: _server_call("count_rows", _table(args), build_where(args.get("where"))),
    },
    {
        "name": "lmfdb_table_stats",
        "description": (
            "Statistics for one column (count, distinct_count, and for numeric columns "
            "min/max/avg/stddev), optionally filtered with the same structured where "
            "syntax as lmfdb_count_rows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": 'Table, e.g. "ec_curvedata".'},
                "column": {"type": "string", "description": 'Column, e.g. "rank".'},
                "where": {"type": "string", "description": 'JSON filter object (same syntax as lmfdb_count_rows).'},
            },
            "required": ["table_name", "column"],
        },
        "run": lambda args: _server_call(
            "table_stats",
            _table(args),
            str(args.get("column") or "").strip(),
            build_where(args.get("where")),
        ),
    },
    {
        "name": "lmfdb_find",
        "description": (
            "Fetch rows from one table with structured filters — the SQL-free way to look "
            "an object up. where is a JSON filter object, e.g. {\"conductor\": 64} or "
            "{\"degree\": 32, \"class_number\": {\"gt\": 1}} or {\"st_group\": {\"in\": "
            "[\"USp(4)\"]}}. Returns columns + rows directly, capped at limit. Use "
            "lmfdb_export_query instead when you want the rows in a file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": 'Table to read, e.g. "nf_fields".'},
                "where": {
                    "type": "string",
                    "description": 'JSON filter object, e.g. {"conductor": 64} or {"rank": {"min": 2}}; omit for an unfiltered sample.',
                },
                "columns": {
                    "type": "string",
                    "description": 'Comma-separated column list, default "*".',
                },
                "limit": {"type": "number", "description": "Max rows (default 20, max 1000)."},
            },
            "required": ["table"],
        },
        "run": find_rows,
    },
    {
        "name": "lmfdb_export_query",
        "description": (
            "Bulk export to a local file instead of into the conversation. Give table + "
            "optional where/columns for a SQL-free export, or sql for a custom SELECT. "
            "Writes CSV or JSONL under the export directory and returns its path — then "
            "read that file with pandas/pwsh instead of re-querying."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": 'Table to export, e.g. "g2c_curves".'},
                "where": {"type": "string", "description": "JSON filter object (same syntax as lmfdb_count_rows)."},
                "columns": {"type": "string", "description": 'Comma-separated column list, default "*".'},
                "sql": {"type": "string", "description": "Optional custom SELECT; overrides table/where/columns."},
                "format": {"type": "string", "enum": ["csv", "jsonl"], "description": "Output format (default csv)."},
                "max_rows": {"type": "number", "description": "Row cap (default 100000, max 200000)."},
                "out_dir": {"type": "string", "description": "Directory for the file; defaults to the configured export dir."},
            },
            "required": [],
        },
        "run": export_query,
    },
]

_BY_NAME = {entry["name"]: entry for entry in MANIFEST}


def dispatch(name: str, args) -> dict:
    entry = _BY_NAME.get(name)
    if entry is None:
        return {"exitCode": 2, "stdout": "", "stderr": f"unknown tool: {name}"}
    try:
        value = entry["run"](args if isinstance(args, dict) else {})
    except ToolError as exc:
        return {"exitCode": 0, "stdout": _js({"error": str(exc)}), "stderr": ""}
    except Exception as exc:  # noqa: BLE001  (surface, never crash the worker)
        return {"exitCode": 0, "stdout": _js({"error": f"{type(exc).__name__}: {exc}"}), "stderr": ""}
    if not isinstance(value, str):
        value = _js(value)
    return {"exitCode": 0, "stdout": value, "stderr": ""}


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--dump-tools":
        tools = [
            {
                "name": entry["name"],
                "description": entry["description"],
                "inputSchema": entry["inputSchema"],
            }
            for entry in MANIFEST
        ]
        sys.stdout.write(json.dumps({"tools": tools}, ensure_ascii=False))
        return 0

    if len(argv) >= 3 and argv[1] == "--call":
        try:
            args = json.loads(argv[3]) if len(argv) >= 4 and argv[3].strip() else {}
        except json.JSONDecodeError as exc:
            sys.stdout.write(_js({"exitCode": 2, "stdout": "", "stderr": f"bad args JSON: {exc}"}))
            return 0
        sys.stdout.write(_js(dispatch(argv[2], args)))
        return 0

    if len(argv) >= 2 and argv[1] == "--serve":
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = dispatch(str(request.get("tool") or ""), request.get("args") or {})
            response["id"] = request.get("id")
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0

    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
