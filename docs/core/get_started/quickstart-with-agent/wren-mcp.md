---
sidebar_label: MCP Server (wren-mcp)
---

# Wren AI MCP Server

`wren-mcp` exposes the Wren semantic SQL engine as a **streamable-http MCP service**. AI agents (Claude Code, Cursor, Codex, ...) call Wren as a service over the MCP protocol instead of shelling out to the `wren` CLI.

## Prerequisites

- Python 3.11+
- A CLI-prepared Wren project (`wren context init` + `wren context build`)
- A connection profile (`wren profile add` + `wren profile switch`)

## Install

```bash
pip install wren-mcp
# with memory tools (Qdrant + Volcengine Ark embeddings):
pip install "wren-mcp[memory]"
```

## Run

```bash
export WREN_MCP_TOKEN="your-secret-token"
wren-mcp --project /path/to/wren/project --token "$WREN_MCP_TOKEN"
```

The server listens on `http://127.0.0.1:8765/mcp` (streamable-http, bearer auth).

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--project` | (required) | Path to a Wren project |
| `--profile` | project / active | Profile to use |
| `--host` / `--port` | `127.0.0.1` / `8765` | Bind address |
| `--token` | `$WREN_MCP_TOKEN` | Bearer token clients must send (required) |
| `--read-only` | off | Drop write/mutation tools |
| `--tools` | `all` | `tier1` (6 core) or `all` (full surface) |

## Agent configuration

### Claude Code (`mcp_settings.json`)

```json
{
  "mcpServers": {
    "wren": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer your-secret-token" }
    }
  }
}
```

### Cursor / other MCP clients

Point the client at the streamable-http URL with an `Authorization: Bearer <token>` header. Requests without a valid token get `401 Unauthorized`.

## Tools

**Tier 1** (always on):

- `wren_query` / `wren_dry_plan` / `wren_dry_run` / `wren_list_models`
- `wren_fetch_context` / `wren_recall_queries` / `wren_store_query` — memory tools; auto-dropped when `QDRANT_URL` is unset. `wren_store_query` is dropped in `--read-only`.

**Tier 2** (`--tools all`, default):

- context: `wren_context_show` / `wren_context_build` / `wren_context_validate` / `wren_context_instructions`
- cube: `wren_cube_list` / `wren_cube_describe` / `wren_cube_query`
- profile: `wren_profile_list` / `wren_profile_debug` (sensitive fields masked)
- memory: `wren_memory_describe` / `wren_memory_status`
- types: `wren_parse_type` / `wren_translate_type`
- ask/skills: `wren_ask` / `wren_skills_list` / `wren_skills_get`
- docs: `wren_docs_connection_info`

Long-running / destructive commands (`memory watch/reset/index`, `genbi open/deploy`, `profile add/rm/switch`) are **not** exposed over MCP — they change global `~/.wren` state or block. Use the CLI.

### Typical agent workflow

1. `wren_list_models` → discover queryable models
2. `wren_fetch_context` (if memory on) → pull relevant schema/rules
3. `wren_dry_plan` → verify SQL targets the right models
4. `wren_query` → execute and fetch rows
5. `wren_store_query` → persist the NL→SQL pair for future recall

## Output contract

Every tool returns an envelope:

```json
{"ok": true,  "content": "...", "data": {...}, "warnings": []}
{"ok": false, "content": "...", "error": {"code": "...", "phase": "...", "message": "...", "metadata": {}}}
```

Recoverable SQL errors (parse / plan / execute) return `ok:false` so the agent can inspect `error.phase` and retry. Infrastructure failures (connection / config) raise to the MCP layer. `wren_query` caps rows at 1000 and truncates content at 16 KB.

## Memory (semantic recall)

Memory tools require Qdrant + Volcengine Ark embeddings:

```bash
pip install "wren-mcp[memory]"
docker run -p 6333:6333 qdrant/qdrant
export QDRANT_URL=http://localhost:6333
export VOLC_ARK_API_KEY=<your-ark-key>
wren memory index            # build the index (CLI, one-time)
wren-mcp --project ... --token ...
```

Without `QDRANT_URL`, `wren_memory_describe` (pure schema text) still works; the other memory tools are auto-dropped.

## Concurrency

The Wren engine, connectors (psycopg3 etc.), and MemoryStore are blocking and not concurrency-safe. `wren-mcp` serializes all engine/connector/memory calls with a single `asyncio.Lock` — correctness over throughput. For higher concurrency, run multiple server instances behind a balancer, each pinned to its own project.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `wren_project.yml not found` | Wrong `--project` path, or run `wren context init` |
| `target/mdl.json not found` | Run `wren context build` |
| `no active Wren profile` | Run `wren profile add` + `wren profile switch`, or pass `--profile` |
| `requires an auth token` | Set `WREN_MCP_TOKEN` or pass `--token` |
| Client gets `401` | Token mismatch — check the `Authorization: Bearer <token>` header |
| Memory tools missing | `QDRANT_URL` unset; install `wren-mcp[memory]` and run `wren memory index` |
| `wren_query` connection error | Profile connection info wrong — inspect with `wren_profile_debug` |

## Next step

- [Quickstart with sample data](../quickstart) - walk through `jaffle_shop` end-to-end
- [Connect your data](/oss/guides/connect) - point Wren AI at a real database
- [wren-mcp README](https://github.com/ahhblss/WrenAI/blob/main/sdk/wren-mcp/README.md) - full package README
