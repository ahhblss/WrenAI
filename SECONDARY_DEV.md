# 二次开发说明

本仓库是 [Canner/WrenAI](https://github.com/Canner/WrenAI) 的 fork，在 `core/wren` 的 memory 子系统上做了如下替换：

| 组件 | 上游 | 本 fork |
|---|---|---|
| 向量库 | LanceDB（本地目录） | **Qdrant**（远程 server） |
| Embedding | sentence-transformers（本地模型） | **火山云方舟 Ark**（OpenAI 兼容 API） |
| 依赖 | lancedb + sentence-transformers + torch | qdrant-client + openai |

起始提交：`84c3b92b feat(memory): replace LanceDB with Qdrant + Volcengine Ark embeddings`

---

## 环境变量

`wren memory` 命令读以下环境变量（可放 `.env`）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `QDRANT_URL` | Qdrant server 地址（必填，启用 Qdrant 后端） | - |
| `QDRANT_API_KEY` | Qdrant 鉴权 key（私有集群才需要） | - |
| `VOLC_ARK_API_KEY` | 火山方舟 API key（真实 embedding 必填） | - |
| `VOLC_ARK_BASE_URL` | 火山方舟 base URL | `https://ark.cn-beijing.volces.com/api/v3` |
| `WREN_EMBEDDING_MODEL` | embedding 模型名 或 endpoint id | `doubao-embedding-text-240715` |
| `WREN_EMBEDDING_PROVIDER` | `fake`=本地无 key 调试；不填=火山云 | 不填（火山云） |
| `WREN_EMBEDDING_BATCH_SIZE` | 单次 embedding API 调用的文本数 | `10` |
| `WREN_MEMORY_BACKEND` | `grep`\|`qdrant` 强制后端；不填自动 | 自动 |

> - `WREN_EMBEDDING_MODEL` 填火山方舟控制台开通的模型名（如 `doubao-embedding-vision`）或 inference endpoint id（`ep-2024xxxx-xxxx`）。模型需先在方舟控制台开通。
> - `WREN_EMBEDDING_BATCH_SIZE` 默认 10 适配火山方舟单次 input 上限；换其他模型（限额更高）可调大。

---

## 配置 .env

`wren memory` 每次执行前自动加载 `.env`（复用 `wren.profile._ensure_env_loaded`），加载顺序（first match wins，shell export 优先于 .env）：

1. `$CWD/.env`
2. 项目根 `.env`（`wren_project.yml` 旁边）
3. `~/.wren/.env`（用户全局兜底）

推荐放 **wren project 根**。示例 `.env`：

```bash
QDRANT_URL=http://localhost:6333
VOLC_ARK_API_KEY=你的火山方舟key
WREN_EMBEDDING_MODEL=doubao-embedding-vision
# 调试时不用 key：
# WREN_EMBEDDING_PROVIDER=fake
```

> `.gitignore` 已忽略 `**/.env`，不会误提交密钥。

---

## 跑 memory（CLI）

前置：Qdrant server 在跑（`docker run -p 6333:6333 qdrant/qdrant`），火山方舟模型已开通。

```bash
# 1. 用源码 .venv 的 wren（见下节）
source D:/hz_workspace/my_wrenai/core/wren/.venv/Scripts/activate   # git bash

# 2. 进 wren project 目录（.env 在这里）
cd <你的 wren project>

# 3. 索引 + 检索
wren memory index                          # 索引 schema + seed queries 到 Qdrant
wren memory status                         # 查看 backend + collection 行数
wren memory recall -q "customer orders"    # 语义检索 NL->SQL
wren memory fetch -q "order status"        # schema 上下文检索
wren memory store --nl "..." --sql "..."   # 存自定义 NL->SQL
wren memory reset --force                  # 清空索引
```

---

## 用源码跑（开发模式）

`core/wren/.venv` 里的 `wrenai` 是 **editable 安装**，`import wren` 直接指向 `core/wren/src/wren/`，改源码即时生效，无需重装。

```bash
cd D:/hz_workspace/my_wrenai/core/wren

# 安装依赖（含 memory extra：qdrant-client + openai；网络不通加代理前缀）
HTTPS_PROXY=http://127.0.0.1:10808 uv sync --extra memory

# 跑 wren CLI（用 .venv 源码）
uv run wren memory status

# 跑测试（需 Qdrant 在 6333）
QDRANT_URL=http://localhost:6333 uv run pytest tests/unit/test_memory.py -v

# lint
uv run ruff check src/wren/memory
```

判断当前 `wren` 是不是源码版：`wren memory status` 输出 `Backend: qdrant` 即是（旧 LanceDB 版会显示 `lancedb`）。

---

## 同步上游 Canner/WrenAI

本 fork 的 main 可能落后上游。同步流程：

```bash
# 1. 加上游 remote（只需一次）
git remote add upstream https://github.com/Canner/WrenAI.git

# 2. 拉上游最新
git fetch upstream

# 3. 切 main，合并上游 main
git checkout main
git merge upstream/main

# 4. 有冲突就解决（memory 模块可能冲突，保留本 fork 的 Qdrant 版）
# 5. 推回自己的 fork
git push origin main
```

> 同步上游后，如果上游改了 memory 模块，可能和本 fork 的 Qdrant 改动冲突。冲突时优先保留本 fork 的 Qdrant 实现（`core/wren/src/wren/memory/`），再手工合并上游的其他改动。

---

## 破坏性变更（相对上游）

- `wren memory forget --id`：从 int 位置索引改为 Qdrant point id（str，`wren memory list` 的 `_row_id`）
- CLI `--path`（LanceDB 目录）改为 `--url`（Qdrant URL）
- SDK memory 自动启用：从 `.wren/memory/` 目录存在改为 `QDRANT_URL` 环境变量设置
- `memory` extra 依赖：`lancedb + sentence-transformers` → `qdrant-client + openai`

---

## 相关文件

- `core/wren/src/wren/memory/embeddings.py` — embedding 抽象（`VolcArkEmbedding` + `FakeEmbedding` + `get_default_embedding`）
- `core/wren/src/wren/memory/store.py` — Qdrant 版 `MemoryStore`
- `core/wren/src/wren/memory/index_backend.py` — grep/qdrant 后端选择
- `core/wren/src/wren/memory/cli.py` — `wren memory` CLI（含 `.env` 自动加载 callback）
- `sdk/wren-pydantic/`、`sdk/wren-langchain/` — SDK 的 `QdrantMemoryProvider`
- `docs/core/concepts/memory_system.md` — memory 系统文档（含配置表）
