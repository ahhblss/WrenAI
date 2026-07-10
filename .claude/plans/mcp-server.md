# Plan: Wren MCP Service (streamable-http)

## 目标

把 wren 做成一个 **streamable-http MCP 服务**，让其他 AI agent 通过 MCP 协议远程调用（取代 CLI shell-out）。暴露**全部 CLI 能力**（除长运行/破坏性命令）。provider trio 抽到 `core/wren` 共享，避免三处 drift。独立 `sdk/wren-mcp` 包，不动 core/wren 的 CLI。

## 已确认的决策

| 维度 | 决策 |
|---|---|
| 传输 | **仅 streamable-http**（远程服务，复用 starlette/uvicorn） |
| 工具范围 | **全部**（~35 个，除长运行/破坏性） |
| provider trio | **抽到 `core/wren/src/wren/providers/` 共享**，两个 SDK 改 re-export |
| CLI 子命令 | **不做**，仅独立 `wren-mcp` console script |

## 关键事实（已验证）

- OSS 仓库 main 分支**无任何 MCP 代码**（`oss_vs_commercial.md` 把 MCP server 列为 Commercial-only）。
- `WrenToolkit.from_project(path, *, profile=None)`（`sdk/wren-langchain/_toolkit.py:156`）已封装 project/profile/.env/MDL/memory 全套接线，缓存 connector + memory_store，是 server 的引擎入口。
- 两个 SDK 的 `_providers/`（connection/mdl_source/memory）**逐字镜像**，仅异常 import 路径不同 → 抽共享零风险。
- envelope 依赖 `wren.model.error.WrenError`（core 已有），是纯函数层，wren-mcp 内对齐实现即可。
- core/wren 无 `mcp` 依赖；`ui` extra（starlette/uvicorn/jinja2）可作 http 底座。
- **并发危害**：`_build_engine` 每次新建 WrenEngine 但复用 `_connector_cache`，且 connector/psycopg 不线程安全。streamable-http 多请求并发下必须串行化。

## 分支

- 分支名：`feat/mcp-server`，从 `main` 切出。

---

## 阶段 1：抽 provider trio 到 core/wren（共享 refactor）

**新建 `core/wren/src/wren/providers/`**：
- `__init__.py` — 导出 `ProfileConnectionProvider`, `ProjectMDLSource`, `QdrantMemoryProvider`, `NoopMemoryProvider`, `WrenToolkitInitError`, `MemoryNotEnabledError`
- `exceptions.py` — `WrenToolkitInitError`, `MemoryNotEnabledError`（从 SDK 移入）
- `connection.py` — `ProfileConnectionProvider`（从 SDK 移入，异常改 import `wren.providers.exceptions`）
- `mdl_source.py` — `ProjectMDLSource`
- `memory.py` — `QdrantMemoryProvider`, `NoopMemoryProvider`

**改两个 SDK（向后兼容 re-export）**：
- `sdk/wren-langchain/src/wren_langchain/_providers/{connection,mdl_source,memory}.py` → 改成 `from wren.providers import ...`（保留模块作兼容 shim，或直接删并改 `_toolkit.py` 的 import）
- `sdk/wren-langchain/src/wren_langchain/exceptions.py` → `WrenToolkitInitError`/`MemoryNotEnabledError` 改为 `from wren.providers.exceptions import ...`（保持外部 import 路径不变）
- `sdk/wren-pydantic/` 同上

**验证**：现有 `test_providers_*.py`、`test_toolkit_init.py` 全绿（re-export 保兼容）；core/wren 加 `tests/unit/test_providers.py`。

---

## 阶段 2：wren-mcp 包骨架

**`sdk/wren-mcp/`**（镜像 sdk/ 布局）：

