# Obsidian LiveSync + MCP Server (CouchDB)

**Single Docker container** running **CouchDB for Obsidian LiveSync** and an **MCP SSE server** for AI agent vault access — with full encryption support.

Agents (Claude Desktop, Hermes, Cursor, etc.) connect to `http://<host>:8000/sse` and get full read/write/search access to your vault. Encrypted vaults work via header-based auto-unlock — no passphrase ever reaches the agent.

## How it works

```
┌──────────────────────────────────────────────┐
│                 Docker Container              │
│                                              │
│  ┌──────────┐         ┌───────────────────┐  │
│  │ CouchDB  │◄───────│  MCP SSE Server   │  │
│  │  :5984   │ local   │  :8000/sse        │  │
│  │          │  HTTP   │                   │  │
│  └────┬─────┘         └────────┬──────────┘  │
│       │                        │              │
└───────┼────────────────────────┼──────────────┘
        │                        │
   Obsidian clients          AI agents
   (LiveSync plugin)    (Claude, Hermes, etc.)
```

- **CouchDB** (port 5984) stores your vault, synced by the [Obsidian LiveSync](https://github.com/vrtmrz/obsidian-livesync) plugin
- **MCP SSE server** (port 8000) talks to CouchDB locally and exposes 14 tools over the [Model Context Protocol](https://modelcontextprotocol.io/)
- Both run under **supervisord** as a single container
- **End-to-end encryption** fully supported — auto-unlock via HTTP header, passphrase never touches the LLM

## Quick start

```bash
git clone https://github.com/bestonephoenix/docker-obsidian-livesync-couchdb-mcp.git
cd docker-obsidian-livesync-couchdb-mcp

# Configure
cp .env.example .env
# Edit .env — set COUCHDB_PASSWORD at minimum

# Build and run
docker compose up -d --build
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SERVER_DOMAIN` | No | `localhost` | Domain for CouchDB setup URI |
| `COUCHDB_USER` | No | `admin` | CouchDB admin username |
| `COUCHDB_PASSWORD` | **Yes** | — | CouchDB admin password |
| `COUCHDB_DATABASE` | No | `obsidian` | Database name for the vault |
| `COUCHDB_PORT` | No | `5984` | Host port for CouchDB |
| `MCP_PORT` | No | `8000` | Host port for MCP SSE endpoint |
| `COUCHDB_DATA` | No | `./couchdb-data` | Persistent volume path |

## Encryption support

### No encryption? Nothing to configure.

If you don't use LiveSync's end-to-end encryption, the server works out of the box. Chunks without the `e_` flag are returned as-is — no decryption layer is involved.

### Using encryption? Set one header.

Add the passphrase to your MCP client config as an HTTP header. The server auto-discovers the PBKDF2 salt from CouchDB and decrypts transparently.

**Hermes Agent** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  obsidian:
    url: "http://your-vps:8000/sse"
    headers:
      X-Livesync-Passphrase: "${LIVESYNC_PASSPHRASE}"
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-client-sse"],
      "env": {
        "MCP_SERVER_URL": "http://your-vps:8000/sse",
        "MCP_HEADER_X_Livesync_Passphrase": "your-passphrase"
      }
    }
  }
}
```

### How it works

1. MCP client sends `X-Livesync-Passphrase` header with every request
2. `LiveSyncAuthMiddleware` captures it on the first request
3. PBKDF2 salt is auto-discovered from CouchDB `_local/` documents
4. All encrypted chunks (`e_: true`) are decrypted via PBKDF2 → HKDF → AES-256-GCM
5. The passphrase stays in your client config — the agent never sees it

If the header approach isn't supported by your client, the `unlock_vault(passphrase)` tool is available as a fallback (agent calls it once to unlock).

### What you need

Only your **encryption passphrase**. The PBKDF2 salt (a 64-character hex string generated when you first enabled encryption) is auto-discovered from your CouchDB instance. If auto-discovery fails, provide it explicitly:

```
X-Livesync-PBKDF2-Salt: "your-64-char-hex-salt"
```

Or via the fallback tool: `unlock_vault("passphrase", "64charhexsalt")`

## Available MCP tools

All tools from [obsidian-self-mcp](https://github.com/suhasvemuri/obsidian-self-mcp) plus one extra:

| Tool | Description |
|---|---|
| `unlock_vault` | **NEW** — unlock encrypted vault (fallback if headers not supported) |
| `list_notes` | List notes with metadata, filter by folder |
| `read_note` | Read full content of a note |
| `write_note` | Create or update a note |
| `search_notes` | Full-text search across vault |
| `append_note` | Append content to an existing note |
| `delete_note` | Delete a note and its chunks |
| `list_folders` | List folders with note counts |
| `read_frontmatter` | Read YAML frontmatter properties |
| `update_frontmatter` | Set/update frontmatter (JSON input) |
| `list_tags` | List all tags with occurrence counts |
| `search_by_tag` | Find notes containing a tag |
| `get_backlinks` | Find notes linking to a given note |
| `get_outbound_links` | List wikilinks from a note |

## What this builds on

This project layers an MCP server on top of the CouchDB configuration from [oleduc/docker-obsidian-livesync-couchdb](https://github.com/oleduc/docker-obsidian-livesync-couchdb), which automates CouchDB setup for Obsidian LiveSync.

MCP tools are provided by [suhasvemuri/obsidian-self-mcp](https://github.com/suhasvemuri/obsidian-self-mcp), which handles LiveSync's document/chunk format.

Encryption decryption implements the HKDF-based AES-256-GCM scheme from [vrtmrz/octagonal-wheels](https://github.com/vrtmrz/octagonal-wheels) (the LiveSync encryption library), reimplemented in Python using `cryptography`.

## Architecture notes

- **ARM64** — builds for `linux/arm64` by default. For amd64, add `platform: linux/amd64` to docker-compose.
- CouchDB data persists via Docker volume at `/opt/couchdb/data`.
- The MCP server connects to CouchDB internally over `localhost:5984`.
- Uses **supervisord** as PID 1 to manage both CouchDB and the MCP SSE server.
- Transport security (DNS rebinding protection) is disabled — safe inside Docker's network namespace.

## Testing

The original CouchDB configuration tests are preserved:

```bash
./run-compatibility-test.sh
```

## License

MIT — same as the upstream projects.

## Credits

- [vrtmrz/obsidian-livesync](https://github.com/vrtmrz/obsidian-livesync) — the sync engine
- [vrtmrz/octagonal-wheels](https://github.com/vrtmrz/octagonal-wheels) — encryption library (reference implementation)
- [oleduc/docker-obsidian-livesync-couchdb](https://github.com/oleduc/docker-obsidian-livesync-couchdb) — the CouchDB container this builds on
- [suhasvemuri/obsidian-self-mcp](https://github.com/suhasvemuri/obsidian-self-mcp) — MCP server and CLI
- [Apache CouchDB](https://couchdb.apache.org/) — the database
- [Obsidian](https://obsidian.md/) — the note-taking app
