# Friday Agent 后端架构文档

> 精确到文件级别的后端实现说明，重点拆解**流式对话**与 **RAG 检索**两条核心链路。
> 行号会随代码演进漂移，以函数名/文件名为准。产品视角与演进计划见 [../POC.md](../POC.md)
> 与 [../ISSUES.md](../ISSUES.md)。

---

## 目录

- [一、目录结构与文件职责](#一目录结构与文件职责)
- [二、启动流程](#二启动流程)
- [三、认证链路](#三认证链路)
- [四、流式对话实现（核心链路）](#四流式对话实现核心链路)
- [五、RAG 检索实现（核心链路）](#五rag-检索实现核心链路)
- [六、图片上传与多模态](#六图片上传与多模态)
- [七、数据模型](#七数据模型)
- [八、可观测体系](#八可观测体系)
- [九、脚本工具](#九脚本工具)
- [十、已知设计权衡与问题](#十已知设计权衡与问题)

---

## 一、目录结构与文件职责

```
backend/
├── .env                        # 环境变量（密钥、模型、数据库连接，不入库）
├── requirements.txt            # 依赖清单（fastapi/agentscope/asyncpg/pgvector 生态）
├── logs/                       # 运行日志与评测报告（backend.log / eval_*.json）
├── files/                      # 上传图片存储目录（/files 静态服务根）
├── scripts/
│   ├── eval_rag.py             # RAG 检索评测：Hit@K / MRR / 平均分 + LLM 裁判位
│   ├── eval_agent.py           # Agent 行为评测：工具决策断言 + ConsoleRenderer 可视化
│   └── cleanup_files.py        # 孤儿附件文件清理（按"是否被消息引用"判断）
└── app/
    ├── main.py                 # 应用入口：lifespan 装配（日志→追踪→建表→路由挂载）
    ├── config.py               # Settings（pydantic-settings，.env 驱动，全部配置项集中于此）
    ├── logging_config.py       # 日志初始化：控制台 INFO + 滚动文件 DEBUG 双 handler
    ├── tracing.py              # OpenTelemetry 初始化（OTLP → AgentScope Studio / Jaeger）
    ├── db.py                   # SQLAlchemy async engine + SessionLocal + create_all 建表
    ├── models.py               # ORM 三张表：users / conversations / messages
    ├── schemas.py              # Pydantic 请求/响应模型（注册、登录、对话、消息）
    ├── auth.py                 # 密码哈希(argon2)、JWT 签发/校验、current_user 依赖
    ├── auth_routes.py          # /api/auth 路由：captcha、register、login、me
    ├── captcha.py              # 图形验证码：Pillow 生成 + HMAC 签名 + 进程内存存储
    ├── conversations.py        # /api/conversations 路由：列表/创建/详情/重命名/删除
    ├── chat.py                 # /api/conversations/{id}/messages：SSE 流式对话（核心）
    ├── agent.py                # AgentService：AgentScope Agent 装配 + 事件流适配（核心）
    ├── rag.py                  # 医学文献向量检索：智谱 embedding + pgvector（核心）
    └── files.py                # 图片上传/解析：内容嗅探校验 + UUID 落盘 + 静态服务
```

---

## 二、启动流程

`app/main.py` 的 `lifespan` 按顺序执行：

1. `setup_logging()`（logging_config.py）—— 初始化 `friday` 根 logger：控制台 INFO +
   `logs/backend.log` DEBUG（RotatingFileHandler 5MB×3）；
2. `setup_tracing()`（tracing.py）—— `tracing_enabled=true` 时注册全局 OTLP TracerProvider；
   未启用时 `TracingMiddleware` 自动短路，零开销；
3. `init_db()`（db.py）—— `Base.metadata.create_all` 建表（无迁移工具，见 §10）；
4. 挂载路由：`auth_router` / `conversations_router` / `chat_router` / `files_router`
   （均带 `/api` 前缀），并将 `backend/files` 以 `/files` 静态目录对外提供；
5. 启动/关闭时分别执行 `setup_tracing()` / `shutdown_tracing()`。

---

## 三、认证链路

```
前端                    auth_routes.py              captcha.py / auth.py
 │ GET  /api/auth/captcha  ──▶ create_captcha() ──▶ 生成 4 位码 + HMAC 签名 token
 │ ◀── {image(base64), token}                   （Pillow 绘制，进程内存 dict 暂存）
 │
 │ POST /api/auth/register ──▶ verify_captcha() ─── 校验码 + 签名 + 10 分钟 TTL
 │      {username,password,captcha,token}    查重 → argon2 哈希入库（auth.py hash_password）
 │ ◀── {access_token(JWT HS256), user}       create_token()：sub=user_id, exp=7天
 │
 │ 后续所有请求 Authorization: Bearer <JWT>
 │                            ──▶ auth.current_user() 依赖：解码 → 查库 → 注入 User 对象
```

- 密码哈希：`pwdlib.PasswordHash.recommended()`（argon2）；
- 验证码存储在**进程内存**（多副本部署需迁 Redis，见 ISSUES.md P1）；
- 所有业务路由通过 `Depends(current_user)` 完成鉴权与用户注入。

---

## 四、流式对话实现（核心链路）

### 4.1 全链路时序

```
前端 ChatWorkspace          lib/api.ts streamMessage      chat.py chat()/generate()     agent.py stream()      AgentScope Agent
 │ POST /messages ─────────────▶ │                              │                          │                     │
 │                               │                      校验会话归属/内容非空                    │                     │
 │                               │                      用户消息落库 + 首条消息改写标题            │                     │
 │                               │                      构建 history（角色+内容）                 │                     │
 │                               │                      节点[接收消息]                            │                     │
 │                               │                      返回 StreamingResponse(text/event-stream) │                     │
 │                               │                              │ async for event                 │                     │
 │                               │                              │  ◀── yield {"type":...} ────────┤ Agent.reply_stream  │
 │                               │                              │      （事件映射，见 4.4）        │  ReAct 循环：        │
 │  onEvent(event) ◀─────────────┤ 逐事件 SSE 下发（+0.05s/条）  │                                │  reasoning→acting   │
 │   text/thinking → 打字机缓冲    │                              │                              │  工具调用→结果回填    │
 │   tool_call → 检索标签 chip     │                              │                              │  (medical_rag_search)│
 │   done/error → 收尾            │                              │ 全文+meta 落库（toolCalls/thinking）│                │
 │                               │                              │ yield {"type":"done"}         │                     │
```

### 4.2 SSE 事件协议（后端 → 前端契约）

定义方：`agent.py stream()` 产出、`chat.py sse()` 序列化（`data: {json}\n\n`），
前端 `lib/api.ts streamMessage()` 按 `\n\n` 分帧解析并回调 `onEvent`：

| type | 字段 | 含义 | 前端处理 |
| --- | --- | --- | --- |
| `text` | `content` | 正文增量（token 级） | 进打字机缓冲，逐步上屏 |
| `thinking` | `content` | 深度思考增量 | 单独收集，折叠展示（不入正文） |
| `tool_call` | `name`, `query` | 模型发起了工具调用（`query` 为解析出的检索词） | 渲染「已检索医学文献库」chip |
| `done` | `message_id` | 流正常结束（assistant 消息已落库） | 解除 loading |
| `error` | `content` | 服务端异常 | `message.error` 提示 |

> 注意 `tool_call` 事件在 `TOOL_CALL_END` 时才下发：此时参数 JSON 才收全，
> 能解析出检索词，多次并行多角度检索各自成 chip（agent.py `_tool_query()`）。

### 4.3 chat.py 的关键实现点

- **请求级落库**：用户消息先落库并改写会话标题（首条消息取前 30 字），再开流——
  即使流失败，用户输入也不丢；
- **事件转发循环**（`generate()`）：每收到一个事件就 `yield sse(event)` 并
  `await asyncio.sleep(0.05)` 做服务端节流（⚠️ 人为限速，长回答会被拖慢，计划移除，
  见 ISSUES.md §6.1）；
- **meta 富信息入库**：assistant 消息的 `meta` 同时保存 `deep_thinking` / `web_search` /
  `toolCalls`（工具名+检索词）/ `thinking` 全文——刷新或重进会话时这些信息不丢；
- **中止生成**（`except (asyncio.CancelledError, GeneratorExit)`）：客户端断开（AbortSignal）
  触发取消；由于请求级 db session 已随请求失效，用**独立 `SessionLocal()`** +
  `asyncio.shield` 把已生成的部分内容带 `stopped: true` 标记落库（`_save_partial()`），
  然后继续向上传播取消；
- **异常路径**：`logger.exception` 记录全节点上下文（事件数/字数/耗时），`db.rollback()` 后
  下发 `error` 事件给前端。

### 4.4 agent.py 的关键实现点

- **双 Agent 装配**（`AgentService._build()`）：普通对话用 `openai_model`（deepseek-chat）；
  「深度思考」开关打开且配置了 `openai_thinking_model`（deepseek-v4-flash）时切换到
  带 `thinking_enable=True` 的 reasoning Agent——思考过程以独立 `THINKING_BLOCK_DELTA`
  事件流出，前端折叠展示；未配置 thinking 模型则退化为提示词引导；
- **多模态消息**（`_user_message()`）：图片附件读文件 → base64 → `DataBlock(Base64Source)`，
  经 `OpenAIChatFormatter` 转成 OpenAI 兼容 `image_url`（AgentScope 自带的
  `DeepSeekChatFormatter` 会跳过 DataBlock，故显式替换 formatter）；
  附件名经 `files.py SAFE_NAME` 正则白名单校验，防路径穿越；
- **AgentScope 事件 → SSE 事件映射**：

| AgentScope EventType | 处理 | 产出的 SSE |
| --- | --- | --- |
| `TEXT_BLOCK_DELTA` | 直接透传 | `{"type": "text", "content": δ}` |
| `THINKING_BLOCK_DELTA` | 直接透传 | `{"type": "thinking", "content": δ}` |
| `TOOL_CALL_START` | 记录 call_id / name，日志 `节点[工具调用]` | —（等参数收全） |
| `TOOL_CALL_DELTA` | 按 call_id 累积参数 JSON 片段 | — |
| `TOOL_CALL_END` | 解析检索词，日志 `节点[工具参数]` | `{"type": "tool_call", "name", "query"}` |
| `TOOL_RESULT_END` | 日志 `节点[工具结果]` | —（结果已在 Agent 内部回填上下文） |

- **ReAct 循环**：Agent 内部自动完成「推理 → 调工具 → 结果回填 → 继续推理」，
  后端只消费事件流，无需手写工具循环；
- **演示模式降级**：未配置模型密钥时走本地模拟流（`_demo_answer()`），保证无 key 也能跑通交互。

### 4.5 前端消费契约（lib/api.ts）

- `fetch` + `AbortSignal`（停止生成按钮触发 `controller.abort()`，即上文的中止链路）；
- `ReadableStream` 手动解析：`buffer.split('\n\n')` 分帧 → 取 `data: ` 行 → `JSON.parse` →
  `onEvent` 回调；
- `onEvent` 内部不做渲染节流——**渲染节流在 ChatWorkspace 的打字机缓冲层**
  （50ms 一拍，按 `缓冲长度/8` 自适应放字），与后端节流相互独立。

---

## 五、RAG 检索实现（核心链路）

### 5.1 三层结构（app/rag.py）

```
模型层    Agent（ReAct 循环）── 决定是否调用、生成检索词、消费检索结果
             │  FunctionTool 包装（toolkit 注册，权限 ALLOW）
工具层    medical_rag_search(query, top_k=4) → str
             │  容错：检索失败返回错误说明而非抛异常（不让工具炸掉 Agent 循环）
             │  空结果：明确提示"未找到文献"，要求模型声明后基于自身知识回答
编排层    rag_search(query, top_k) → list[dict]
             │  节点日志（开始/完成/命中/异常）+ min_score 过滤
基础设施  embed_query() ── httpx → 智谱 /embeddings（embedding-3，dimensions=512）
          _get_pool()   ── asyncpg 连接池（懒加载 + 双检锁，1~4 连接）
```

### 5.2 关键实现细节

1. **查询向量化**（`embed_query`）：POST `{embedding_base_url}/embeddings`，
   `{"model": "embedding-3", "input": [text], "dimensions": 512}`——`dimensions` 必须与
   库内 `chunks.embedding vector(512)` 维度一致；
2. **向量检索 SQL**：asyncpg 不原生支持 vector 类型，把 512 维浮点拼成字面量后显式转换：

   ```sql
   SELECT c.chunk_text, d.title, d.source_name, d.source_url,
          1 - (c.embedding <=> $1::vector) AS score      -- 余弦距离转相似度
   FROM chunks c JOIN documents d ON d.id = c.document_id
   ORDER BY c.embedding <=> $1::vector
   LIMIT $2
   ```

   命中 `chunks` 表的 HNSW（vector_cosine_ops）索引；结果按 `rag_min_score` 过滤
   （当前默认 0.0，即全保留——见 §10 已知问题）；
3. **工具 schema 即函数签名**：`FunctionTool` 从 `medical_rag_search` 的签名与 docstring
   自动提取 JSON Schema 给模型；docstring 同时承担「何时该调用」的指令；
4. **权限显式放行**：`PermissionDecision(behavior=ALLOW)`——AgentScope 权限引擎默认要求
   人工确认工具执行，无头 SSE 场景必须显式允许，否则事件流停在 `REQUIRE_USER_CONFIRM`；
5. **检索词由模型生成**：模型会自主把中文问题改写成英文医学检索词（甚至多角度并行多次检索），
   跨语言场景实测 Top 相似度可达 0.63~0.65。

### 5.3 一次完整检索的节点日志

```
节点[工具调用]  model 自主决策调用工具 name=medical_rag_search call_id=call_01_…
节点[RAG检索开始] query="genetics of Parkinson's disease inherited…" top_k=5
节点[RAG向量化] model=embedding-3 dims=512 latency=0.28s
节点[RAG检索完成] hits=5 kept=5 top_score=0.6518 latency=0.28s
节点[RAG命中] #1 score=0.6518 title=… snippet=…          （DEBUG 级）
节点[工具参数] call_id=… args={"query": "…", "top_k": 5}
节点[工具结果] call_id=… 工具结果已返回给模型
```

### 5.4 已知问题

- **嵌入模型失配**：查询向量用 GLM embedding-3@512，而语料入库时使用了另一个 512 维模型
  （自检索校验证明存储无损，跨模型查询失配）。中文裸查询 Top 分仅 ~0.09；模型改写英文检索词后
  回升到 0.63+。根治方案：用同一模型重建语料（评测脚本已能量化验证）；
- 中文裸查直接进 `rag_search` 的场景（评测脚本）分数失真，属预期内。

---

## 六、图片上传与多模态

`app/files.py`：

- `POST /api/files`：读取原始字节 → 大小校验（≤5MB）→ **Pillow 按真实内容嗅探格式**
  （不信后缀与 Content-Type）→ `uuid4().hex` 命名落盘 `backend/files/` →
  返回 `{name, url, size}`；
- `resolve_stored_image()`：`SAFE_NAME` 正则（32 位 hex + 白名单后缀）防路径穿越，
  返回路径 + 发模型用的 MIME；
- 静态访问走 `main.py` 挂载的 `/files`（UUID 文件名不可枚举；加鉴权为既定改进项）；
- 多模态送模型见 §4.4 `_user_message()`。

---

## 七、数据模型

`app/models.py`（SQLAlchemy 2.0 声明式，PostgreSQL JSONB）：

```
users(id, username unique, password_hash, created_at)
   1:N (CASCADE)
conversations(id, user_id, title, created_at, updated_at)
   1:N (CASCADE, order_by created_at)
messages(id, conversation_id, role, content Text, meta JSONB, created_at)
```

`messages.meta`（JSONB，无 schema 版本号——见 ISSUES.md §6.2）当前承载：

| key | 写入方 | 内容 |
| --- | --- | --- |
| `attachments` | chat.py（用户消息） | 图片文件名列表 |
| `deep_thinking` / `web_search` | chat.py（助手消息） | 模式开关 |
| `toolCalls` | chat.py（助手消息） | `[{name, query}]` 工具调用记录 |
| `thinking` | chat.py（助手消息） | 深度思考全文 |
| `stopped` | chat.py `_save_partial` | 中止标记 |

---

## 八、可观测体系

| 层 | 实现 | 内容 |
| --- | --- | --- |
| 结构化节点日志 | logging_config.py + 各模块 `friday.*` logger | 控制台 INFO + 滚动文件 DEBUG；节点清单：接收消息 / 流式开始 / Agent调用 / 工具调用 / 工具参数 / 工具结果 / RAG连接 / RAG向量化 / RAG检索开始·完成·命中·异常 / 保存回复 / 流式完成·中止·异常 / 中止保存 / 多模态消息 / 图片上传 |
| 分布式追踪 | tracing.py + agent.py 的 `TracingMiddleware()` | OTLP（gRPC 4317 / HTTP 3000）导出，AgentScope Studio 可直接可视化 trace 树、token 用量、耗时；`tracing_enabled=false` 时零开销短路 |

日志设计约定：**节点[名称]** 前缀统一格式，每条链路可凭 `conversation` / `call_id` 串起全轨迹。

---

## 九、脚本工具

| 脚本 | 用途 | 备注 |
| --- | --- | --- |
| `scripts/eval_rag.py` | RAG 检索评测：8 条 golden 查询，Hit@K / MRR / 平均 Top 分 | LLM 裁判需 `EVAL_JUDGE_API_KEY`（留空自动跳过）；报告 `logs/eval_rag.json` |
| `scripts/eval_agent.py` | Agent 行为评测：医学问题应触发工具 / 非医学不应触发；agentscope `ConsoleRenderer` 可视化运行轨迹 | 实测 3/3 通过；报告 `logs/eval_agent.json` |
| `scripts/cleanup_files.py` | 孤儿附件清理（按引用判断，只预览可确认） | 见 ISSUES.md P1 附件管理 |

---

## 十、已知设计权衡与问题

| # | 事实 | 影响 | 计划 |
| --- | --- | --- | --- |
| 1 | `agent_service = AgentService()` 进程级单例，Agent 自带对话状态；`stream()` 只取 `messages[-1]`，传入的 `history` 是死参数 | 跨会话/跨用户记忆串味（已两次实证）、重启失忆、无法多副本 | 无状态化：每轮显式回放库中历史 + token 预算（ISSUES.md §1.1，第一优先级） |
| 2 | chat.py 每事件 `asyncio.sleep(0.05)` 人为限速 | 长回答整体被拖慢 | 移除，仅保留前端打字机节奏 |
| 3 | RAG 嵌入模型与建库模型不一致 | 中文裸查询检索质量坍塌（评测 Hit@4=0） | 统一模型并重建语料；评测脚本已可量化验证 |
| 4 | 验证码存进程内存、附件存本地磁盘、`create_all` 建表 | 多副本部署阻塞 | Redis / 对象存储 / Alembic |
| 5 | 无测试、无 CI | 重构风险高 | 先补鉴权/会话/SSE 三条主链路测试（ISSUES.md 第一批） |

---

*本文档与代码同步演进；深度问题分析见 [../ISSUES.md](../ISSUES.md)，产品视角见 [../POC.md](../POC.md)。*
