# Friday Agent 系统架构

> 本文档是**全局视角**的架构图与说明，覆盖 frontend / backend / mcp-server / shared 四个子项目、数据存储与外部服务。
> 后端内部实现细节（模块逐文件职责、SSE 协议字段、权限 deny 规则等）见 [`backend/ARCHITECTURE.md`](backend/ARCHITECTURE.md)；
> RAG 检索细节见 [`RAG.md`](RAG.md)，记忆系统设计见 [`MEMORY.md`](MEMORY.md)。

**一句话概括**：Next.js 前端 ↔ FastAPI 后端（AgentScope 编排 DeepSeek 模型）↔ FastMCP 工具服务（医学 RAG 混合检索 + 联网搜索），业务数据在 PostgreSQL，文献语料在 Elasticsearch，日志由 shared 包统一收敛到根目录 `logs/`。

---

## 一、系统总览（容器图）

```mermaid
flowchart TB
    User([用户浏览器])

    subgraph FE["frontend · Next.js 16 · :3000"]
        direction TB
        Pages["App Router 页面<br/>chat / history / login / register"]
        Chat["ChatWorkspace（核心组件）<br/>排队发送 · 拖拽排序 · 权限确认卡"]
        MD["Markdown 渲染<br/>代码高亮 · mermaid · KaTeX"]
        APIC["lib/api.ts 统一 API Client<br/>fetch + SSE 手动解析"]
    end

    subgraph BE["backend · FastAPI + AgentScope · :8000"]
        direction TB
        Routes["路由层<br/>auth / conversations / chat / files / workspaces"]
        AgentSvc["AgentService（无状态）<br/>每轮重建 Agent"]
        Ctx["上下文装配 + 两级记忆<br/>context / summarizer / memory"]
        Perm["权限确认 Hub<br/>park &amp; resume"]
    end

    subgraph MCPS["mcp-server · FastMCP · :8806"]
        direction TB
        FastMCP["common-tools 服务<br/>streamable-http · /mcp"]
        RAGT["medical_rag_search 工具"]
        WST["web_search 工具"]
    end

    subgraph DATA["数据与本地存储"]
        direction LR
        PG[("PostgreSQL 16 · :5432<br/>users / conversations / messages<br/>user_memories / conversation_settings")]
        ES[("Elasticsearch 9 · :9200<br/>medical_chunks_v2 索引")]
        FILES[("backend/files/<br/>上传图片（UUID 文件名）")]
        WS[("backend/workspaces/会话目录<br/>Agent 内置工具工作区")]
    end

    subgraph EXT["外部模型服务"]
        direction LR
        DS["DeepSeek API<br/>deepseek-v4-pro（对话/思考）<br/>deepseek-flash（多模态）"]
        ZP["智谱 API<br/>embedding-3 · rerank<br/>web_search · glm-5.3-flash"]
        OTLP["OTLP 追踪后端<br/>AgentScope Studio / Langfuse 等"]
    end

    User -->|HTTP| Pages
    Pages --- Chat
    Chat --- MD
    Chat -->|REST + SSE · Bearer JWT| Routes
    Routes --> AgentSvc
    Routes --> PG
    Routes --> FILES
    Routes --> WS
    AgentSvc --> Ctx
    AgentSvc <-->|权限确认| Perm
    AgentSvc -->|OpenAI 兼容流式| DS
    AgentSvc -->|MCP over HTTP| FastMCP
    FastMCP --> RAGT
    FastMCP --> WST
    RAGT --> ES
    RAGT -->|向量化 / rerank| ZP
    WST -->|联网检索 search_std| ZP
    Ctx -->|摘要 / 记忆抽取| ZP
    AgentSvc -.->|OpenTelemetry| OTLP
```

**要点**

- 前后端唯一通道是 `frontend/src/lib/api.ts`：普通请求走 fetch JSON，流式对话用 `POST /api/conversations/{id}/messages` 以 fetch reader 手动解析 SSE（无 WebSocket）。
- 后端 Agent **每轮无状态重建**（`AgentService` + `AgentState`），会话状态全部外置到 PostgreSQL，跨用户串味、重启失忆从架构上消除。
- mcp-server 是独立进程、独立 venv，通过 MCP streamable-http 挂进 Agent 的 Toolkit；两个工具均声明 `readOnlyHint=True`，Agent 侧自动放行。
- 多模态路由：带图请求自动切 `deepseek-flash`（`settings.llm_vision_model`），纯文本走 `deepseek-v4-pro`。

