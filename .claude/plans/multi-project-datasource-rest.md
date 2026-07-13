# 多项目支持:数据源管理 REST 服务 + wren-mcp 改造

## 目标

把"数据源管理"层抽成独立 REST 服务(`sdk/wren-datasource/`),wren-mcp 不再启动时 pin 单一 project,改为按请求 `X-Wren-Project` header 路由到对应 project 的 per-project 状态。**一个 wren-mcp 实例服务多 project。**

## 决策(已与用户确认)

- **路由方式**:Header `X-Wren-Project` + `contextvar`(对 LLM 透明,不改工具签名,兼容 stateless 多 worker)
- **REST 范围**:project + profile + connection 全管理(wren-mcp 完全无状态,所有信息从 REST 取)

## 现状根因

- `ServerState` 启动时固化 `project_path` / `connection` / `_connector_cache` / `_memory_store_cache` / `engine_lock` / `memory_lock`(`_state.py:78-110`)
- 工具闭包在注册时捕获**单一** `state`(`_tools/__init__.py:44`)
- `WrenEngine` 本身**不绑 project**(每次 `build_engine()` read-through 重读 manifest,`_state.py:114`),可按 project 重建 ✅
- `MemoryStore` 已支持 `collection_prefix`(`store.py:77-83`),多 project memory 隔离**无需改 store** ✅
- `_auth.py` 的 `BaseHTTPMiddleware` 模式可直接复用做路由中间件 ✅

---

## Part 1:新模块 `sdk/wren-datasource/`(数据源管理 REST 服务)

### 职责
接管 project 注册 + profile CRUD + connection 解析 + manifest 代理。取代 `~/.wren/profiles.yml` + `ProfileConnectionProvider` + `ProjectMDLSource` 在 wren-mcp 里的角色。

### 技术栈
FastAPI + uvicorn(同 wren-mcp 栈);SQLite(`~/.wren/datasource.db`)存 projects + profiles;Bearer token 认证(复用 `_auth.py` 模式)。

### API
```
POST   /projects                   {name, project_path, profile_name?} -> {project_id, ...}
GET    /projects                   -> [{project_id, name, project_path, profile_name, datasource}]
GET    /projects/{id}              -> 详情
DELETE /projects/{id}

GET    /profiles                   -> [{name, datasource, ...}]   (redacted)
POST   /profiles                   {name, datasource, ...fields} -> {name}
DELETE /profiles/{name}
POST   /profiles/{name}/activate

GET    /projects/{id}/connection   -> {datasource, connection_info, memory_collection_prefix}
                                     (expand_profile_secrets + DataSource.get_connection_info)
GET    /projects/{id}/manifest     -> <manifest json>   (read-through ProjectMDLSource)
POST   /projects/{id}/validate     -> {ok, error}        (SELECT 1 through connector)
```

### 复用(不重写)
- `wren.profile.expand_profile_secrets`
- `wren.model.DataSource.get_connection_info`(类型化 + timeout 注入,`data_source.py:62`)
- `wren.providers.ProjectMDLSource`(manifest 代理)
- `wren.profile_cli._validate_connection` 的 SELECT 1 逻辑

### 存储
SQLite 两表:`projects(id, name, project_path, profile_name, created_at)`、`profiles(name, datasource, config_json)`。启动时可选 `--import-profiles` 从 `~/.wren/profiles.yml` 导入。

### 认证
`WREN_DATASOURCE_TOKEN` / `--token`,Bearer middleware。

### 代码结构
```
sdk/wren-datasource/
├── pyproject.toml          (PyPI: wren-datasource; deps: wrenai + fastapi + uvicorn)
├── README.md
├── src/wren_datasource/
│   ├── __init__.py
│   ├── server.py           (CLI entrypoint + uvicorn)
│   ├── _app.py             (FastAPI app + middleware)
│   ├── _store.py           (SQLite: projects + profiles CRUD)
│   ├── _resolve.py         (profile -> datasource + connection_info + collection_prefix)
│   ├── _auth.py            (Bearer middleware)
│   └── api/{projects,profiles,connection}.py
└── tests/
```

---

## Part 2:wren-mcp 多项目改造

### 新增

**`ProjectState`**(从 `ServerState` 拆出 per-project 部分):
- 字段:`project_id`, `project_path`, `mdl_source`, `datasource`, `connection_info`, `memory_collection_prefix`, `_connector_cache`, `_memory_store_cache`, `engine_lock`, `memory_lock`
- 方法:`build_engine` / `query` / `dry_plan` / `dry_run` / `load_manifest` / `memory_store`(同现 `ServerState`)
- connection_info 从 REST 懒加载(带 TTL 缓存,避免每请求 HTTP)

**`ProjectRegistry`**(进程内 LRU 池):
- `OrderedDict[project_id, ProjectState]`,上限 `WREN_MCP_MAX_PROJECTS`(默认 8)
- `async get(project_id)`:命中 move-to-end;未命中从 REST 拉连接信息建 state;超限 evict 最旧(`close()` connector + memory)
- `close_all()`:shutdown 用

**`current_project_id: ContextVar[str | None]`**(`_routing.py`):请求级路由上下文。

**`ProjectRoutingMiddleware`**(`_routing.py`,照 `_auth.py` 模式):
- `dispatch` 里读 `X-Wren-Project` header(缺失 fallback 到 `config.default_project`),设进 `current_project_id`
- 无 header 且无 default -> 422 `{"error": "missing X-Wren-Project header"}`