```
sdk/wren-mcp/
├── pyproject.toml          # hatchling; deps: wrenai>=0.12.0, mcp, starlette, uvicorn; optional [memory]
├── README.md
├── src/wren_mcp/
│   ├── __init__.py
│   ├── server.py           # main() 入口: argparse + uvicorn 启动
│   ├── _app.py             # build FastMCP app + 注册全部工具 + 生命周期
│   ├── _state.py           # ServerState: toolkit, asyncio.Lock, config
│   ├── _bridge.py          # sync->async: anyio.to_thread.run_sync + lock 串行化
│   ├── _envelope.py        # make_success/make_error/format_error (对齐 wren-langchain/_envelope.py)
│   ├── _format.py          # pa.Table -> content (对齐 wren-langchain/_format.py)
│   ├── _auth.py            # bearer token middleware (WREN_MCP_TOKEN)
│   └── _tools/
│       ├── __init__.py
│       ├── query.py        # wren_query, wren_dry_plan, wren_dry_run
│       ├── context.py      # show/build/validate/instructions/init/import/set-profile/upgrade
│       ├── cube.py         # list/describe/query
│       ├── profile.py      # list/debug/add(non-ui)/import/rm/switch
│       ├── memory.py       # describe/status/fetch/recall/store/index/export/list/dump/load (+ forget --id gated)
│       ├── docs.py         # connection_info
│       ├── types.py        # parse_type, translate_type
│       ├── ask.py          # ask
│       ├── skills.py       # list/get
│       └── genbi.py        # list/verify/build/register/remove
└── tests/
    ├── unit/               # 每工具 envelope/输入/输出
    ├── integration/        # duckdb 内存连接 + 真 MCP client 调用
    └── conformance/        # 参考 sdk conformance 契约
```

**`pyproject.toml`**：
- `name = "wren-mcp"`, `requires-python >= "3.11"`, hatchling
- `dependencies = ["wrenai>=0.12.0", "mcp>=1.2", "starlette>=0.37", "uvicorn>=0.29", "anyio>=4"]`
- `[project.optional-dependencies] memory = ["wrenai[memory]"]`
- `[project.scripts] wren-mcp = "wren_mcp.server:main"`

---

## 阶段 3：工具实现（全部 ~35 个）

每个工具：`@mcp.tool()` 装饰 + 类型注解输入 + 调 toolkit/core API（经 `_bridge`）+ 返回 envelope（作 structuredContent）。

**工具清单（分 Tier，实现顺序）**：

- **Tier 1 — 核心 6（先交付可用 server）**：`wren_query`, `wren_dry_plan`, `wren_list_models`, `wren_fetch_context`, `wren_recall_queries`, `wren_store_query`（直接复用 `WrenToolkit` 的 6 个方法）
- **Tier 2 — 扩展**：
  - query: `wren_dry_run`
  - context: `show`, `build`, `validate`, `instructions`, `init`, `import`, `set_profile`, `upgrade`（调 `wren.context`）
  - cube: `list`, `describe`, `query`（调 `wren_core.cube_query_to_sql` + engine）
  - profile: `list`, `debug`, `add`(non-ui), `import`, `rm`, `switch`（调 `wren.profile`）
  - memory: `describe`, `status`, `fetch`, `recall`, `store`, `index`, `export`, `list`, `dump`, `load`（调 `WrenMemory`/`MemoryStore`）
  - docs: `connection_info`（调 `field_registry`）
  - types: `parse_type`, `translate_type`（调 `type_mapping`）
  - ask: `ask`（调 `wren.ask`）
  - skills: `list`, `get`（调 `skills_delivery`）
  - genbi: `list`, `verify`, `build`, `register`, `remove`（调 `wren.genbi`）

**排除（长运行/破坏性）**：`memory watch`, `memory reset`(destructive), `memory forget`(interactive), `genbi open`, `genbi deploy`, `profile add --ui/--interactive`。

**副作用 gating**：写文件/改全局配置的工具（context build/init/import, memory store/index/load/dump, profile add/import/rm/switch, genbi build/register/remove）在 `--read-only` 模式下禁用，返回明确错误。默认开启但日志记录。

---

## 阶段 4：并发安全 + sync/async 桥接（核心风险点）

`_bridge.py`：
```python
async def run_blocked(fn, *args, **kw):
    async with state.engine_lock:          # 串行所有 toolkit/engine 调用
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kw))
```