---

## 二、目录结构

```
friday-agent/
├── backend/                  # FastAPI + AgentScope 2.0.10 后端（:8000）
│   ├── app/
│   │   ├── main.py           # 入口：5 组路由挂 /api、lifespan（日志/追踪/建表）
│   │   ├── config.py         # Pydantic Settings（llm_* 前缀环境变量）
│   │   ├── auth.py / auth_routes.py / captcha.py
│   │   ├── conversations.py / chat.py        # 会话 CRUD / SSE 流式对话
│   │   ├── agent.py                        # AgentService.stream()：无状态编排入口
│   │   ├── agent_factory.py                # build_agent()：组装 AgentScope Agent
│   │   ├── agent_tools.py / agent_mcps.py / agent_skills.py
│   │   │                                    # 工具三层：内置 / MCP / 技能
│   │   ├── agent_messages.py / agent_events.py / agent_logging.py
│   │   ├── context.py / summarizer.py / memory.py   # 上下文装配 / 两级记忆
│   │   ├── confirm.py                      # 权限确认 park & resume
│   │   ├── rag.py / websearch.py           # RAG 与联网的请求侧接线
│   │   ├── files.py / workspace_picker.py
│   │   ├── models.py / schemas.py / db.py  # SQLAlchemy 2.0 async ORM
│   │   └── tracing.py                      # OpenTelemetry 初始化
│   ├── skills/medical-literature-search/   # AgentScope 技能（SKILL.md）
│   ├── scripts/               # eval_agent / cleanup_files / es 索引 mapping
│   ├── tests/                 # 纯函数单测（pytest）
│   ├── files/                 # 上传图片（运行时）
│   ├── workspaces/            # 每会话工作区（运行时）
│   └── ARCHITECTURE.md        # 后端内部架构文档（深入细节看这里）
├── frontend/                  # Next.js 16 + React 19 前端（:3000）
│   └── src/
│       ├── app/               # App Router：(main)/chat、history + login/register
│       ├── components/        # ChatWorkspace / Shell / Markdown / AuthPage…
│       ├── lib/api.ts         # 唯一后端 API client（含 SSE 解析）
│       └── store/             # Zustand：登录态 / UI 状态
├── mcp-server/                # FastMCP 公共工具服务（:8806）
│   ├── app/
│   │   ├── server.py          # FastMCP("common-tools")
│   │   ├── tools/rag_search.py / web_search.py
│   │   └── auth.py            # http 模式静态 Bearer token 校验
│   └── scripts/               # eval_rag / smoke_test
├── shared/                    # friday_shared 包：统一日志（纯 stdlib 零依赖）
│   └── friday_shared/logging_setup.py
├── logs/                      # 全仓统一日志目录（backend.log / mcp-server.log…）
├── docker-compose.yml         # 仅 PostgreSQL；ES/Kibana 由 start.sh 独立管理
├── start.sh / stop.sh         # 一键启停（容器 + MCP + 后端 + 前端）
└── README.md / RAG.md / MEMORY.md
```

---

## 三、核心链路：一次流式对话（时序图）

```mermaid
sequenceDiagram
    autonumber
    participant U as 浏览器
    participant FE as 前端 api.ts
    participant API as FastAPI chat.py
    participant AG as AgentService<br/>(AgentScope Agent)
    participant LLM as DeepSeek API
    participant MCP as mcp-server
    participant ES as Elasticsearch
    participant ZP as 智谱 API

    U->>FE: 发送消息（可带图片 / 深度思考 / 联网开关）
    FE->>API: POST /api/conversations/{id}/messages (Bearer JWT)
    API->>API: 用户消息落库；装配上下文<br/>长期记忆 + 滚动摘要 + 预算内回放窗口
    API-->>FE: SSE sent（真实 message_id，替换乐观 id）
    API->>AG: stream(消息, 配置)
    AG->>LLM: 第 1 次模型调用（system + 工具定义）
    LLM-->>AG: tool_call: medical_rag_search(检索词)
    AG-->>FE: SSE tool_call（工具名 + 实际检索词）
    AG->>MCP: 工具调用（HTTP /mcp）
    MCP->>ZP: 查询向量化 embedding-3
    MCP->>ES: knn + BM25 双路并行召回
    MCP->>MCP: 应用层 RRF 融合
    MCP->>ZP: rerank 精排
    MCP-->>AG: 文献结果 JSON（top_k=3）
    AG->>LLM: 第 2 次模型调用（带检索结果）
    LLM-->>AG: 流式回答（含 thinking 增量）
    AG-->>API: 事件流（text / thinking / tool_call）
    API-->>FE: SSE text… → done
    FE-->>U: 打字机渲染、思考面板、检索词 chip
    API->>API: 后台异步：溢出段摘要压缩 / 用户记忆抽取（GLM）
```

