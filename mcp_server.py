#!/usr/bin/env python3
"""
MCP StreamableHTTP server for Obsidian vault access via CouchDB LiveSync.

Passphrase via LIVESYNC_PASSPHRASE env var (header support: WIP).

Agents connect at: http://<host>:8000/mcp (StreamableHTTP)
"""

import asyncio
import contextvars
import logging
import os
import sys
from typing import Optional

os.environ["FASTMCP_HOST"] = os.environ.get("MCP_HOST", "0.0.0.0")
os.environ["FASTMCP_PORT"] = os.environ.get("MCP_PORT", "8000")

from obsidian_self_mcp.server import mcp, _get_client
from obsidian_self_mcp.client import ObsidianVaultClient

passphrase_ctx = contextvars.ContextVar("livesync_passphrase", default="")

_pbkdf2_salt: Optional[str] = None


def _get_passphrase() -> str:
    return passphrase_ctx.get() or os.environ.get("LIVESYNC_PASSPHRASE", "")


# ── Step 3: HTTP-type check only, no ContextVar ops ────────────────


def _passthrough_middleware(app):
    async def wrapper(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        # TODO: extract header → passphrase_ctx.set()
        await app(scope, receive, send)

    return wrapper


# ── Salt discovery ─────────────────────────────────────────────────

async def _discover_pbkdf2_salt() -> Optional[str]:
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
    return base64.b64decode(salt_b64).hex()


# ── Patch _fetch_chunks ────────────────────────────────────────────

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


# ── Patch _get_all_file_docs ───────────────────────────────────────

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
        "/_all_docs",
        params={"include_docs": "true", "startkey": '"h:~"'},
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))
    seen: dict[str, dict] = {}
    skipped_deleted = 0
    skipped_internal = 0
    skipped_duplicate = 0
    for row in all_rows:
        doc = row.get("doc")
        if not doc:
            continue
        if doc.get("_deleted") or row.get("value", {}).get("deleted"):
            skipped_deleted += 1
            continue
        if doc.get("type") not in ("plain", "newnote") or "children" not in doc:
            continue
        doc_id = doc.get("_id", "")
        if any(doc_id.startswith(p) for p in _LIVESYNC_INTERNAL_PREFIXES):
            skipped_internal += 1
            continue
        path = doc.get("path", doc_id)
        existing = seen.get(path)
        if existing is None or doc.get("mtime", 0) > existing.get("mtime", 0):
            if existing is not None:
                skipped_duplicate += 1
            seen[path] = doc
        else:
            skipped_duplicate += 1
    if skipped_deleted or skipped_internal or skipped_duplicate:
        logging.info(
            "_get_all_file_docs: filtered %d deleted, %d internal, "
            "%d duplicate — %d unique",
            skipped_deleted, skipped_internal, skipped_duplicate, len(seen),
        )
    return list(seen.values())


ObsidianVaultClient._get_all_file_docs = _patched_get_all_file_docs


# ── Startup ────────────────────────────────────────────────────────

async def _startup():
    global _pbkdf2_salt
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
            _pbkdf2_salt = salt
            print("Vault UNLOCKED.", file=sys.stderr, flush=True)
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
    app = _passthrough_middleware(app)
    print(
        f"Obsidian MCP server on {host}:{port}/mcp (Step 3: http-check only)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