- **一个全局 `asyncio.Lock`** 串行所有碰 toolkit/engine/connector/memory 的调用。正确性优先，性能后续优化（远程 agent 调用频率不高，可接受）。
- 只读 manifest 的纯函数工具（`list_models`, `context show`, `cube list/describe`, `memory describe/status`, `docs`, `types`, `ask`, `skills get/list`, `profile list/debug`）可不持锁，但第一版统一走 lock 简化。
- 所有 blocking 调用（engine.query/dry_run, connector, MemoryStore, VolcArkEmbedding）一律 `anyio.to_thread.run_sync`，绝不阻塞事件循环。

---

## 阶段 5：传输 + auth + 生命周期

`server.py` / `_app.py`：
- FastMCP streamable-http：`app = mcp.streamable_http_app()` 取 ASGI app（实现时验证 mcp SDK 版本 API；备选 `mcp.run(transport="streamable-http")`）。
- **Auth**：bearer token middleware。`WREN_MCP_TOKEN` 必填，未设则启动失败（远程服务强制 auth）。校验 `Authorization: Bearer <token>`，不符返回 401。
- **生命周期**：启动 → `WrenToolkit.from_project(project, profile=profile)`；关闭 → `toolkit` 持有的 connector `close()` + memory_store `close()`（uvicorn lifespan）。
- **启动参数**：`--project`（必填，Wren 项目路径）、`--profile`、`--host`（默认 127.0.0.1）、`--port`（默认 8765）、`--read-only`、`--tools tier1|all`（默认 all）、`--token`（或 env `WREN_MCP_TOKEN`）。
- DB/profile secrets 一律 server 端 `expand_profile_secrets` 解析，绝不返回客户端（`debug` 工具 mask）。

---

## 阶段 6：测试 + 文档 + CI

- **单测**：每工具的输入校验、envelope 形状、error phase 映射（duckdb 内存连接）。
- **集成**：起 server + mcp client，调 Tier 1 全部工具 + 一个 cube query + 一个 context build，断言 envelope。
- **conformance**：参考 `sdk/wren-langchain/tests/conformance/`，wren-mcp 工具契约对齐。
- **README**：安装（`pip install wren-mcp`/`wren-mcp[memory]`）、首次配置（project/profile/mdl.json）、启动、agent 接入示例（Claude Code / Cursor 的 mcp config JSON，指向 streamable-http URL + token）。
- **docs**：`docs/core/get_started/quickstart-with-agent/` 加 `wren-mcp.md`。
- **CI**：`.github/workflows/sdk-mcp-ci.yml`，path-filtered on `sdk/wren-mcp/**` + `core/wren/src/wren/providers/**`，跑 ruff + pytest。

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 并发损坏 connector | 全局 `asyncio.Lock` 串行（阶段 4） |
| mcp SDK API 与假设不符 | 阶段 5 先写最小 streamable-http 验证，再铺工具 |
| 副作用工具在远程多租户误触 | `--read-only` gating + 日志 + token auth |
| memory extra 缺失时 `fetch_context` 不可用 | QDRANT_URL 未设则 drop memory 工具并返回清晰安装提示（复用 SDK auto-filter） |
| RLAC/CLAC 无 Python API | 不做"describe access policy"工具；引擎自动强制，安全不受影响 |
| provider trio refactor 破坏 SDK | re-export 保兼容 + 现有 `test_providers_*` 守护 |
| payload 超限 | 延续 MAX_QUERY_ROWS=1000、content 16KB、metadata 4KB cap |

## 交付顺序（建议分 PR）

1. PR1：阶段 1（provider trio 抽共享）— 纯 refactor，独立可审，现有测试守护。
2. PR2：阶段 2+3 Tier 1 + 阶段 4+5（最小可用 streamable-http server，6 核心工具）— 端到端跑通。
3. PR3：阶段 3 Tier 2（其余 ~29 工具）+ 副作用 gating。
4. PR4：阶段 6（测试补全 + 文档 + CI）。
