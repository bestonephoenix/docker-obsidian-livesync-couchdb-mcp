#!/usr/bin/env python3
"""
MCP SSE server for Obsidian vault access via CouchDB LiveSync.

This thin wrapper imports the tool definitions from obsidian-self-mcp
and starts a FastMCP server using SSE (Server-Sent Events) transport
on an HTTP port, so external MCP clients (Claude Desktop, Hermes, etc.)
can connect over the network.

Agents connect at: http://<host>:8000/sse
"""

import os
import sys

# Force-set FastMCP host/port BEFORE importing — pydantic-settings reads
# FASTMCP_HOST / FASTMCP_PORT at class-definition time.
os.environ["FASTMCP_HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
os.environ["FASTMCP_PORT"] = os.environ.get("MCP_PORT", "8000")

# Import the FastMCP instance — all @mcp.tool() decorators
# fire at import time, registering every tool automatically.
from obsidian_self_mcp.server import mcp

if __name__ == "__main__":
    print(
        f"Obsidian MCP server starting on "
        f"{mcp.settings.host}:{mcp.settings.port}/sse",
        file=sys.stderr,
    )
    mcp.run(transport="sse")
    )
    mcp.run(transport="sse")
