"""Primary backend for the LMFDB tools: the public MCP server.

The hosted endpoint at ``https://mcp.lmfdb.org/mcp`` speaks the same nine tools
as the local ``server.py``. When it answers, the tools use it; when it times
out, refuses, or returns an unusable payload, the caller falls back to the
read-only PostgreSQL mirror through ``server.py`` (see ``_fetch`` /
``_invoke`` in agent_tools.py).

Environment:

    LMFDB_MCP_URL         default https://mcp.lmfdb.org/mcp
    LMFDB_MCP_TIMEOUT_MS  default 15000 (connect + call, whole request)

Every failure is normalised to :class:`McpUnavailable` so callers can fall back
without distinguishing httpx/anyio/protocol errors.
"""

from __future__ import annotations

import json
import os

DEFAULT_URL = "https://mcp.lmfdb.org/mcp"
DEFAULT_TIMEOUT_MS = 15_000


class McpUnavailable(RuntimeError):
    """The remote MCP server could not be reached or returned no usable answer."""


def url() -> str:
    return (os.environ.get("LMFDB_MCP_URL") or DEFAULT_URL).strip()


def timeout_ms() -> int:
    try:
        return int(os.environ.get("LMFDB_MCP_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS


def call_tool(tool: str, args: dict | None = None) -> str:
    """Call one remote MCP tool and return its JSON text.

    Raises McpUnavailable on timeout, transport error, an MCP-level error
    response, or a payload that is not JSON.
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    timeout_s = max(1.0, timeout_ms() / 1000.0)
    captured: dict[str, str] = {}

    async def _run() -> None:
        async with streamablehttp_client(
            url(),
            timeout=timeout_s,
            sse_read_timeout=timeout_s,
            terminate_on_close=False,  # the endpoint is stateless; no DELETE needed
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool(tool, args or {})
                texts = [
                    block.text
                    for block in response.content
                    if getattr(block, "type", "") == "text"
                ]
                if not texts:
                    raise McpUnavailable(f"{tool}: empty response")
                if getattr(response, "isError", False):
                    raise McpUnavailable(f"{tool}: {texts[0][:300]}")
                captured["text"] = "\n".join(texts)

    try:
        anyio.run(_run)
    except McpUnavailable:
        raise
    except BaseException as exc:  # anyio wraps cancellation/timeouts in several types
        raise McpUnavailable(f"{type(exc).__name__}: {exc}") from exc

    text = captured.get("text", "")
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpUnavailable(f"{tool}: non-JSON response") from exc
    return text


def ping(timeout_ms_override: int | None = None) -> bool:
    """Cheap health probe. Used by the CLI/tests; never by the hot path."""
    previous = os.environ.get("LMFDB_MCP_TIMEOUT_MS")
    if timeout_ms_override is not None:
        os.environ["LMFDB_MCP_TIMEOUT_MS"] = str(timeout_ms_override)
    try:
        call_tool("overview", {})
        return True
    except Exception:
        return False
    finally:
        if timeout_ms_override is not None:
            if previous is None:
                os.environ.pop("LMFDB_MCP_TIMEOUT_MS", None)
            else:
                os.environ["LMFDB_MCP_TIMEOUT_MS"] = previous