**SSE 事件协议**（完整字段见 [`backend/ARCHITECTURE.md`](backend/ARCHITECTURE.md) §4.2）：

| type | 说明 |
| --- | --- |
| `sent` | 流首：用户消息已落库的真实 id |
| `text` / `thinking` | 正文 / 思考过程增量（分离推送，前端可折叠） |
| `tool_call` | 工具调用完成，含工具名与实际检索词 |
| `permission_ask` | 内置工具待确认（park，前端弹确认卡） |
| `error` / `done` | 出错 / 本轮结束 |

---

## 四、Agent 编排与工具体系

### 4.1 每轮构建（无状态）

```mermaid
flowchart TB
    REQ["每轮请求"] --> BUILD["agent_factory.build_agent()"]

    subgraph AGENT["agentscope.agent.Agent（name=Friday）"]
        direction TB
        MODEL["DeepSeekChatModel<br/>流式；深度思考时 thinking_enable"]
        MW["中间件：TracingMiddleware（OTel）<br/>+ AgentLoggingMiddleware（自研）"]
        STATE["AgentState<br/>session_id + 滚动摘要 + 权限上下文"]
        TK["Toolkit：三类工具统一挂载"]
    end

    BUILD --> AGENT
```

Agent 进程内零跨请求状态：会话记忆在数据库，权限模式等会话级配置在 `conversation_settings` 表，每轮读出重建。

### 4.2 工具三层组装

```mermaid
flowchart LR
    TK["Toolkit"] --> BASH
    TK --> RAGT2
    TK --> SKILL

    subgraph T1["① 内置工具 · agent_tools.py"]
        BASH["Bash / Read / Write / Edit<br/>Glob / Grep / Task 四件套<br/>（Windows 加 PowerShell）"]
        PC["PermissionContext<br/>权限模式 + 工作区目录 + deny 规则<br/>（封禁 .env / *.db / .git 等）"]
        BASH --- PC
    end

    subgraph T2["② MCP 工具 · agent_mcps.py"]
        RAGT2["medical_rag_search<br/>（始终注册）"]
        WST2["web_search<br/>（联网开关开启时注册）"]
    end

    subgraph T3["③ 技能 · agent_skills.py"]
        SKILL["medical-literature-search<br/>SKILL.md 清单进 system prompt<br/>模型按需读取全文"]
    end

    RAGT2 --> SRV["mcp-server :8806"]
    WST2 --> SRV
```

调用与否完全由模型读 system prompt 与工具定义自主决策，后端只做**门控**（注册哪些工具）与**权限校验**（是否放行执行）。

### 4.3 权限确认链路（park & resume）

```mermaid
flowchart TB
    TOOL["Agent 请求执行内置工具"] --> MODE{权限模式}
    MODE -->|dont_ask / 工作区免确认| ALLOW["自动放行（仍受 deny 约束）"]
    MODE -->|accept_edits / 只读| RULE["按读写语义放行或询问"]
    MODE -->|default 每步确认| ASK["park：暂停 Agent 等待确认"]
    ASK --> SSE2["SSE permission_ask 事件"]
    SSE2 --> CARD["前端确认卡片<br/>（120s 超时自动拒绝）"]
    CARD -->|允许 / 本会话总是允许| RESUME["resume：UserConfirmResultEvent 续跑"]
    CARD -->|拒绝| DENY["工具调用被拒，Agent 收到拒绝原因"]

    MODE -.->|"deny 规则命中<br/>（任何模式）"| DENY2["直接拒绝"]
```

---

## 五、RAG 医学文献检索链路

