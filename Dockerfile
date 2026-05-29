FROM oleduc/docker-obsidian-livesync-couchdb:latest

# ── Metadata ──────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="Obsidian LiveSync with MCP Server"
LABEL org.opencontainers.image.description="A Docker container that configures CouchDB specifically for use with Obsidian LiveSync, automating the setup process by parsing the bash script provided by obsidian-livesync's maintainer with integrated MCP server for AI agent vault access"
LABEL org.opencontainers.image.url="https://github.com/bestonephoenix/docker-obsidian-livesync-couchdb-mcp"
LABEL org.opencontainers.image.source="https://github.com/bestonephoenix/docker-obsidian-livesync-couchdb-mcp"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.authors="bestonephoenix"
LABEL org.opencontainers.image.vendor="bestonephoenix"

# ── Python + Supervisor ───────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    git \
    python3 \
    python3-pip \
    python3-venv \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# ── MCP server (obsidian-self-mcp by @suhasvemuri) ────────────────────
# Not on PyPI — install directly from GitHub
# https://github.com/suhasvemuri/obsidian-self-mcp
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install git+https://github.com/suhasvemuri/obsidian-self-mcp.git

# __ Removed Livesync since MCP can speak directly to the DB ____________

# ── Our files ──────────────────────────────────────────────────────────
COPY mcp_sse_server.py /scripts/mcp_sse_server.py
COPY supervisord.conf /etc/supervisor/conf.d/obsidian.conf

EXPOSE 5984 8000

# supervisord runs both CouchDB and the MCP SSE server
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
