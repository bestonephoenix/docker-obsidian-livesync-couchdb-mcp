#!/usr/bin/env python3
"""
MCP StreamableHTTP server for Obsidian vault access via CouchDB LiveSync.

Passphrase via LIVESYNC_PASSPHRASE env var only.
PBKDF2 salt auto-discovered from CouchDB at startup with retry.
No passphrase ever touches the agent.

Agents connect at: http://<host>:8000/mcp (StreamableHTTP)
"""

import asyncio
import logging
import os
import sys
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
                    data = "[ENCRYPTED — restart container with LIVESYNC_PASSPHRASE]"
            result[row["id"]] = data
    return result


ObsidianVaultClient._fetch_chunks = _patched_fetch_chunks


# ── Startup (async — handles retry for CouchDB readiness) ─────────

async def _startup():
    """Discover salt, unlock vault."""
    global _credentials

    passphrase = os.environ.get("LIVESYNC_PASSPHRASE", "")
    if passphrase:
        salt = os.environ.get("LIVESYNC_PBKDF2_SALT", "")
        if not salt:
            print("Waiting for CouchDB...", file=sys.stderr, flush=True)
            for attempt in range(1, 8):
                await asyncio.sleep(3)
                try:
                    salt = await _discover_pbkdf2_salt()
                    if salt:
                        break
                except Exception as e:
                    print(f"  Attempt {attempt}/7: {e}", file=sys.stderr, flush=True)
            else:
                salt = None

        if salt:
            _credentials = {"passphrase": passphrase, "pbkdf2_salt": salt}
            print("Vault UNLOCKED.", file=sys.stderr, flush=True)
        else:
            print(
                "WARNING: Could not discover PBKDF2 salt. "
                "Set LIVESYNC_PBKDF2_SALT if needed.",
                file=sys.stderr,
                flush=True,
            )
    else:
        # Unencrypted vault — nothing to do, works out of the box
        pass

    try:
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    except AttributeError:
        pass


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    asyncio.run(_startup())

    print(
        f"Obsidian MCP server starting on {host}:{port}/mcp (StreamableHTTP)",
        file=sys.stderr,
    )
    mcp.run(transport="streamable-http", host=host, port=port)
