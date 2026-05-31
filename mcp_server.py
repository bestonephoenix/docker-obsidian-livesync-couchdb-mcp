#!/usr/bin/env python3
"""
MCP StreamableHTTP server for Obsidian vault access via CouchDB LiveSync.

Passphrase via X-Livesync-Passphrase HTTP header (or LIVESYNC_PASSPHRASE env var fallback).
PBKDF2 salt auto-discovered from CouchDB at startup with retry.
No passphrase ever touches the agent.

Agents connect at: http://<host>:8000/mcp (StreamableHTTP)
"""

import asyncio
import contextvars
import logging
import os
import sys
from typing import Optional

# Force host BEFORE FastMCP import
os.environ["FASTMCP_HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
os.environ["FASTMCP_PORT"] = os.environ.get("MCP_PORT", "8000")

from obsidian_self_mcp.server import mcp, _get_client
from obsidian_self_mcp.client import ObsidianVaultClient

# ── Per-request passphrase (ContextVar, set by middleware) ─────────

passphrase_ctx = contextvars.ContextVar("livesync_passphrase", default="")

# ── Global salt (discovered once at startup) ──────────────────────

_pbkdf2_salt: Optional[str] = None


def _get_passphrase() -> str:
    """Return passphrase from request header, falling back to env var."""
    return passphrase_ctx.get() or os.environ.get("LIVESYNC_PASSPHRASE", "")


# ── ASGI middleware: extract passphrase from HTTP header ───────────

def _passphrase_middleware(app):
    """Wrap an ASGI app to extract X-Livesync-Passphrase into a ContextVar."""

    async def asgi_wrapper(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        passphrase_bytes = headers.get(b"x-livesync-passphrase", b"")
        passphrase = passphrase_bytes.decode() if passphrase_bytes else ""

        token = passphrase_ctx.set(passphrase)
        try:
            await app(scope, receive, send)
        finally:
            passphrase_ctx.reset(token)

    return asgi_wrapper


# ── Auto-discover PBKDF2 salt from CouchDB ────────────────────────

async def _discover_pbkdf2_salt() -> Optional[str]:
    """Find PBKDF2 salt from CouchDB sync params document (base64-encoded)."""
    import base64

    vault_client = _get_client()
    http = await vault_client._get_client()
    resp = await http.get("_local/obsidian_livesync_sync_parameters")
    if resp.status_code != 200:
        logging.warning("Sync params doc not found (status %s)", resp.status_code)
        return None
    doc = resp.json()
    salt_b64 = doc.get("pbkdf2salt", "")
    if not salt_b64:
        logging.warning("pbkdf2salt field not found in sync params")
        return None
    salt_bytes = base64.b64decode(salt_b64)
    return salt_bytes.hex()


# ── Patch _fetch_chunks for decryption ─────────────────────────────


async def _patched_fetch_chunks(self, chunk_ids):
    httpx_client = await self._get_client()
    resp = await httpx_client.post(
        "/_all_docs",
        json={"keys": chunk_ids},
        params={"include_docs": "true"},
    )
    resp.raise_for_status()
    result = {}
    passphrase = _get_passphrase()
    for row in resp.json().get("rows", []):
        doc = row.get("doc")
        if doc and "data" in doc:
            data = doc["data"]
            if doc.get("e_"):
                if passphrase and _pbkdf2_salt:
                    from livesync_decrypt import decrypt_chunk
                    try:
                        data = decrypt_chunk(data, passphrase, _pbkdf2_salt)
                    except Exception as e:
                        data = f"[DECRYPT FAILED: {e}]"
                        logging.warning("Decrypt failed for chunk %s: %s", row["id"], e)
                else:
                    data = (
                        "[ENCRYPTED — set X-Livesync-Passphrase header"
                        " or LIVESYNC_PASSPHRASE env var]"
                    )
            result[row["id"]] = data
    return result


ObsidianVaultClient._fetch_chunks = _patched_fetch_chunks


# ── Startup (async — handles retry for CouchDB readiness) ─────────

async def _startup():
    """Discover PBKDF2 salt, build app."""
    global _pbkdf2_salt

    # Try explicit salt first, then auto-discover from CouchDB
    salt = os.environ.get("LIVESYNC_PBKDF2_SALT", "")
    if not salt:
        print("Discovering PBKDF2 salt from CouchDB...", file=sys.stderr, flush=True)
        for attempt in range(1, 8):
            await asyncio.sleep(3)
            try:
                salt = await _discover_pbkdf2_salt()
                if salt:
                    break
            except Exception as e:
                print(f"  Attempt {attempt}/7: {e}", file=sys.stderr, flush=True)

    if salt:
        _pbkdf2_salt = salt
        print("Salt discovered. Vault ready.", file=sys.stderr, flush=True)
    else:
        print(
            "WARNING: Could not discover PBKDF2 salt. "
            "Set LIVESYNC_PBKDF2_SALT if encryption is used.",
            file=sys.stderr,
            flush=True,
        )

    try:
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    except AttributeError:
        pass

    return mcp.streamable_http_app()


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    app = asyncio.run(_startup())
    # Wrap manually — avoids add_middleware() interfering with
    # Starlette's internal middleware stack that FastMCP has already built
    app = _passphrase_middleware(app)

    print(
        f"Obsidian MCP server starting on {host}:{port}/mcp"
        " (StreamableHTTP + header auth)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
