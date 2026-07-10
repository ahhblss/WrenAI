# 二次开发：向量库替换为 Qdrant + Embedding 替换为火山云方舟

## 目标

将 `core/wren` 的 memory 子系统从 **LanceDB（本地）+ sentence-transformers（本地 embedding）** 替换为：
- **向量库**：Qdrant（仅远程 server 模式，通过 `QDRANT_URL` 连接）
- **Embedding**：火山云方舟 Ark（OpenAI 兼容接口，用 `openai` SDK 接入）
- **完全移除** LanceDB / sentence-transformers / transformers / torch 依赖

## 已确认的关键决策

| 决策点 | 选择 |
|---|---|
| 替换策略 | 直接替换 LanceDB，保留无依赖的 `GrepIndex` 作为 fallback |
| Qdrant 模式 | 仅远程 server（`QDRANT_URL` 必填） |
| Embedding SDK | `openai` SDK（OpenAI 兼容，指向火山方舟 base_url） |
| 本地 fallback | 完全移除 sentence-transformers |

## 架构设计

### 1. Embedding 抽象层（`embeddings.py` 重写）

定义可注入的 provider 接口，便于测试用 fake 替换火山云：

```python
class EmbeddingProvider(ABC):
    @property
    def dim(self) -> int: ...
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

class VolcArkEmbedding(EmbeddingProvider):
    """火山方舟 Ark embedding，OpenAI 兼容接口。"""
    # openai.OpenAI(base_url=VOLC_ARK_BASE_URL, api_key=VOLC_ARK_API_KEY)
    # client.embeddings.create(model=..., input=texts) -> [d.embedding for d in resp.data]
    # 内置分批（默认 16 条/批，WREN_EMBEDDING_BATCH_SIZE 可调）+ 重试
    # dim 首次调用探测并缓存（doubao-embedding-text-240715 = 2048）

class FakeEmbedding(EmbeddingProvider):
    """测试用：确定性伪向量（基于文本 hash），不依赖网络。"""
```

环境变量：
- `VOLC_ARK_API_KEY`（必填）
- `VOLC_ARK_BASE_URL`（默认 `https://ark.cn-beijing.volces.com/api/v3`）
- `WREN_EMBEDDING_MODEL`（沿用已有变量，默认值改为 `doubao-embedding-text-240715`）
- `WREN_EMBEDDING_BATCH_SIZE`（默认 16）

移除：`get_embedding_function`、`warm_up`、`suppress_stderr`、`_disable_transformers_progress_bar`（均为 LanceDB/sentence-transformers 专用）。

### 2. 存储层（`store.py` 重写 `MemoryStore`，基于 qdrant-client）

**Qdrant 概念映射**：
- LanceDB table → Qdrant collection：`{prefix}_schema_items`、`{prefix}_query_history`（prefix 默认 `wren`，避免多项目远程共享时冲突）
- LanceDB row → Qdrant point：`id=uuid4(str)` + `vector` + `payload`
- LanceDB `where` SQL 字符串 → Qdrant `Filter(must=[FieldCondition(key, MatchValue(value))])`
- pyarrow schema → 创建 collection 时 `VectorParams(size=dim, distance=COSINE)`

**构造函数（依赖注入，关键）**：
```python
MemoryStore(
    url: str | None = None,           # 默认 os.environ["QDRANT_URL"]，缺失报错
    api_key: str | None = None,       # 默认 os.environ.get("QDRANT_API_KEY")
    embedding: EmbeddingProvider | None = None,  # 默认 VolcArkEmbedding()
    collection_prefix: str = "wren",
)
```
默认从环境变量构造；测试注入 `FakeEmbedding` + testcontainers/service-container 的 Qdrant URL。

