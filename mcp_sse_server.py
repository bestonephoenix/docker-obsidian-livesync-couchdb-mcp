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

# Import the FastMCP instance — all @mcp.tool() decorators
# fire at import time, registering every tool automatically.
from obsidian_self_mcp.server import mcp

if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", "8000"))
    host = os.environ.get("MCP_HOST", "0.0.0.0")

    print(f"Obsidian MCP server starting on {host}:{port}/sse", file=sys.stderr)
    mcp.run(transport="sse", host=host, port=port)
