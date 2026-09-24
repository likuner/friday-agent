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
- [十、内置工具与权限模型（app/agent_tools.py）](#十内置工具与权限模型appagent_toolspy)
- [十一、已知设计权衡与问题](#十一已知设计权衡与问题)

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
    ├── conversations.py        # /api/conversations 路由：列表/创建/详情/重命名/删除 + 消息截断（编辑重发）
    ├── chat.py                 # /api/conversations/{id}/messages：SSE 流式对话（核心）
    ├── agent.py                # AgentService：AgentScope Agent 组装（开关组合懒加载）+ 事件流适配（核心）
    ├── agent_logging.py        # AgentLoggingMiddleware：Agent 执行段节点日志（模型/工具/耗时/token，挂载于 Agent）
    ├── agent_tools.py          # 内置工具注册与权限边界：工具清单 + 权限上下文 + deny 规则
    ├── workspace_picker.py     # 会话工作区：系统原生目录选择框（osascript/zenity）+ 路径校验
    ├── rag.py                  # 医学文献向量检索：智谱 embedding + pgvector（核心）
    ├── websearch.py            # 联网搜索：web_search 工具转交 GLM 内置联网检索执行（复用 zhipu_api_key）
    └── files.py                # 图片上传/解析：内容嗅探校验 + UUID 落盘 + 静态服务
├── workspaces/                    # Agent 内置工具的会话工作区（/workspaces/<会话id>/ 静态服务，运行时生成）
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
| `sent` | `message_id` | 流首事件：用户消息落库后的真实 id（chat.py 直发，非 agent 事件） | 替换乐观渲染的本地 id（编辑重发按 id 截断的前提） |
| `text` | `content` | 正文增量（token 级） | 进打字机缓冲，逐步上屏 |
| `thinking` | `content` | 深度思考增量 | 单独收集，折叠展示（不入正文） |
| `tool_call` | `name`, `query` | 模型发起了工具调用（`query` 为解析出的检索词/命令/文件路径摘要） | 渲染检索 chip：`medical_rag_search` →「已检索」，`web_search` →「联网搜索」，内置工具按名映射 |
| `permission_ask` | `reply_id`, `calls[]` | 权限 ASK：本轮 parked，等前端确认（DEFAULT/ACCEPT_EDITS 模式） | 渲染确认卡片（允许 / 总是允许 / 拒绝），应答走 `POST /permissions/confirm` |
| `permission_resolved` | `approved`, `timed_out` | 确认已处理（应答或超时自动拒绝），流即将续跑 | 卡片置为已允许 / 已拒绝 / 超时状态 |
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
  下发 `error` 事件给前端；
- **消息截断**（conversations.py `DELETE /{id}/messages/{mid}`）：删除指定消息及其后全部
  消息（`created_at >=` 目标），前端「编辑重发」先截断再重发；跨用户访问返回 404。

### 4.4 agent.py 的关键实现点

- **按开关组合装配**（`AgentService._get_agent()`）：以（深度思考, 联网搜索）组合为键懒加载并缓存
  Agent（各自独立 Toolkit 与对话状态）。普通对话用 `openai_model`（deepseek-chat）；
  「深度思考」打开且配置了 `openai_thinking_model`（deepseek-v4-flash）时切换到
  带 `thinking_enable=True` 的 reasoning Agent——思考过程以独立 `THINKING_BLOCK_DELTA`
  事件流出，前端折叠展示；未配置 thinking 模型则退化为提示词引导；
- **联网搜索工具**：「联网搜索」打开且配置了 `ZHIPU_API_KEY` 时，`web_search` FunctionTool
  （app/websearch.py）随组合注册，系统提示词追加时效性检索指令；主模型 tool_call 触发后，
  工具内部调用 GLM `chat/completions` + 内置 `web_search` 工具（`glm-4-flash` + `search_std`
  引擎），把「GLM 检索摘要 + 原始网页结果（标题/链接/摘要/发布时间）」回填给主模型引用作答；
  未配置密钥时降级为提示词引导（模型会说明无法联网）；
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

### 4.6 会话列表：分页 + 服务端搜索

`GET /api/conversations` 一次返回一页（`ConversationPage = {items, total, has_more}`），
参数：`q`（标题 ILIKE，服务端过滤，用户输入的 `%`/`_` 由 `_like_pattern` 转义为字面量）、
`kind`（`all` / `workspace` / `plain`，靠 `LEFT JOIN conversation_settings` +
`workspace_root IS [NOT] NULL` 区分，**无设置行与仅有权限模式的行都算 plain**）、
`limit`（默认 20，≤100）、`offset`。排序 `updated_at DESC, id DESC`——补 id 是为了
`updated_at` 同秒时 offset 分页不跳条不重条；`total` 用同样的过滤条件单独 `count(*)`。

前端 `components/LoadMore.tsx` 是两处共用的「加载下一页」交互：`IntersectionObserver`
观察底部哨兵（`rootMargin: 160px` 提前触发）自动加载，同时保留一枚可点按钮兜底
（触控高度 ≥32px，手机友好），`hasMore=false` 时显示「已全部加载」（`showEnd={false}`
可关闭，侧边栏「工作区」组即用它去掉收尾文案）。`hasMore` 由
`已加载条数 < total` 推导，因此删除/新增后无需额外同步。`loading` 由 true 回到 false
会重建 observer——新一页若仍在视口内会继续自动加载，不必等用户再滚一次。

- 侧边栏（`Shell.tsx`）：「工作区 / 对话」两组各自持有 `items + total` 与独立 `offset`，
  两个请求并发（`kind=workspace` / `kind=plain`），互不影响；
- 历史记录页：输入框 300ms 防抖后把关键词作为 `q` 发服务端；请求带序号，
  搜索词变化后旧响应一律丢弃，避免慢响应覆盖新结果。

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
| 结构化节点日志 | logging_config.py + 各模块 `friday.*` logger；Agent 执行段由 agent_logging.py 的 `AgentLoggingMiddleware` 产出（挂载于 `Agent(middlewares=...)`），服务层/工具内部日志仍在各模块 | 控制台 INFO + 滚动文件 DEBUG；节点清单：接收消息 / 流式开始 / Agent调用 / Agent返回（含轮次·整轮 token 合计·耗时） / 模型调用·返回（含每轮 token 用量·缓存命中） / 工具调用（含参数） / 工具结果（含状态·耗时） / RAG连接 / RAG向量化 / RAG检索开始·完成·命中·异常 / 联网搜索开始·完成·命中·异常 / 记忆合并（含合并后全文）/ 保存回复 / 流式完成·中止·异常 / 中止保存 / 多模态消息 / 图片上传 |
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

## 十、内置工具与权限模型（app/agent_tools.py）

「Agent 工具」开关（前端开关 × 服务端 `AGENT_TOOLS_ENABLED` 总开关，默认关）开启时，
在 `medical_rag_search`/`web_search` 之外注册 AgentScope 内置工具：Bash、Read、Write、Edit、
Glob、Grep、TaskCreate/Get/List/Update（PowerShell 仅 Windows 注册）。

工作区有两种形态（`resolve_workspace`，存 `conversation_settings.workspace_root`）：

| 形态 | 目录 | 说明 |
| --- | --- | --- |
| 默认（无设置） | `backend/workspaces/<会话id>/`（自动创建） | 会话隔离；产出文件经 `/workspaces/<会话id>/<文件名>` 静态提供（与 `/files` 同安全水位） |
| 会话自选（workspace_picker.py） | 用户自选的本机目录（不 mkdir） | DeepSeek Harness 式交互：后端跑在本机时，点「工作区」由后端弹**系统原生目录选择框**（macOS `osascript` 的 `choose folder`、Linux `zenity`），选完路径落库为该会话工作区（Bash 起始目录 + 写入自动放行区，改用 HOST 版提示词，无静态下载链路）。`validate_workspace_root` 校验：绝对路径、存在且是目录、不在敏感清单（.git/.ssh/.gnupg 等）。选框弹在**后端所在机器**屏幕——本机部署即用户本人，远程部署该形态退化（对话框弹在服务器上）；多用户部署时任何登录用户均可触发（进程级串行），信任边界由部署者把握 |

### 10.1 权限模式：请求侧可切换，默认 DEFAULT，会话记住选择

权限上下文经 `AgentState.permission_context` 注入，模式由 `ChatRequest.permission_mode` 指定，
**生效优先级：会话设置（conversation_settings.permission_mode）> 请求参数 > default**（服务端权威）。
前端仅工作区会话显示选择器（chip + Dropdown），改动即 `PUT /conversations/{id}/permission-mode`
落库。工作区会话自动开启 Agent 工具（前端发送 `agent_tools = !!workspace_root`，无手动开关），
工作区在侧边栏「工作区」组标题旁的 + 图标中选定（侧边栏无独立新建按钮；组头常驻，
零工作区时也可新建。先弹原生选框，取消零副作用；选定后
`PUT /workspace` 已设置即 409，**不可更改**）。侧边栏「工作区」组内每条会话只展示
**目录名**（取 `workspace_root` 最末级，悬停 title 给全路径），不展示会话标题。
BYPASS（跳过全部安全检查）不开放。

| 模式 | 行为 |
| --- | --- |
| `default` | 未授权操作产出 ASK → 推确认卡片给前端，应答后续跑；超时自动拒绝（见 10.2） |
| `accept_edits` | 工作区写入免确认，其余同 `default` |
| `explore` | 只读模式，一切修改直接拒绝 |
| `dont_ask` | 无交互场景：ASK 一律转 DENY（无人值守行为） |

各模式共同的裁决基底：Read/Glob/Grep 只读放行；Task 四件套恒 ALLOW；Write/Edit 工作目录内
自动放行（仅 ACCEPT_EDITS/DONT_ASK，DEFAULT 下会 ASK）；Bash 只读白名单放行、文件变更命令
仅限工作目录；deny 规则最高优先、危险命令等 bypass-immune 安全 ASK 无法被 allow 规则豁免。

### 10.2 确认链路（park & resume，app/confirm.py）

AgentScope 的 ASK 是 park-and-resume 模型：`reply_stream` 产出 `RequireUserConfirmEvent` 后
本轮自然结束（parked 状态留在 Agent 实例的 state 里），不悬挂协程。本项目对接方式：

1. agent.py 的事件循环收到该事件 → SSE 推 `permission_ask`（只含工具名与参数摘要，
   工具调用权威副本不出服务端，前端只回布尔值——防伪造）；
2. SSE 请求内 `await` ConfirmHub 的 asyncio.Event（进程内协调器），同一请求全程保活 parked Agent；
3. 前端卡片调 `POST /api/conversations/{id}/permissions/confirm` 应答 → 唤醒等待方 →
   以 `UserConfirmResultEvent` 为输入对同一 Agent 再次 `reply_stream()` 续跑；
4. 「总是允许」（always=true）：建议规则（suggested_rules）随确认事件喂给引擎（本 reply 内
   立即生效），同时存入 ConfirmHub 的会话规则表，后续轮次新建 Agent 时重放进 permission_context；
5. 无人应答超时（`PERMISSION_CONFIRM_TIMEOUT_SECONDS`，默认 120s）按拒绝续跑——模型收到
   denied 结果继续生成，流不会悬挂；SSE 断开（GeneratorExit）时清理登记。

限制：ConfirmHub 与会话规则均为进程内状态，多副本部署需换共享存储（与验证码存储同批改造）。

### 10.3 deny 规则（所有模式最高优先级）

deny 规则在权限引擎中先于只读放行与工具自身判定生效，清单在 `agent_tools.py:_deny_patterns()`：

- 敏感路径 glob：`**/.env*`、`**/*.db`、`**/.git*`、`**/.ssh*`、`**/.aws*`、`**/*.pem`、`**/id_rsa*`；
- 应用自身目录：`os.listdir(backend/)` 动态枚举除 `workspaces/` 外的全部顶层条目生成
  `f"{backend_dir}/{name}*"`（fnmatch 的 `*` 跨 `/`，覆盖子树）——源码、`.env`、上传图片、
  日志、依赖清单全部封禁，新增文件自动纳入保护；工作区是唯一豁免。

Read/Write/Edit 按 `file_path` 匹配，Glob/Grep 按其 `path` 参数匹配（后者只在模型显式传 path 时
拦得住，保护有限）。Bash 不配 deny 规则（其规则是命令子串匹配，易误伤），依赖框架内置的
危险命令黑名单 + 工作目录限制 + DONT_ASK 转拒。

本机开发逃生门：`AGENT_TOOLS_BASH_ALLOW_PREFIXES`（逗号分隔的命令子串，如 `open -a`）会生成
Bash allow 规则，放行 DONT_ASK 兜底本会拒绝的命令（如让 Friday 打开本机应用）。allow 在
deny/安全检查之后判定，放不进危险命令；命令仍在后端所在机器上执行，勿在多用户部署中开启。

### 10.4 已知限制（非硬隔离）

1. **框架把服务器进程 cwd 也当工作目录**（`ToolBase._path_in_allowed_working_path` 无条件并入
   `os.getcwd()`）：Bash 对 cwd（backend/）内的文件命令可写——Write/Edit 路径已被 deny 封住，
   但 Bash 侧无法用路径规则表达同等封禁，属已知残留；
2. Bash 只读白名单命令可读宿主上未被 deny 命中的普通文件（如 /etc/hosts）；
3. 任务列表存在 `AgentState.tasks_context`，Agent 每轮重建 → **仅单次回复内有效**（单次回复的
   多步规划够用）；跨轮持久化需把 tasks_context 序列化进 conversation，留作后续；
4. 硬隔离升级路径：把工具的 `backend` 换成 `DockerWorkspace` 等沙箱后端（AgentScope 原生支持），
   本次未做。

边界行为由 `tests/test_agent_tools.py` 锁定（deny 覆盖面 + 引擎裁决 + chip 摘要取值）。

---

## 十一、已知设计权衡与问题

| # | 事实 | 影响 | 计划 |
| --- | --- | --- | --- |
| 1 | `agent_service = AgentService()` 进程级单例，Agent 自带对话状态；`stream()` 只取 `messages[-1]`，传入的 `history` 是死参数 | 跨会话/跨用户记忆串味（已两次实证）、重启失忆、无法多副本 | 无状态化：每轮显式回放库中历史 + token 预算（ISSUES.md §1.1，第一优先级） |
| 2 | chat.py 每事件 `asyncio.sleep(0.05)` 人为限速 | 长回答整体被拖慢 | 移除，仅保留前端打字机节奏 |
| 3 | RAG 嵌入模型与建库模型不一致 | 中文裸查询检索质量坍塌（评测 Hit@4=0） | 统一模型并重建语料；评测脚本已可量化验证 |
| 4 | 验证码存进程内存、附件存本地磁盘、`create_all` 建表 | 多副本部署阻塞 | Redis / 对象存储 / Alembic |
| 5 | 无测试、无 CI | 重构风险高 | 先补鉴权/会话/SSE 三条主链路测试（ISSUES.md 第一批） |

---

*本文档与代码同步演进；深度问题分析见 [../ISSUES.md](../ISSUES.md)，产品视角见 [../POC.md](../POC.md)。*
