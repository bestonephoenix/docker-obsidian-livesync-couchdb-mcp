# Phase 2: Header-based passphrase ✅ COMPLETED

**Completed:** May 31, 2026

## What changed

Passphrase can now be provided via HTTP header (`X-Livesync-Passphrase`) instead of (or in addition to) the Docker environment variable.

## How it works

```
Hermes config            MCP server
─────────────            ──────────
headers:                 _header_middleware
  X-Livesync-            extracts header
  Passphrase: "..."  →   stores in ContextVar
                         (per-request,
                          async-safe)
                              │
                              ▼
                         _get_passphrase()
                         ContextVar first,
                         env var fallback
                              │
                              ▼
                         _patched_fetch_chunks
                         decrypts chunks
```

## Hermes config

```yaml
mcp_servers:
  obsidian:
    url: "http://host:8000/mcp"
    timeout: 120
    headers:
      X-Livesync-Passphrase: "your-passphrase"
```

The `LIVESYNC_PASSPHRASE` env var in docker-compose.yml remains as a fallback — both methods work.

## Technical details

- **Middleware:** Raw ASGI wrapper (not BaseHTTPMiddleware) to avoid breaking SSE streaming
- **Context isolation:** `contextvars.ContextVar` — thread-safe, async-safe, per-request
- **Salt discovery:** Always runs at startup, decoupled from passphrase source
- **App wrapping:** Manual `app = middleware(app)` — avoids Starlette middleware stack conflicts

## Lessons learned

1. **BaseHTTPMiddleware breaks SSE** — it reads the full response body, which kills the long-lived GET stream used by StreamableHTTP session establishment
2. **add_middleware() interacts poorly with FastMCP** — FastMCP pre-builds its Starlette middleware stack; adding to it post-creation causes routing issues
3. **Proxy issues can masquerade as code bugs** — always test directly before debugging code
4. **Salt discovery must be decoupled from passphrase** — the salt is a vault-level property, independent of how the passphrase arrives
