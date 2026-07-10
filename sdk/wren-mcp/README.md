# wren-mcp

MCP (Model Context Protocol) server exposing the [Wren](https://getwren.ai) semantic SQL engine to AI agents over **streamable-http**. Agents call Wren as a service instead of shelling out to the `wren` CLI.

## Install

```bash
pip install wren-mcp
# with memory tools (Qdrant + Volcengine Ark embeddings):
pip install "wren-mcp[memory]"
```

## Prerequisites

A CLI-prepared Wren project and a connection profile:

```bash
wren context init          # scaffold wren_project.yml + models/
wren context build         # build target/mdl.json
wren profile add mydb --datasource duckdb --path ./data.duckdb
wren profile switch mydb
```

`wren-mcp` serves **one project at a time**, pinned at startup.

## Run

```bash
export WREN_MCP_TOKEN="<your-secret-token>"
wren-mcp --project /path/to/wren/project --token "$WREN_MCP_TOKEN"
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--project` | (required) | Path to a Wren project (has `wren_project.yml` + `target/mdl.json`) |
| `--profile` | project / active | Profile to use |
| `--host` / `--port` | `127.0.0.1` / `8765` | Bind address |
| `--token` | `$WREN_MCP_TOKEN` | Bearer token clients must send (required) |
| `--read-only` | off | Drop write/mutation tools (`wren_store_query`, `wren_context_build`, ...) |
| `--tools` | `all` | `tier1` (6 core tools) or `all` (full surface) |

The server listens on `http://<host>:<port>/mcp` (streamable-http, bearer auth).

## Agent configuration

### Claude Code (`mcp_settings.json`)

```json
{
  "mcpServers": {
    "wren": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <your-secret-token>" }
    }
  }
}
```

### Cursor / other MCP clients

Point the client at the streamable-http URL with an `Authorization: Bearer <token>` header.

## Tools

**Tier 1** (always on):
- `wren_query`, `wren_dry_plan`, `wren_dry_run`, `wren_list_models`
- `wren_fetch_context`, `wren_recall_queries`, `wren_store_query` — memory tools; auto-dropped when `QDRANT_URL` is unset. `wren_store_query` is additionally dropped in `--read-only`.

**Tier 2** (`--tools all`, default):
- `wren_context_show` / `wren_context_build` / `wren_context_validate` / `wren_context_instructions`
- `wren_cube_list` / `wren_cube_describe` / `wren_cube_query`
- `wren_profile_list` / `wren_profile_debug`
- `wren_memory_describe` / `wren_memory_status`
- `wren_parse_type` / `wren_translate_type`
- `wren_ask` / `wren_skills_list` / `wren_skills_get`
- `wren_docs_connection_info`

Long-running / destructive commands (`memory watch`/`reset`/`index`, `genbi open`/`deploy`, `profile add`/`rm`/`switch`) are **not** exposed over MCP in v1 — they change global `~/.wren` state or block; use the CLI.

## Output contract

Every tool returns an envelope:

```json
{"ok": true,  "content": "...", "data": {...}, "warnings": []}
{"ok": false, "content": "...", "error": {"code": "...", "phase": "...", "message": "...", "metadata": {...}}}
```

Recoverable SQL errors (parse / plan / execute) return `ok:false` so the agent can inspect `error.phase` and retry. Infrastructure failures (connection / config) raise to the MCP layer. This mirrors the wren-pydantic retry/propagate taxonomy.

## Concurrency

The Wren engine, connectors (psycopg3 etc.), and MemoryStore are blocking and **not** concurrency-safe. `wren-mcp` serializes all engine/connector/memory calls with a single `asyncio.Lock` — correctness over throughput. Remote agent call rates are low; for higher concurrency, run multiple server instances behind a balancer, each pinned to its own project.

## Architecture

`wren-mcp` consumes the shared provider trio (`wren.providers`) + `WrenEngine` directly — it does **not** depend on `wren-langchain` or `wren-pydantic`. DB/profile secrets are resolved server-side (`expand_profile_secrets`) and never returned to clients; `wren_profile_debug` masks `password`/`token`/`credential` fields.
