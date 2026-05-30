#!/usr/bin/env python3
"""
MCP SSE server for Obsidian vault access via CouchDB LiveSync.

Decryption: pass passphrase via query parameter or HTTP header.
Hermes SSE: use query parameter (http://host:8000/sse?passphrase=xxx)

Agents connect at: http://<host>:8000/sse
"""

import os
import sys
import logging
from typing import Optional

# Force host BEFORE FastMCP import
os.environ["FASTMCP_HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
os.environ["FASTMCP_PORT"] = os.environ.get("MCP_PORT", "8000")

from starlette.requests import Request
from starlette.middleware.base import BaseHTTPMiddleware

from obsidian_self_mcp.server import mcp, _get_client
from obsidian_self_mcp.client import ObsidianVaultClient

# ── Session state ──────────────────────────────────────────────────

_pbkdf2_salt: Optional[str] = os.environ.get("LIVESYNC_PBKDF2_SALT") or None
_credentials: dict = {}

# Auto-unlock if env vars are set
_passphrase_from_env = os.environ.get("LIVESYNC_PASSPHRASE", "")
if _passphrase_from_env and _pbkdf2_salt:
    _credentials = {"passphrase": _passphrase_from_env, "pbkdf2_salt": _pbkdf2_salt}


# ── Auto-discovery of PBKDF2 salt ──────────────────────────────────

async def _discover_pbkdf2_salt() -> Optional[str]:
    """Try to find the PBKDF2 salt from CouchDB _local/ documents."""
    try:
        vault_client = _get_client()
        http = await vault_client._get_client()
        resp = await http.get(
            "/_all_docs",
            params={
                "startkey": '"_local/"',
                "endkey": '"_local0"',
                "include_docs": "true",
            },
        )
        resp.raise_for_status()
        for row in resp.json().get("rows", []):
            doc = row.get("doc", {})
            for val in doc.values():
                if isinstance(val, str) and len(val) == 64:
                    if all(c in "0123456789abcdefABCDEF" for c in val):
                        logging.info(
                            "Auto-discovered PBKDF2 salt from doc: %s", row["id"]
                        )
                        return val
    except Exception as e:
        logging.warning("Salt discovery failed: %s", e)
    return None


# ── Auth middleware (reads passphrase from header or query param) ───

class LiveSyncAuthMiddleware(BaseHTTPMiddleware):
    """Extract passphrase from header or query parameter on first request."""

    async def dispatch(self, request: Request, call_next):
        global _pbkdf2_salt, _credentials

        if not _credentials:
            # Check header first, then query parameter (Hermes SSE compat)
            passphrase = request.headers.get("X-Livesync-Passphrase", "")
            if not passphrase:
                passphrase = request.query_params.get("passphrase", "")
            if passphrase:
                salt = request.headers.get("X-Livesync-PBKDF2-Salt", "")
                if not salt and not _pbkdf2_salt:
                    _pbkdf2_salt = await _discover_pbkdf2_salt()
                if _pbkdf2_salt:
                    _credentials = {
                        "passphrase": passphrase,
                        "pbkdf2_salt": salt or _pbkdf2_salt,
                    }
                    logging.info("Vault unlocked (auto-discovered salt)")

        return await call_next(request)


# ── Fallback: unlock_vault tool ────────────────────────────────────

@mcp.tool()
async def unlock_vault(passphrase: str, pbkdf2_salt: str = "") -> str:
    """Unlock encrypted vault with your passphrase (fallback if headers not supported).

    Use X-Livesync-Passphrase header in MCP client config instead if possible.

    Args:
        passphrase: Your LiveSync encryption passphrase.
        pbkdf2_salt: 64-char hex PBKDF2 salt. Auto-discovered if omitted.
    """
    global _pbkdf2_salt, _credentials

    if pbkdf2_salt and len(pbkdf2_salt) == 64:
        _pbkdf2_salt = pbkdf2_salt
    elif not _pbkdf2_salt:
        _pbkdf2_salt = await _discover_pbkdf2_salt()
        if not _pbkdf2_salt:
            return (
                "ERROR: Could not auto-discover PBKDF2 salt. "
                "Provide it: unlock_vault(passphrase, pbkdf2_salt_hex). "
                "Find it in Obsidian → LiveSync settings → 64-char hex value."
            )

    _credentials = {"passphrase": passphrase, "pbkdf2_salt": _pbkdf2_salt}
    return "Vault unlocked. Encrypted notes will now decrypt."


# ── Patch _fetch_chunks for decryption ─────────────────────────────

_original_fetch = ObsidianVaultClient._fetch_chunks


async def _patched_fetch_chunks(self, chunk_ids):
    httpx_client = await self._get_client()
    resp = await httpx_client.post(
        "/_all_docs",
        json={"keys": chunk_ids},
        params={"include_docs": "true"},
    )
    resp.raise_for_status()
    result = {}
    for row in resp.json().get("rows", []):
        doc = row.get("doc")
        if doc and "data" in doc:
            data = doc["data"]
            if doc.get("e_"):
                if _credentials:
                    from livesync_decrypt import decrypt_chunk

                    try:
                        data = decrypt_chunk(
                            data,
                            _credentials["passphrase"],
                            _credentials["pbkdf2_salt"],
                        )
                    except Exception as e:
                        data = f"[DECRYPT FAILED: {e}]"
                        logging.warning(
                            "Decrypt failed for chunk %s: %s", row["id"], e
                        )
                else:
                    data = (
                        "[ENCRYPTED — set passphrase in URL "
                        "or call unlock_vault(passphrase)]"
                    )
            result[row["id"]] = data
    return result


ObsidianVaultClient._fetch_chunks = _patched_fetch_chunks

# ── Start server ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    # Disable MCP SDK DNS rebinding protection (Docker networking)
    try:
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    except AttributeError:
        pass

    # Build SSE app and add our auth middleware
    app = mcp.sse_app()
    app.add_middleware(LiveSyncAuthMiddleware)

    print(f"Obsidian MCP server starting on {host}:{port}/sse", file=sys.stderr)
    if not _credentials:
        print(
            "Vault is ENCRYPTED — add ?passphrase=xxx to URL or call unlock_vault()",
            file=sys.stderr,
        )

    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
