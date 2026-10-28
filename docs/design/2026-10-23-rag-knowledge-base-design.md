# RAG + 本地知识库设计（2026-10-23）

## 目标

为 llama.cpp 专业用户提供完全本地的文档知识库：导入本机文件、后台索引、语义/关键词检索、可选 rerank、Chat 自动注入带编号来源，并在消息中持久化 citations。不改变模型权重，不是训练。

## 非目标（首版）

- 不做 OCR、扫描件识别。
- 不自动下载模型或 llama-server。
- 不上传文件、不做云同步、不做多用户权限。
- 不做 ANN/外部向量数据库；单库目标约 20,000 chunks。
- 不做对话内模型自主多次检索（先做确定性的单次预检索）。

## 用户流程

1. 用户启动一个 embedding 实例（use-case recipe: Embeddings）和一个可选 reranker 实例。
2. 知识库页面新建库，选择 embedding 实例；设置 chunk 大小/重叠、Top-K、阈值、是否 rerank。
3. 添加文件或目录；后台索引；页面显示进度、文档状态、失败原因。
4. 检索测试页输入 query，检查命中片段、分数、rerank 结果。
5. Chat 选择知识库；发送消息时后端先检索、注入带编号上下文、再调用聊天模型；答案下方显示来源，重载后仍可见。

## 架构

```
files → extractor → chunker → embedding client → knowledge.db (metadata, FTS, float32 vectors)
                                                   ↓
chat query → embedding query → hybrid retrieval → optional rerank → context + citations → chat model
```

- 新模块：`backend/core/knowledge_store.py`、`knowledge_extract.py`、`knowledge_index.py`、`knowledge_retrieval.py`、`knowledge_embedding.py`、`knowledge_worker.py`。
- API 路由集中在 `backend/main.py`，沿用现有认证、请求上限和 `asyncio.to_thread`。
- 前端新增 `frontend/js/knowledge.js`；Chat 增加知识库选择和 citations。
- embedding/rerank 使用现有运行中实例，通过 `ProcessManager` 查询；不另起推理进程。

## 文件格式

- 文本/代码：UTF-8/UTF-8-SIG、UTF-16（BOM）、GB18030 回退；拒绝含 NUL 的二进制。
- `.md/.markdown/.txt/.json/.jsonl/.yaml/.yml/.toml/.ini/.cfg/.conf/.csv/.tsv/.log` 及常见代码后缀。
- `.html/.htm`：标准库 HTMLParser，移除 script/style/表单。
- `.pdf`：`pypdf` 文本层，保留页码；扫描件/无文本提示，不做 OCR。
- `.docx`：标准库 zipfile + XML 提取段落；不引入 python-docx。
- 目录递归扫描跳过 symlink/junction；只索引普通文件；单文件、单库总量和文件数有硬上限。

## 数据模型（knowledge.db）

- `knowledge_bases`：id, name, description, embedding_model, embedding_dim, chunk_chars, chunk_overlap, top_k, min_score, rerank_enabled, status, progress, error, timestamps。
- `documents`：id, base_id, path, display_name, kind, size, mtime, sha256, status, error, chunk_count, indexed_at。
- `chunks`：id, base_id, document_id, ordinal, text, page, heading, char_start, char_end, token_estimate, embedding BLOB(float32), norm。
- `chunks_fts`：FTS5（trigram；不可用时回退 LIKE）。
- 损坏数据库沿用 `sqlite_recovery` 隔离重建；备份/恢复包含 `knowledge.db`。

## 索引

- 单 worker 队列；状态 `queued/indexing/ready/error`，进度 0–100；后端重启把未完成任务标为中断。
- 每个文档先提取、分块、批量 embedding；全部成功后才替换旧 chunks；失败保留旧索引并记录文档错误。
- 默认 chunk 1200 字符、overlap 200；按段落/标题/页码切分；embedding batch 16。
- 文档 SHA-256 未变则跳过；模型/维度变化必须全库重建。
- 限额：2,000 文件/库、512 MiB/库、20,000 chunks/库；超限明确失败。

## 检索

- query embedding（当前 query 一次；不重算文档向量）。
- NumPy 归一化矩阵点积 + FTS5/关键词混合；Top-30 候选。
- 可选 rerank（Top-30 → Top-K）；rerank 实例不可用时明确记录 warning，不静默改变配置。
- 默认 Top-K 6，上下文预算最多约 2,000 tokens；按聊天 `ctx_size` 再收紧。
- 结果带 source：文档名、路径、页码/标题、片段、分数、chunk id。

## Chat 集成

- 会话新增 `knowledge_base_id`；创建/更新接口接受该字段。
- Chat 请求体新增 `knowledge_base_id`；后端在代理转发前检索并注入第二条 system message：
  `[Knowledge base sources]` + 编号片段 + 引用要求。
- 流式响应头 `X-LunarShroud-Sources` 传 base64url JSON（citations），CORS expose。
- 前端把 sources 放入 `assistant.stats.sources`，消息下方显示可预览的来源卡片；失败/重载仍可见。
- 无可用 embedding 实例或知识库为空时返回可读 409/400，不静默跳过 RAG。

## 资源策略

- 默认 embedding 使用小模型，建议 CPU/`-ngl 0`；rerank 默认关闭。
- 不自动启动/停止模型；知识库页和 Chat 明确显示所需服务状态。
- 索引后台运行，与聊天 GPU 解耦；embedding/rerank 选择独立运行实例。
- 模型路径/维度写入 KB；查询实例模型不匹配时拒绝。

## API（草图）

- `GET/POST /api/knowledge/bases`；`GET/PATCH/DELETE /api/knowledge/bases/{id}`
- `GET /api/knowledge/instances`：运行中 embedding/rerank 候选
- `POST /api/knowledge/bases/{id}/documents`（paths，启动索引）
- `GET /api/knowledge/bases/{id}/documents`；`DELETE .../documents/{doc_id}`
- `POST /api/knowledge/bases/{id}/reindex`；`POST .../cancel`
- `POST /api/knowledge/bases/{id}/search`
- `GET /api/knowledge/chunks/{chunk_id}`（预览）
- Chat：`POST /api/instances/{id}/chat/completions` 增加知识库字段。

## 测试与验收

- 单元：提取（含用户提供的 PDF/DOCX/HTML）、分块、哈希、限额、路径/链接拒绝、向量检索、混合排序。
- 集成：假 embedding/rerank 服务；索引→检索→Chat 注入→citations；无实例 409；模型不匹配 409。
- 真实模型：使用用户提供的 Qwen3-Embedding-0.6B-Q8_0、Qwen3-Reranker-0.6B-Q8_0 和 `study/知识库` 做一次端到端索引/检索验收。
- 前端：Node 纯函数测试；浏览器知识库页、进度、检索测试、Chat citations、Lighthouse/控制台。
- 打包：PyInstaller 包含 pypdf/numpy；packaged backend smoke；备份/恢复包含 knowledge.db。

## 分阶段

1. ✅ 后端数据层、提取/分块、embedding/索引、检索/rerank、API、备份。
2. ✅ 知识库页面、Chat KB 绑定与 citations、i18n/CSS。
3. ✅ P1/P2 前端：导航收敛、模型卡片、状态与加载、Playwright/截图回归；
   Lighthouse 依赖因高漏洞未引入，改由 axe-core + 首屏性能预算做 CI 门禁。
4. ⏳ 打包后端重建、安装对部署与多硬件长稳验收。
