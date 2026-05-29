#!/usr/bin/env python3
"""
MCP SSE server for Obsidian vault access via CouchDB LiveSync.

Agents connect at: http://<host>:8000/sse
"""

import os
import sys

from obsidian_self_mcp.server import mcp

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    # Disable MCP SDK's DNS rebinding protection — it rejects non-localhost
    # Host headers (like Docker container names). Not needed inside Docker.
    try:
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    except AttributeError:
        # Older MCP SDK without transport_security settings
        pass

    print(f"Obsidian MCP server starting on {host}:{port}/sse", file=sys.stderr)
    uvicorn.run(
        mcp.sse_app(),
        host=host,
        port=port,
        proxy_headers=False,
        log_level="info",
    )