实现位于 `mcp-server/app/tools/rag_search.py`，由 backend 经 MCP 调用。

```mermaid
flowchart LR
    Q["query 检索词"] --> EMB["智谱 embedding-3<br/>1024 维向量"]
    EMB --> KNN
    EMB --> BM25

    subgraph ESREC["Elasticsearch · medical_chunks_v2（别名 medical_chunks）"]
        KNN["knn 稠密召回<br/>k=20 · num_candidates=100"]
        BM25["BM25 稀疏召回<br/>IK 分词 · size=20"]
    end

    KNN --> GAT["asyncio.gather 并行"]
    BM25 --> GAT
    GAT --> RRF["应用层 RRF 融合<br/>score = Σ 1/(60+rank)<br/>取候选池 top10"]
    RRF --> RR["智谱 /rerank 精排<br/>（失败降级 RRF 序）"]
    RR --> FIL["min_score 相关性阈值过滤"]
    FIL --> OUT["返回 top_k=3<br/>title / source / url / chunk / score"]
```

- ES 服务端 RRF retriever 是付费功能，故在客户端应用层做融合（数学等价）。
- 索引契约：`chunk_text`（ik_max_word 索引 / ik_smart 检索）+ `embedding`（dense_vector 1024 维 cosine），mapping 存档于 `backend/scripts/es/medical_chunks_v2.mapping.json`。
- 联网搜索 `web_search` 是并列的另一个 MCP 工具：直连智谱 `/web_search` API（`search_std` 引擎）。

---

## 六、两级记忆系统

```mermaid
flowchart TB
    subgraph ROUND["每轮上下文组成（context.py 装配）"]
        direction TB
        S1["① system prompt + 工具定义<br/>+ 用户长期记忆（≤500 tok）"]
        S2["② 会话滚动摘要（AgentState.summary）"]
        S3["③ 回放窗口：token 预算内最近若干轮纯文本"]
        S4["④ 本轮用户消息 + 图片附件（图片只随本轮发）"]
    end

    subgraph BG["后台异步管线（不阻塞对话流）"]
        direction LR
        SUM["summarizer.py<br/>溢出段触发 GLM 压缩成滚动摘要<br/>游标幂等 · 失败下轮自愈"]
        MEM["memory.py<br/>抽取稳定事实与偏好<br/>合并重写 user_memories 单行<br/>新信息覆盖旧信息"]
    end

    SUM --> PGC[("conversations.summary<br/>+ 摘要/抽取进度游标列")]
    MEM --> PGM[("user_memories<br/>每用户一行")]
    PGC --> S2
    PGM --> S1
```

两条管线共用「每会话锁 + 进度游标 + 失败自愈」模式，均使用 GLM（glm-5.3-flash）执行，互不阻塞对话主流程。

---

## 七、数据模型

```mermaid
erDiagram
    users ||--o{ conversations : "创建"
    conversations ||--o{ messages : "包含"
    users ||--o| user_memories : "拥有（每用户一行）"
    conversations ||--o| conversation_settings : "工作区与权限配置"

    users {
        uuid id PK
        varchar username UK
        varchar password_hash
        timestamptz created_at
    }
    conversations {
        uuid id PK
        uuid user_id FK
        varchar title
        text summary
        uuid summary_upto_message_id
        uuid memory_extracted_upto
        varchar kind
    }
    messages {
        uuid id PK
        uuid conversation_id FK
        varchar role
        text content
        jsonb meta
        timestamptz created_at
    }
    user_memories {
        uuid user_id PK
        text content
        timestamptz updated_at
    }
    conversation_settings {
        uuid conversation_id PK
        varchar workspace_root
        varchar permission_mode
    }
```

- `messages.meta`（JSONB）记录 `thinking` / `toolCalls`（工具名 + 检索词）/ `attachments`（图片文件名），工具调用可追溯、刷新不丢。
- 建表策略：启动时 `create_all` + 对已有表幂等 `ALTER TABLE … ADD COLUMN IF NOT EXISTS`（未引入 Alembic）。
- 图片本体存 `backend/files/`（32 位随机名），库里只存文件名；历史轮次回放为 `[图片]` 占位。

---

## 八、部署与运维拓扑

