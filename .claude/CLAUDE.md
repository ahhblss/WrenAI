# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Wren is an open-source semantic engine for MCP clients and AI agents. It translates SQL queries through a semantic layer (MDL — Modeling Definition Language) and executes them against 22+ data sources (PostgreSQL, BigQuery, Snowflake, Spark, etc.). The Rust engine is powered by Apache DataFusion (Canner fork).

The previous WrenAI services (`wren-ui/`, `wren-ai-service/`, `wren-launcher/`, `docker/`, `deployment/`) were moved to the `legacy/v1` branch (tag `v1-final`) as of the wren-engine import. Active development is focused on the Open Context Engine.

## Repository Structure

```
core/
├── wren-core/        Rust semantic engine (Cargo workspace)
├── wren-core-base/   Shared Rust crate — manifest types (Model, Column, Metric, Relationship, View) + ManifestBuilder
├── wren-core-py/     PyO3 bindings exposing wren-core to Python (PyPI: wren-core)
├── wren-core-wasm/   WebAssembly build of wren-core for in-browser semantic SQL (npm: wren-core-wasm)
├── wren/             Python SDK and CLI — `wren` command, profile/context/memory management (PyPI: wrenai)
└── wren-mdl/         MDL JSON schema definition

sdk/
├── wren-mcp/          MCP server (streamable-http) exposing a Wren project to AI agents (PyPI: wren-mcp)
├── wren-langchain/    LangChain WrenToolkit adapter
└── wren-pydantic/     Pydantic AI WrenToolkit adapter

docs/core/            Module documentation
examples/             Example projects (placeholder — to be populated)
skills/               CLI-based agent skills (wren-generate-mdl, wren-usage, wren-dlt-connector, wren-onboarding)
scripts/              Repo helper scripts
```

## Build & Development Commands

### core/wren-core (Rust)
```bash
cd core/wren-core
cargo check --all-targets                                  # compile check
cargo test --lib --tests --bins                            # tests (set RUST_MIN_STACK=8388608)
cargo fmt --all                                            # format
cargo clippy --all-targets --all-features -- -D warnings   # lint
taplo fmt                                                  # format Cargo.toml files
```

Most unit tests live in `core/wren-core/core/src/mdl/mod.rs`. SQL end-to-end tests use sqllogictest files in `core/wren-core/sqllogictest/test_files/`.

### core/wren-core-py (Python bindings)
```bash
cd core/wren-core-py
just install      # uv sync (deps only; --no-install-project)
just develop      # build dev wheel with maturin
just test-rs      # Rust tests (cargo test --no-default-features)
just test-py      # Python tests (pytest)
just test         # both
just format       # cargo fmt + ruff + taplo
```