### 改造

**`ServerState` -> 拆分**:
- per-project 部分 -> `ProjectState`
- 进程级共享部分(`rest_client`, `registry`, `config`, `tool_timeout`)-> `ServerContext`

**`_tools/__init__.py register_all`**:签名 `(mcp, state)` -> `(mcp, ctx: ServerContext)`。每个工具闭包改为:
```python
async def wren_query(sql, limit=100):
    pid = current_project_id.get()
    try:
        state = await ctx.registry.get(pid)   # per-project state
    except ProjectNotFoundError:
        return make_error(...)
    table = await run_blocked(state, state.query, sql, limit)
    ...
```

**`_bridge.run_blocked`**:不变。`state` 现在是 per-project 的,`engine_lock`/`memory_lock` 也 per-project。contextvar 在 middleware 顶层设置,worker thread 经 `copy_context` 继承 ✅。

**`_app.py build_mcp`**:
- 不再 `ServerState.from_config`(不 pin project)
- 建 `ServerContext`(`rest_client` + `registry` + `config`)
- `register_all(mcp, ctx)`
- `stateless_http=config.workers > 1`(保持)
- middleware 链:`BearerTokenMiddleware` -> `ProjectRoutingMiddleware`

**`server.py`**:
- `--project` 改为**可选**(注册为 default project,header 缺失时 fallback;单项目用户无感)
- 新增 `--datasource-url`(REST 服务地址,必需)
- 新增 `--datasource-token`(REST 服务 token)
- banner 改为 `serving via <datasource-url> (default_project=..., max_projects=...)`
- 多 worker:每 worker 独立 `ProjectRegistry`,共享 REST 服务

### 向后兼容
- `--project` 仍可用:启动时把它注册为 default project。单项目用户配 MCP client 不加 `X-Wren-Project` header 即可,行为不变
- 未配 `--datasource-url`:报错引导(保持单一代码路径,不维护双模式)

---

## Part 3:Memory 隔离

- `ProjectState` 用 `memory_collection_prefix = f"wren_{project_id}"` 建 `MemoryStore`
- `MemoryStore(collection_prefix=...)` 已支持(`store.py:77`),**无需改 store**
- `wren_memory_index` / `wren_memory_load` / `wren_memory_forget` 等工具自动按 `current_project_id` 路由到对应 collection

---

## Part 4:多 worker 一致性

- 每 worker 独立 `ProjectRegistry`(进程内 LRU)
- 共享 REST 服务(单一数据源真相)
- `stateless_http=True` 保持(多 worker 必须)
- connector / memory cache 每 worker 独立(已是现状)

---

## Part 5:文档 + client 配置

- `sdk/wren-datasource/README.md`:部署拓扑(1 wren-datasource + 1 wren-mcp,可多 worker)
- `sdk/wren-mcp/README.md`:多项目配置章节
- MCP client 多项目配置示例:
```json
{
  "wren-sales": {"url": "http://host:8765/mcp",
                 "headers": {"Authorization": "Bearer X", "X-Wren-Project": "sales"}},
  "wren-ops":   {"url": "http://host:8765/mcp",
                 "headers": {"Authorization": "Bearer X", "X-Wren-Project": "ops"}}
}
```
- `.github/workflows/sdk-datasource-ci.yml`:新增 CI

---

## 风险与对策

| 风险 | 对策 |
|------|------|
| contextvar 跨 `anyio.to_thread.run_sync` worker thread | 默认 `copy_context` 传播 ✅;加测试验证 |
| `list_tools` 全局(无 project 概念) | 多 project 共享工具名,底层路由不同;工具集(read-only/tier)由 wren-mcp config 全局决定,不按 project 变;LLM 看到 `wren_query` 不变 |
| REST 调用频率 | registry 缓存 connection_info(TTL / until-error),非每请求 HTTP;manifest 仍 read-through 本地 `mdl.json` |
| secret 明文传输 | REST 返回明文 connection_info;REST <-> wren-mcp 须同机 unix socket 或 mTLS + 网络隔离 |
| REST 单点 | REST 挂了无法建新 project state(已缓存仍可用);可多实例 + 共享 SQLite |
| `wren_ask`/`wren_skills_*` 是无 project 纯工具 | 仍走 `run_blocked(state, ...)`,用任意 project state 的 lock(或独立全局 lock);不碰 project 资源 |

---

## 实施阶段

1. **Phase 1 — `sdk/wren-datasource/` REST 服务**:SQLite 后端 + profiles.yml 导入 + project/profile/connection CRUD + 测试
2. **Phase 2 — wren-mcp 改造**:`ProjectState` + `ProjectRegistry` + `ProjectRoutingMiddleware` + 工具闭包改造;`--project` 降级为 default;单项目向后兼容测试
3. **Phase 3 — memory 隔离 + 多 worker**:`collection_prefix` 按 project;多 worker 验证;contextvar 跨 thread 测试
4. **Phase 4 — 文档 + CI**:README + `sdk-datasource-ci.yml` + MCP client 多项目示例

---

## 不在范围

- wren-langchain / wren-pydantic 不改(直连 `wren.providers`,可选后续接 REST)
- wren-core Rust 引擎不改(已支持按 manifest 重建)
- `~/.wren/profiles.yml` 仍被 wren CLI 直接使用(REST 与之共存,可导入)