**方法映射**（保持公开 API 签名兼容，内部实现换 Qdrant）：
| 现有方法 | Qdrant 实现 |
|---|---|
| `index_schema` | extract items → `embed_texts` → `upsert` 批量 points |
| `_search_schema` | `query_points(query=vec, query_filter=Filter(mdl_hash,item_type,model_name), limit)` |
| `store_query` | embed nl → `upsert` 单 point |
| `recall_queries` | `query_points` + `Filter(datasource)` |
| `list_queries` | `scroll` 分页，返回 `_row_id`=point id(str) |
| `count_queries_by_source` | `count(count_filter=Filter(tags))` |
| `forget_queries_by_ids` | `delete(ids=[...])` —— **语义变化：id 从位置索引[int] 改为 point id[str]** |
| `forget_queries_by_source` | `delete(filter=Filter(tags))` |
| `dump_queries` | `scroll` 全量（去 vector） |
| `_existing_pairs_index` | `scroll` 全量构建 exact_set / nl_to_ids |
| `load_queries` | 复用 store_query + 去重（逻辑不变） |
| `schema_is_current` | `scroll` schema_items，校验所有 payload 的 mdl_hash 一致 |
| `status` | `get_collection` → count；返回 `{"url":..., "tables":{name:count}}` |
| `reset` | `delete_collection` 两张表 |

**关键变化点**：
- **point id 用 uuid4 字符串**：`_row_id` / CLI `--id` 从 `int` 位置索引改为 `str` point id。`forget_queries_by_ids(ids: list[str])`。CLI `forget --id` 参数类型 `list[int]` → `list[str]`。
- **datetime 存 ISO 字符串**：payload 是 JSON，`indexed_at`/`created_at` 存 `isoformat()` 字符串。CLI 输出处的 `hasattr(v, "isoformat")` 判断保留（兼容已是字符串的情况）。
- **移除** `pyarrow` import、`_esc`、`_schema_items_arrow_schema`、`_query_history_arrow_schema`、`_table_names`、`_db`/`_embed_fn`/`_dim` 内部属性（`_db` 被测试直接访问，需相应改测试）。
- **collection 不存在时自动创建**（带向量维度配置），首次写入触发。

### 3. 后端选择层（`index_backend.py`）

- `LanceDBIndex` → `QdrantIndex`（包装 Qdrant 版 `MemoryStore`，实现 `rebuild`/`search`/`reset`/`status`）
- `resolve_backend()` 返回 `"grep" | "qdrant"`；`WREN_MEMORY_BACKEND` 接受 `grep|qdrant`
- `_extra_available()` 检测 `qdrant_client` + `openai` 可导入
- qdrant 缺依赖或无 `QDRANT_URL` 时降级 grep
- `get_index` 的合法 backend 集合 `{"grep","qdrant"}`

### 4. 高层 API（`__init__.py` `WrenMemory`）

- `__init__(self, path=None)` 的 `path` 参数废弃（远程模式无本地目录），改为从环境变量读 Qdrant 配置；保留 `path` 形参但忽略并告警，或直接移除。倾向**移除 path 形参**，保持接口干净。
- docstring 更新 LanceDB → Qdrant。

### 5. CLI（`memory/cli.py`）

- `_get_store()`：捕获 `ModuleNotFoundError` 的模块集合从 `{lancedb, sentence_transformers, pyarrow}` 改为 `{qdrant_client, openai}`；错误提示文案更新。
- `PathOpt`（`--path`）语义：原指 LanceDB 目录。改为 `--url`（Qdrant URL，覆盖 `QDRANT_URL`），或保留 `--path` 作别名。倾向**新增 `--url`，`--path` 标记废弃**（短期保留避免脚本断链）。
- `memory_app` help、各命令 help 文本 LanceDB → Qdrant。
- `index`/`watch` 命令的 `resolve_backend() == "grep"` 分支保留不变。
- `forget --id` 参数类型 `list[int]` → `list[str]`（point id）。
- `status` 命令输出 `Backend: qdrant`，`info["tables"]` 逻辑保留；`path` 字段改 `url`。

