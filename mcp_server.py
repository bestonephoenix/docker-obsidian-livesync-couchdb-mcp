#!/usr/bin/env python3
"""
MCP StreamableHTTP server for Obsidian vault access via CouchDB LiveSync.

DEBUG: logging salt discovery to isolate Step 5 → Step 4 regression.

Agents connect at: http://<host>:8000/mcp (StreamableHTTP)
"""

import asyncio
import contextvars
import logging
import os
import sys
from typing import Optional

logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

os.environ["FASTMCP_HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
os.environ["FASTMCP_PORT"] = os.environ.get("MCP_PORT", "8000")

from obsidian_self_mcp.server import mcp, _get_client
from obsidian_self_mcp.client import ObsidianVaultClient

passphrase_ctx = contextvars.ContextVar("livesync_passphrase", default="")

_pbkdf2_salt: Optional[str] = None


def _get_passphrase() -> str:
    return passphrase_ctx.get() or os.environ.get("LIVESYNC_PASSPHRASE", "")


# ── Middleware ─────────────────────────────────────────────────────


def _header_middleware(app):
    async def wrapper(scope, receive, send):
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
    return wrapper


# ── Salt discovery ─────────────────────────────────────────────────

async def _discover_pbkdf2_salt() -> Optional[str]:
    import base64
    logging.debug("_discover_pbkdf2_salt: calling _get_client()")
    vault_client = _get_client()
    http = await vault_client._get_client()
    logging.debug("_discover_pbkdf2_salt: GET _local/obsidian_livesync_sync_parameters")
    resp = await http.get("_local/obsidian_livesync_sync_parameters")
    logging.debug("_discover_pbkdf2_salt: status=%s", resp.status_code)
    if resp.status_code != 200:
        return None
    doc = resp.json()
    salt_b64 = doc.get("pbkdf2salt", "")
    logging.debug("_discover_pbkdf2_salt: salt_b64=%s...", salt_b64[:20] if salt_b64 else "(none)")
    if not salt_b64:
        return None
    return base64.b64decode(salt_b64).hex()


# ── Patches ────────────────────────────────────────────────────────

async def _patched_fetch_chunks(self, chunk_ids):
    httpx_client = await self._get_client()
    resp = await httpx_client.post(
        "/_all_docs", json={"keys": chunk_ids}, params={"include_docs": "true"}
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
                    data = "[ENCRYPTED — set passphrase (header or env)]"
            result[row["id"]] = data
    return result

ObsidianVaultClient._fetch_chunks = _patched_fetch_chunks

_LIVESYNC_INTERNAL_PREFIXES = ("i:", "ps:", "ix:", "_design/", "_local/")

async def _patched_get_all_file_docs(self):
    httpx_client = await self._get_client()
    all_rows = []
    resp = await httpx_client.get(
        "/_all_docs",
        params={"include_docs": "true", "endkey": '"h:"', "inclusive_end": "false"},
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))
    resp = await httpx_client.get(
        "/_all_docs", params={"include_docs": "true", "startkey": '"h:~"'},
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))
    seen: dict[str, dict] = {}
    for row in all_rows:
        doc = row.get("doc")
        if not doc:
            continue
        if doc.get("_deleted") or row.get("value", {}).get("deleted"):
            continue
        if doc.get("type") not in ("plain", "newnote") or "children" not in doc:
            continue
        doc_id = doc.get("_id", "")
        if any(doc_id.startswith(p) for p in _LIVESYNC_INTERNAL_PREFIXES):
            continue
        path = doc.get("path", doc_id)
        existing = seen.get(path)
        if existing is None or doc.get("mtime", 0) > existing.get("mtime", 0):
            seen[path] = doc
    return list(seen.values())

ObsidianVaultClient._get_all_file_docs = _patched_get_all_file_docs


# ── Startup ────────────────────────────────────────────────────────

async def _startup():
    global _pbkdf2_salt
    logging.debug("_startup: begin")

    salt = os.environ.get("LIVESYNC_PBKDF2_SALT", "")
    logging.debug("_startup: LIVESYNC_PBKDF2_SALT=%s", salt[:10] if salt else "(not set)")
    if not salt:
        logging.debug("_startup: entering salt discovery loop")
        for attempt in range(1, 8):
            await asyncio.sleep(3)
            try:
                salt = await _discover_pbkdf2_salt()
                if salt:
                    logging.debug("_startup: salt discovered on attempt %d", attempt)
                    break
            except Exception as e:
                logging.debug("_startup: attempt %d failed: %s", attempt, e)

    if salt:
        _pbkdf2_salt = salt
        logging.debug("_startup: _pbkdf2_salt set, len=%d", len(salt))
        print("Salt discovered. Vault ready.", file=sys.stderr, flush=True)
    else:
        logging.warning("_startup: salt NOT discovered")

    try:
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    except AttributeError:
        pass

    logging.debug("_startup: calling mcp.streamable_http_app()")
    app = mcp.streamable_http_app()
    logging.debug("_startup: app created, type=%s", type(app).__name__)
    return app


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    logging.debug("main: calling asyncio.run(_startup())")
    app = asyncio.run(_startup())
    logging.debug("main: wrapping with _header_middleware")
    app = _header_middleware(app)
    logging.debug("main: calling uvicorn.run")
    print(
        f"Obsidian MCP server on {host}:{port}/mcp (DEBUG: salt discovery logging)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
