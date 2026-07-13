# wren-mcp

MCP (Model Context Protocol) server exposing the [Wren](https://getwren.ai) semantic SQL engine to AI agents over **streamable-http**. Agents call Wren as a service instead of shelling out to the `wren` CLI.

`wren-mcp` serves **one project** (single-project mode, pinned at startup) or **many projects from one process** (multi-project mode, routed per-request by the `X-Wren-Project` header via a [wren-datasource](../wren-datasource) REST service).

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

## Run

### Single-project mode

One `wren-mcp` process serves one project, pinned at startup (backward compatible):

```bash
export WREN_MCP_TOKEN="<your-secret-token>"
wren-mcp --project /path/to/wren/project --token "$WREN_MCP_TOKEN"
```

### Multi-project mode

One `wren-mcp` process serves many projects. Connections are resolved per-request from a [wren-datasource](../wren-datasource) REST service by the `X-Wren-Project` header:

```bash
# 1. Start the datasource management REST service (once)
wren-datasource --token "$WREN_DATASOURCE_TOKEN" --port 8766
# Register projects + profiles via its REST API (see wren-datasource/README.md)

# 2. Start wren-mcp pointing at it
wren-mcp --datasource-url http://127.0.0.1:8766 \
         --datasource-token "$WREN_DATASOURCE_TOKEN" \
         --token "$WREN_MCP_TOKEN" \
         --default-project sales
```

Each project gets its own cached `ServerState` (connector + memory store + locks), LRU-evicted past `WREN_MCP_MAX_PROJECTS` (default 8). Memory is isolated per project via Qdrant collection prefixes (`wren_{project_id}`).

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--project` | (single-project: required) | Path to a Wren project (`wren_project.yml` + `target/mdl.json`) |
| `--profile` | project / active | Profile to use (single-project mode) |
| `--datasource-url` | (none) | wren-datasource REST URL - enables multi-project mode |
| `--datasource-token` | `$WREN_MCP_DATASOURCE_TOKEN` | Bearer token for the datasource service (multi-project) |
| `--default-project` | `$WREN_MCP_DEFAULT_PROJECT` | Project id when a request has no `X-Wren-Project` header (multi-project) |
| `--host` / `--port` | `127.0.0.1` / `8765` | Bind address |
| `--token` | `$WREN_MCP_TOKEN` | Bearer token clients must send (required) |
| `--read-only` | off | Gate write/mutation tools (`wren_store_query` dropped; `wren_context_build`, `wren_profile_add/remove/switch`, `wren_memory_index/load/forget/reset`, `wren_genbi_deploy` return a read-only error) |
| `--tools` | `all` | `tier1` (6 core tools) or `all` (full surface) |
| `--workers` | `1` | uvicorn worker count (>1 scales SQL concurrency past the GIL; each worker has its own registry/locks) |

The server listens on `http://<host>:<port>/mcp` (streamable-http, bearer auth).

## Agent configuration

### Single-project

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

### Multi-project

One `wren-mcp` process serving many projects - set `X-Wren-Project` per server entry:

```json
{
  "mcpServers": {
    "wren-sales": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <token>", "X-Wren-Project": "sales" }
    },
    "wren-ops": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <token>", "X-Wren-Project": "ops" }
    }
  }
}
```

### Cursor / other MCP clients

Point the client at the streamable-http URL with an `Authorization: Bearer <token>` header (and `X-Wren-Project` in multi-project mode).

## Tools

**Tier 1** (always on):
- `wren_query`, `wren_dry_plan`, `wren_dry_run`, `wren_list_models`
- `wren_fetch_context`, `wren_recall_queries`, `wren_store_query` - memory tools; auto-dropped when `QDRANT_URL` is unset. `wren_store_query` is additionally dropped in `--read-only`.

**Tier 2** (`--tools all`, default):
- `wren_context_show` / `wren_context_build` / `wren_context_validate` / `wren_context_instructions`
- `wren_cube_list` / `wren_cube_describe` / `wren_cube_query`
- `wren_profile_list` / `wren_profile_debug` (sensitive fields masked)
- `wren_profile_add` / `wren_profile_remove` / `wren_profile_switch` - edit `~/.wren/profiles.yml`; `--read-only` gated. In single-project mode switching does NOT re-route the running server - restart to serve a new profile. (Multi-project mode routes per-request, so this limitation does not apply.)
- `wren_memory_describe` / `wren_memory_status`
- `wren_memory_index` / `wren_memory_load` / `wren_memory_dump` / `wren_memory_forget` / `wren_memory_reset` - Qdrant index management; `reset` requires `force=true`; write tools `--read-only` gated (`dump` is read-only). In multi-project mode each project's index lives in its own Qdrant collection (`wren_{project_id}`).
- `wren_genbi_deploy` - verify + ship a registered app to Vercel/Cloudflare; irreversible (public URL); token read from env, never an argument; `--read-only` gated.
- `wren_parse_type` / `wren_translate_type`
- `wren_ask` / `wren_skills_list` / `wren_skills_get`
- `wren_docs_connection_info`

Side-effect tools (`profile add/rm/switch`, `memory index/load/forget/reset`, `genbi deploy`, `context build`) return a `read-only` error envelope when `--read-only` is set, instead of disappearing - so agents get actionable feedback. The long-running `memory watch` and `genbi open` (blocking servers) are not exposed over MCP - run them from the CLI.

## Output contract

Every tool returns an envelope:

```json
{"ok": true,  "content": "...", "data": {...}, "warnings": []}
{"ok": false, "content": "...", "error": {"code": "...", "phase": "...", "message": "...", "metadata": {...}}}
```

Recoverable SQL errors (parse / plan / execute) return `ok:false` so the agent can inspect `error.phase` and retry. Infrastructure failures (connection / config) raise to the MCP layer. This mirrors the wren-pydantic retry/propagate taxonomy.

## Concurrency

The Wren engine, connectors (psycopg3 etc.), and MemoryStore are blocking and **not** concurrency-safe. Each project's `ServerState` serializes its engine/connector calls with a per-project `engine_lock` and memory calls with a separate `memory_lock` - so a query on project A runs concurrently with a query on project B, and a memory call overlaps an engine call within a project. `--workers N` spawns N independent uvicorn processes (each its own registry/locks) to scale past the GIL.

## Architecture

`wren-mcp` consumes the shared provider trio (`wren.providers`) + `WrenEngine` directly - it does **not** depend on `wren-langchain` or `wren-pydantic`. DB/profile secrets are resolved server-side (`expand_profile_secrets`) and never returned to clients; `wren_profile_debug` masks `password`/`token`/`credential` fields.

**Multi-project routing**: `ServerContext` is a dispatcher tools capture; per-project attributes (`query`, `engine_lock`, `project_path`, ...) delegate to the current request's `ServerState` via a `current_state` contextvar set by `ProjectRoutingMiddleware` from the `X-Wren-Project` header. `SingleProjectRegistry` (one local state) backs single-project mode; `RestProjectRegistry` (LRU cache, fetches from wren-datasource) backs multi-project mode. The contextvar propagates across `anyio.to_thread.run_sync` worker threads so tool calls resolve the right project.