```mermaid
flowchart TB
    START["./start.sh（顺序启动）"]

    START --> P1["① docker · friday-agent-postgres :5432<br/>等 healthcheck（docker-compose.yml）"]
    START --> P2["② docker · med-es :9200<br/>ES 9.5.4 单节点 + IK 插件（--no-es 跳过）"]
    START --> P3["③ docker · med-kibana :5601（存在才启）"]
    START --> P4["④ mcp-server :8806<br/>MCP_TRANSPORT=http · nohup 托管"]
    START --> P5["⑤ backend :8000<br/>uvicorn --reload · /health 判活"]
    START --> P6["⑥ frontend :3000<br/>npm run dev"]
```

| 服务 | 端口 | 进程/容器 | 说明 |
| --- | --- | --- | --- |
| 前端 Next.js | 3000 | start.sh nohup | App Router + Turbopack dev |
| 后端 FastAPI | 8000 | start.sh nohup | `/api` 前缀 + `/health`，CORS 白名单前端 |
| mcp-server | 8806 | start.sh nohup | streamable-http `/mcp`，可选 Bearer token |
| PostgreSQL | 5432 | docker compose | 业务库 `friday_agent` |
| Elasticsearch | 9200 | docker（独立管理） | https + basic auth，自签证书 |
| Kibana | 5601 | docker（可选） | ES 观测 |

**统一日志**：`shared/friday_shared`（纯 stdlib、零第三方依赖）由 backend 与 mcp-server 的 requirements 以 `-e ../shared` 可编辑安装，各自 venv 一份：

```mermaid
flowchart LR
    SH["shared/friday_shared<br/>setup_service_logging()"] -->|"-e ../shared"| BV["backend venv"]
    SH -->|"-e ../shared"| MV["mcp-server venv"]
    BV --> L1["logs/backend.log<br/>DEBUG · 5MB×3 滚动"]
    MV --> L2["logs/mcp-server.log<br/>DEBUG · 5MB×3 滚动"]
    BV & MV --> L3["logs/*.console.log<br/>start.sh 控制台镜像"]
```

- PID 文件与导入预检输出在 `.run/`；`./stop.sh` 按「先停消费方再停 MCP」顺序回收进程，容器 stop 但保留数据卷。
- start.sh 按 requirements 文件哈希幂等安装依赖，在 main（AgentScope 栈）与 feature 分支（LangGraph 栈）间切换后重跑即可自动升降级。
- 可观测：`TRACING_ENABLED=true` 时 `TracingMiddleware` 产出 OpenTelemetry GenAI 语义约定 span，经 OTLP 上报 AgentScope Studio 或任意兼容后端（如 Langfuse）；未开启时自动短路，近乎零开销。

---

## 九、技术栈速览

| 层 | 技术 |
| --- | --- |
| 前端 | Next.js 16（App Router）· React 19 · TypeScript · Ant Design 5 · Tailwind CSS 3 · Zustand · @dnd-kit · react-markdown / mermaid / KaTeX |
| 后端 | FastAPI 0.115 · Uvicorn · AgentScope 2.0.10（Agent / Toolkit / 事件流）· SQLAlchemy 2.0 async + asyncpg · Pydantic Settings · PyJWT · pwdlib[argon2] · Pillow · OpenTelemetry |
| MCP 工具服务 | FastMCP（streamable-http）· elasticsearch[async] 9.1 · httpx |
| 数据 | PostgreSQL 16（业务）· Elasticsearch 9.5 + IK（文献语料） |
| 模型 | DeepSeek（对话 / 思考 / 多模态）· 智谱 GLM（embedding / rerank / 联网搜索 / 摘要与记忆抽取） |

## 十、相关文档

| 文档 | 内容 |
| --- | --- |
| [`backend/ARCHITECTURE.md`](backend/ARCHITECTURE.md) | 后端内部架构：模块职责、启动流程、认证链路、SSE 协议、权限模型细节 |
| [`RAG.md`](RAG.md) | Elasticsearch 混合检索完整实现与索引契约 |
| [`MEMORY.md`](MEMORY.md) | 两级记忆系统的设计、代码地图与验收记录 |
| [`README.md`](README.md) | 功能清单、快速开始、环境变量、API 一览 |
| [`backend/README.md`](backend/README.md) / [`frontend/README.md`](frontend/README.md) | 子项目细节 |
