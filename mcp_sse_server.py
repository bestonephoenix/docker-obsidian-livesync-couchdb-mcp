#!/usr/bin/env python3
"""
MCP SSE server for Obsidian vault access via CouchDB LiveSync.

Bypasses FastMCP's run() entirely — calls uvicorn directly with the
SSE Starlette app, guaranteeing host="0.0.0.0" without pydantic-settings
overhead.

Agents connect at: http://<host>:8000/sse
"""

import os
import sys

# These env vars are used by obsidian_self_mcp (CouchDB connection).
# FastMCP host is handled directly by uvicorn below — no FASTMCP_HOST needed.
from obsidian_self_mcp.server import mcp

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    print(f"Obsidian MCP server starting on {host}:{port}/sse", file=sys.stderr)

    # Bypass mcp.run() — use uvicorn directly with mcp's SSE app
    uvicorn.run(mcp._sse_app, host=host, port=port, log_level="info")
