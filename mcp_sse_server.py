#!/usr/bin/env python3
"""
MCP SSE server for Obsidian vault access via CouchDB LiveSync.

Passphrase via LIVESYNC_PASSPHRASE env var only.
PBKDF2 salt auto-discovered from CouchDB at startup.
No passphrase ever touches the agent.

Agents connect at: http://<host>:8000/sse
"""

import asyncio
import os
import sys
import logging
from typing import Optional

# Force host BEFORE FastMCP import
os.environ["FASTMCP_HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
os.environ["FASTMCP_PORT"] = os.environ.get("MCP_PORT", "8000")

from obsidian_self_mcp.server import mcp, _get_client
from obsidian_self_mcp.client import ObsidianVaultClient

# ── Session state ──────────────────────────────────────────────────

_credentials: dict = {}


# ── Auto-discover PBKDF2 salt from CouchDB ────────────────────────

async def _discover_pbkdf2_salt() -> Optional[str]:
    """Find PBKDF2 salt in CouchDB _local/ documents (64-char hex string)."""
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
                        logging.warning("Decrypt failed for chunk %s: %s", row["id"], e)
                else:
                    data = (
                        "[ENCRYPTED — set LIVESYNC_PASSPHRASE env var "
                        "and restart the container]"
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

    # Auto-unlock: discover salt and set credentials at startup
    passphrase = os.environ.get("LIVESYNC_PASSPHRASE", "")
    if passphrase:
        salt = os.environ.get("LIVESYNC_PBKDF2_SALT", "")
        if not salt:
            print("Discovering PBKDF2 salt from CouchDB...", file=sys.stderr)
            salt = asyncio.run(_discover_pbkdf2_salt())
        if salt:
            _credentials = {"passphrase": passphrase, "pbkdf2_salt": salt}
            print("Vault UNLOCKED — encryption passphrase set.", file=sys.stderr)
        else:
            print(
                "WARNING: Could not discover PBKDF2 salt. "
                "Set LIVESYNC_PBKDF2_SALT env var.",
                file=sys.stderr,
            )
    else:
        print(
            "Vault is ENCRYPTED — set LIVESYNC_PASSPHRASE env var and restart.",
            file=sys.stderr,
        )

    app = mcp.sse_app()
    print(f"Obsidian MCP server starting on {host}:{port}/sse", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