### core/wren-core-wasm (WASM)
```bash
cd core/wren-core-wasm
just build        # wasm-pack build (browser target)
just test         # wasm-pack test
```
Outputs a ~68 MB WASM binary; distributed via npm and unpkg (jsDelivr's 50 MB per-file CDN limit blocks it).

### core/wren (SDK & CLI)
```bash
cd core/wren
just install              # uv sync (locked prebuilt wren-core-py wheel from PyPI; no Rust build)
just install-all          # with all optional extras (incl. memory)
just install-extra <e>    # e.g. just install-extra postgres
just install-memory       # memory extra (qdrant + Volcengine Ark embeddings)
just install-local        # engine dev: uv sync + build local wheel + overlay
just use-local-core       # rebuild + re-overlay after Rust changes
just dev                  # run `wren` CLI
just test                 # pytest tests/
just test-memory          # memory-specific tests
just lint                 # ruff format --check + ruff check
just format               # ruff auto-fix
just build                # uv build (produces wheel)
```

Uses `uv` (not Poetry). `pyproject.toml` uses `hatchling` as build backend. Optional extras: `postgres`, `mysql`, `bigquery`, `snowflake`, `clickhouse`, `trino`, `mssql`, `databricks`, `redshift`, `spark`, `athena`, `oracle`, `memory`, `all`, `dev`.

### sdk/wren-mcp (MCP server)
```bash
cd sdk/wren-mcp
just install      # uv sync (deps: wrenai + mcp + starlette + uvicorn + anyio)
just test         # pytest
# Tests reuse core/wren's venv (wrenai + mcp SDK installed) + PYTHONPATH=src:
PYTHONPATH=src ../../core/wren/.venv/Scripts/python.exe -m pytest tests/
```

## Architecture: Query Flow

```
SQL query
  → wren CLI / wren-core-py
  → wren-core (Rust): MDL analysis → logical plan → optimization
  → DataFusion (query planning, Canner fork canner/v49.0.1)
  → connector-specific SQL (Ibis / sqlglot)
  → native execution on the target data source
```

## Key Architecture Details

**wren-core internals** (`core/wren-core/core/src/`):
- `mdl/` — Core MDL processing: `WrenMDL` (manifest + symbol table), `AnalyzedWrenMDL` (with lineage), function definitions per dialect (scalar/aggregate/window), type planning
- `logical_plan/analyze/` — DataFusion analyzer rules: `ModelAnalyzeRule` (TableScan → ModelPlanNode), scope tracking, access control (RLAC/CLAC), view expansion, relationship chain resolution
- `logical_plan/optimize/` — Optimization passes: type coercion, timestamp simplification
- `sql/` — SQL parsing and analysis

**Manifest types** (`core/wren-core-base/src/mdl/`):
- `manifest.rs` — `Manifest`, `Model`, `Column`, `Metric`, `Relationship`, `View`, `RowLevelAccessControl`, `ColumnLevelAccessControl`
- `builder.rs` — Fluent `ManifestBuilder` API
- Uses `wren-manifest-macro` for auto-generating Pydantic-compatible Python classes

## wren-mcp (MCP server)

`sdk/wren-mcp/` exposes a Wren project as a streamable-http MCP service for AI agents. It consumes the shared provider trio (`wren.providers`) + `WrenEngine` directly - it does **not** depend on `wren-langchain` or `wren-pydantic`.

- **Single-project pinning**: one server = one project + profile resolved at startup. `wren_profile_switch` edits `~/.wren` but does NOT re-route the running server - restart to serve a new profile.
- **Tool tiers**: Tier 1 (`wren_query`/`dry_plan`/`dry_run`/`list_models` + memory) always on; Tier 2 (`--tools all`, default) adds context/cube/profile/memory-mutate/genbi/types/ask/skills/docs. Memory tools auto-drop when `QDRANT_URL` is unset.
- **read-only guard**: side-effect tools (`profile add/rm/switch`, `memory index/load/forget/reset`, `genbi deploy`, `context build`) return a `read-only` error envelope under `--read-only` instead of disappearing. `wren_memory_reset` additionally requires `force=true`.
- **Concurrency**: two locks. `engine_lock` serializes engine/connector calls (single cached DB connection, not thread-safe); `memory_lock` serializes memory (Qdrant/Ark) calls separately - the two share no state, so an embedding call runs concurrently with a SQL query. `run_blocked`/`run_memory_blocked` offload to a worker thread under the respective lock, bounded by `state.tool_timeout` (env `WREN_MCP_TOOL_TIMEOUT`, default 120s): a hung call raises `TimeoutError` and releases the lock (worker thread can't be force-killed). Requires `abandon_on_cancel=True` on `anyio.to_thread.run_sync` - anyio shields the worker future by default, which would make the timeout wait for the thread.
- **Multi-worker** (`--workers N`): scales SQL concurrency past the GIL + engine_lock by spawning N independent uvicorn processes (each its own engine/connector/locks). Config is passed to workers via `WREN_MCP_CFG_*` env vars (`app_factory` rebuilds each). `workers>1` forces `stateless_http=True` on FastMCP - streamable-http session state is in-process, so a stateful session would break across workers (initialize on worker A, call_tool on worker B hangs); stateless treats every request independently. Costs N x memory + N x DB/Qdrant connections. wren-core `SessionContext` sharing is GIL-safe (`transform_sql` holds the GIL; verified 320/320 concurrent calls correct), so per-worker engine concurrency is bounded only by the single cached connector connection.
- **Timeout protection**: the embedding and Qdrant clients have no useful SDK default timeout (OpenAI SDK = 600s read + 2 retries; qdrant-client = none). When Ark/Qdrant is unreachable, a single `embed_texts` or Qdrant call would hold `engine_lock` for minutes and stall every tool - the original "over-wire stall" was this, NOT an MCP SDK transport bug (the SSE layer's 15s ping works fine). Bounded by `WREN_EMBEDDING_TIMEOUT` (30s) + `WREN_EMBEDDING_MAX_RETRIES` (1) on the OpenAI client, `WREN_QDRANT_TIMEOUT` (30s) on QdrantClient, and `WREN_MCP_TOOL_TIMEOUT` (120s) as the `run_blocked` backstop.
- **Memory/embedding**: memory tools need `QDRANT_URL` + `VOLC_ARK_API_KEY`. `doubao-embedding-vision` (as configured in the wren-test project) is a text embedding model (dim 2048) via `VOLC_ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3` - despite the name it works for text.
- **Testing**: over-wire tests use `streamablehttp_client` (the deprecated name; the new `streamable_http_client` rejects the `headers` kwarg). In-process tests use `build_mcp(ServerConfig(...))` + `await mcp.call_tool(name, args)` -> `(content, structuredEnvelope)`.
- **cube_query** blocks in the Rust engine on a missing cube - test with a real cube or skip it.

## Known wren-core Limitations

**ModelAnalyzeRule — correlated subquery column resolution**: cannot resolve outer column references inside correlated subqueries; only sees the subquery's own table scope. Affects TPCH Q2, Q4, Q15, Q17, Q20, Q21, Q22.

## Conventions

- **Commits**: Conventional commits (`feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`, `perf:`, `deps:`). Releases are automated via release-please with independent release lines per module.
- **Rust**: format with `cargo fmt`, lint with `clippy -D warnings`, TOML with `taplo`.
- **Python**: format and lint with `ruff` (line-length 88, target Python 3.11). Both `core/wren-core-py` and `core/wren` use uv.
- **DataFusion fork**: `https://github.com/Canner/datafusion.git` branch `canner/v49.0.1`.
- **Snapshot testing**: wren-core uses `insta` for Rust snapshot tests.
- **CI**: Per-module path-filtered workflows trigger only on changes inside that module.