### 6. SDK 层（`sdk/wren-pydantic`、`sdk/wren-langchain`）

- `_providers/memory.py`：`LocalLanceDBMemoryProvider` → `QdrantMemoryProvider`。`open()` 返回 Qdrant 版 `MemoryStore`（无 `memory_path`，从环境变量读 url）。`NoopMemoryProvider` 不变。
- `_toolkit.py`：
  - `_resolve_memory_provider` 现基于 `<project>/.wren/memory/` 目录存在与否。改为基于 `QDRANT_URL` 环境变量是否设置（设置且非空 → QdrantMemoryProvider，否则 Noop）。或保留本地标记目录作为"是否启用"开关 + url 来自环境变量。倾向**基于 `QDRANT_URL` 是否设置**。
  - 类型注解 `LocalLanceDBMemoryProvider` → `QdrantMemoryProvider`。
  - 注释 "MemoryStore is heavy (loads sentence-transformer model)" 更新（火山云是远程 API，无本地模型加载，但仍缓存实例避免重复建连）。

### 7. 依赖（`core/wren/pyproject.toml`）

```toml
memory = ["qdrant-client>=1.9", "openai>=1.0"]
```
移除 `lancedb`、`sentence-transformers`。`pyarrow` 保留在主依赖（其他模块用）。dev 组可选加 `testcontainers` 用于本地 Qdrant 测试。

### 8. 测试改造

**`tests/unit/test_memory.py`**（最大改动）：
- `memory_store` / `wren_memory` fixture：`pytest.importorskip` 从 `lancedb`/`sentence_transformers` 改为 `qdrant_client`/`openai`；构造 `MemoryStore(url=os.environ["QDRANT_URL"], embedding=FakeEmbedding())`。无 `QDRANT_URL` 时 `pytest.skip`。
- 直接访问 `memory_store._db.open_table(...)` / `table.to_pandas()` 的测试（`TestMemoryStoreSeedLifecycle` 等）改用公开 API（`list_queries`/`count_queries_by_source`/`dump_queries`）。
- `forget_queries_by_ids([0])` / `[0,2,4]` 位置索引测试：改为先 `list_queries` 取真实 point id 再 forget。
- `test_lancedb_backend_via_get_index`：`WREN_MEMORY_BACKEND=lancedb` + `idx.name == "lancedb"` → `qdrant`。
- `test_cli_export_migrates_query_history_to_markdown`：`MemoryStore(path=...)` → `MemoryStore(url=..., embedding=FakeEmbedding())`。
- 纯 `schema_indexer` 测试（`TestManifestHash`/`TestExtractSchemaItems`/`TestCubeSchemaItems`/`TestDescribeSchema`）**不变**。

**`tests/unit/test_index_backend.py`**：
- `test_resolve_backend_env_override`：`resolve_backend("lancedb")` 断言 → `"qdrant"`；`_extra_available` 含义随实现变。
- grep 后端测试**不变**。

**`tests/test_cli_memory_detection.py`**：
- `MEMORY_INSTALLED` 检测 `lancedb`+`sentence_transformers` → `qdrant_client`+`openai`。
- `_fresh_import_modules` 检测 `torch`/`lancedb` 不泄漏 → `qdrant_client`/`openai` 不泄漏（CLI 启动不 eager import）。
- 断言文案更新。

**`tests/unit/test_memory_watch.py`** / **`test_memory_markdown.py`** / **`test_served_content_guard.py`**：仅注释/文案更新，逻辑不变。

**SDK 测试**（`test_providers_memory.py`、`test_toolkit_init.py`、`test_memory_tools.py`）：provider 名与启用逻辑更新。

### 9. CI（`.github/workflows/wren-ci.yml` 等）

