#!/usr/bin/env python3
"""
MCP StreamableHTTP server for Obsidian vault access via CouchDB LiveSync.

Passphrase via X-Livesync-Passphrase HTTP header, with LIVESYNC_PASSPHRASE
env var as fallback. PBKDF2 salt auto-discovered from CouchDB at startup.

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

# ── Per-request passphrase (ContextVar, set by middleware) ─────────

passphrase_ctx = contextvars.ContextVar("livesync_passphrase", default="")

# ── Global salt (discovered once at startup) ──────────────────────

_pbkdf2_salt: Optional[str] = None


def _get_passphrase() -> str:
    """Return passphrase: ContextVar first (from header), then env var fallback."""
    return passphrase_ctx.get() or os.environ.get("LIVESYNC_PASSPHRASE", "")


# ── ASGI middleware: extract passphrase from HTTP header ───────────


def _header_middleware(app):
    """Wrap an ASGI app to extract X-Livesync-Passphrase header per request."""

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


# ── Salt discovery (always runs at startup) ────────────────────────

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
                        logging.warning(
                            "Decrypt failed for chunk %s: %s", row["id"], e
                        )
                else:
                    data = (
                        "[ENCRYPTED — set X-Livesync-Passphrase header"
                        " or LIVESYNC_PASSPHRASE env var]"
                    )
            result[row["id"]] = data
    return result


ObsidianVaultClient._fetch_chunks = _patched_fetch_chunks


# ── Patch _get_all_file_docs: dedup by filename, filter ghosts ────

_LIVESYNC_INTERNAL_PREFIXES = ("i:", "ps:", "ix:", "_design/", "_local/")


async def _patched_get_all_file_docs(self):
    """Fetch all file docs (skip chunks, design docs, index docs).

    Patched to:
    1. Exclude LiveSync internal metadata prefixes (i:, ps:, ix:)
    2. Exclude CouchDB system docs (_design/, _local/)
    3. Exclude deleted documents
    4. Exclude ghost files (empty children — parent doc with zero chunks)
    5. Deduplicate by base filename — keeps the entry with the highest mtime.
       This handles files that were moved between folders (root → subfolder)
       where the old CouchDB doc with the old path persists alongside the new one.
    """
    httpx_client = await self._get_client()

    all_rows = []

    # Range 1: docs before "h:" (excludes all chunk IDs)
    resp = await httpx_client.get(
        "/_all_docs",
        params={
            "include_docs": "true",
            "endkey": '"h:"',
            "inclusive_end": "false",
        },
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))

    # Range 2: docs after "h:~" (after all possible chunk IDs)
    resp = await httpx_client.get(
        "/_all_docs",
        params={
            "include_docs": "true",
            "startkey": '"h:~"',
        },
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))

    # Process with deduplication by base filename
    seen: dict[str, dict] = {}
    skipped_deleted = 0
    skipped_internal = 0
    skipped_ghost = 0
    skipped_duplicate = 0

    for row in all_rows:
        doc = row.get("doc")
        if not doc:
            continue

        # Skip deleted documents (check both doc._deleted and row.value.deleted)
        if doc.get("_deleted") or row.get("value", {}).get("deleted"):
            skipped_deleted += 1
            continue

        # Skip non-file docs (must have type and children)
        if doc.get("type") not in ("plain", "newnote") or "children" not in doc:
            continue

        # Skip ghost files (parent doc with zero chunks — orphaned entry)
        if not doc.get("children"):
            skipped_ghost += 1
            continue

        doc_id = doc.get("_id", "")

        # Skip LiveSync internal metadata and CouchDB system docs
        if any(doc_id.startswith(p) for p in _LIVESYNC_INTERNAL_PREFIXES):
            skipped_internal += 1
            continue

        # Deduplicate by base filename, keep highest mtime.
        # Uses filename (last path segment) as the dedup key so files
        # moved between folders (e.g. root → subfolder) collapse to one entry.
        path = doc.get("path", doc_id)
        filename = path.rsplit("/", 1)[-1]

        existing = seen.get(filename)
        if existing is None or doc.get("mtime", 0) > existing.get("mtime", 0):
            if existing is not None:
                skipped_duplicate += 1
                logging.debug(
                    "Dedup: keeping %s over %s (mtime %s > %s)",
                    path,
                    existing.get("path", existing.get("_id")),
                    doc.get("mtime"),
                    existing.get("mtime"),
                )
            seen[filename] = doc
        else:
            skipped_duplicate += 1

    if skipped_deleted or skipped_internal or skipped_ghost or skipped_duplicate:
        logging.info(
            "_get_all_file_docs: filtered %d deleted, %d internal, "
            "%d ghost, %d duplicate — %d unique file docs remain",
            skipped_deleted,
            skipped_internal,
            skipped_ghost,
            skipped_duplicate,
            len(seen),
        )

    return list(seen.values())


ObsidianVaultClient._get_all_file_docs = _patched_get_all_file_docs


# ── Startup: always discover salt, regardless of passphrase source ─

async def _startup():
    """Discover PBKDF2 salt, build and return the ASGI app."""
    global _pbkdf2_salt

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
    app = _header_middleware(app)

    print(
        f"Obsidian MCP server starting on {host}:{port}/mcp (StreamableHTTP + header auth)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
