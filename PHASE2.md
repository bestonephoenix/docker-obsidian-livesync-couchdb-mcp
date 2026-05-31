# Phase 2: Header-based passphrase

## Goal

Move `LIVESYNC_PASSPHRASE` from Docker environment variable to Hermes config HTTP header, removing it from the compose file entirely.

## Current state (Phase 1)

```
.env → docker-compose.yml → container env → os.environ → _credentials dict
```

Passphrase lives in `.env` and is mounted into the container at startup. Works but the secret is in Docker's environment space.

## Target state (Phase 2)

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  obsidian:
    url: "http://host:8000/mcp"
    headers:
      X-Livesync-Passphrase: "your-passphrase"
```

Hermes forwards custom `headers` with every StreamableHTTP request. The passphrase lives only in Hermes config — never touches Docker.

## Implementation

### Server-side (mcp_server.py)

1. **Add middleware** — Starlette `BaseHTTPMiddleware` that extracts `X-Livesync-Passphrase` from POST request headers
2. **ContextVar** — store per-request passphrase in `contextvars.ContextVar` (async-safe, per-request isolation)
3. **Update decryption patch** — `_patched_fetch_chunks` reads from ContextVar instead of module-level `_credentials` dict
4. **Keep env var fallback** — if no header, fall back to `LIVESYNC_PASSPHRASE` env var for backward compat
5. **Salt discovery** — PBKDF2 salt still discovered from CouchDB at startup (unchanged)

### Client-side (Hermes config)

```yaml
mcp_servers:
  obsidian:
    url: "http://couchdb-obsidian-livesync:8000/mcp"
    timeout: 120
    headers:
      X-Livesync-Passphrase: "${LIVESYNC_PASSPHRASE}"
```

### After verification

- Remove `LIVESYNC_PASSPHRASE` from `docker-compose.yml` and `.env.example`
- Update README to document the header approach as primary
- Keep env var as fallback path in code

## Risk

ContextVar propagation through FastMCP's internal asyncio task structure needs verification. MCP tools are called from POST handlers — ContextVars should propagate within the same asyncio task, but FastMCP may spawn sub-tasks for tool execution.

## Prerequisites

- [x] StreamableHTTP transport (Phase 1 completed May 2026)
- [x] Hermes supports `headers` in MCP server config (confirmed in `native-mcp` skill — StreamableHTTP forwards custom headers with every request)