`test-memory` job：
- 移除 `Cache sentence-transformers model`（huggingface）步骤。
- `uv sync --extra memory` 现装 qdrant-client + openai。
- 新增 **Qdrant service container**（`qdrant/qdrant` docker image，端口 6333），设 `QDRANT_URL=http://localhost:6333`。
- embedding：CI 无火山云 key，测试用 `FakeEmbedding` 注入，不调真实 API。移除 `WREN_EMBEDDING_MODEL=paraphrase-MiniLM-L3-v2` env。
- `sdk-langchain-ci.yml` / `sdk-pydantic-ci.yml`：若有 memory 集成测试，同样加 Qdrant service container。

### 10. 文档

- `docs/core/concepts/memory_system.md`、`docs/core/reference/cli.md`、`docs/core/reference/architecture.md`、`docs/core/get_started/quickstart.md`、`core/wren/README.md`、`core/wren/docs/cli.md`、`core/wren/CHANGELOG.md`、SDK README 等：LanceDB → Qdrant，embedding 改火山云，新增环境变量说明（`QDRANT_URL`/`QDRANT_API_KEY`/`VOLC_ARK_API_KEY`/`VOLC_ARK_BASE_URL`/`WREN_EMBEDDING_MODEL`）。
- `core/wren/src/wren/memory/markdown.py`、`schema_indexer.py`、`seed_queries.py`、`cli.py`、`watch.py` 的 docstring 注释 LanceDB → Qdrant。

## 实施阶段（建议顺序）

1. **Embedding 层**：`embeddings.py` 重写（`EmbeddingProvider` + `VolcArkEmbedding` + `FakeEmbedding`）。
2. **存储层**：`store.py` 重写 `MemoryStore`（Qdrant + 依赖注入）。
3. **后端选择 + 高层 API**：`index_backend.py`（`QdrantIndex`）、`__init__.py`（`WrenMemory`）。
4. **CLI**：`memory/cli.py`（`_get_store`、`--url`、`--id` str、help 文案）。
5. **SDK 层**：两个 SDK 的 `_providers/memory.py` + `_toolkit.py`。
6. **依赖**：`pyproject.toml`（memory extra）。
7. **测试**：`test_memory.py`、`test_index_backend.py`、`test_cli_memory_detection.py`、SDK 测试。
8. **CI**：`wren-ci.yml` 等（Qdrant service container、移除 huggingface）。
9. **文档**：批量更新 LanceDB → Qdrant 文案与环境变量说明。
10. **验证**：`just test` / `just lint`（core/wren），SDK 各自测试；手动 `wren memory index` + `recall` 跑通（需本地 Qdrant + 火山云 key）。

## 风险与取舍

- **`forget --id` 破坏性变更**：从 int 位置索引改为 str point id，现有脚本/SDK 调用需适配。这是 Qdrant 无自增主键的必然结果。已在 CHANGELOG 标注。
- **远程 Qdrant 依赖**：本地开发/CI 必须有 Qdrant 实例（docker）。比原 LanceDB 本地目录门槛略高，但符合用户"仅远程 server"选择。
- **embedding 网络依赖**：火山云远程 API，断网则 memory 不可用（用户已确认移除本地 fallback，可接受）。
- **CI 无法测真实火山云**：用 `FakeEmbedding` 覆盖存储/检索逻辑；真实 embedding 留给手动验收。
- **collection 多项目隔离**：远程共享 Qdrant 时用 `collection_prefix` 隔离，默认 `wren`，可按项目配置。

## 不在本次范围

- 不动 wren-core（Rust 引擎）—— memory 是纯 Python 子系统。
- 不动 `markdown.py`/`schema_indexer.py`/`seed_queries.py`/`watch.py` 的逻辑（仅注释更新）。
- 不迁移现有 LanceDB 数据 —— markdown（`knowledge/sql/`）和 manifest 是 source of truth，Qdrant 是派生索引，重新 `wren memory index` 即可重建。
- 不保留 LanceDB 后端代码（直接替换，非并存）。
