# Friday Agent

一个名字叫做 Friday 的 AI Agent 应用：FastAPI + AgentScope 后端，Next.js + Ant Design 前端。
支持流式对话、深度思考、医学文献 RAG 检索、多模态图片输入与深色主题。

---

## 功能

| 功能 | 说明 |
| --- | --- |
| 账号体系 | 图片验证码 + JWT，注册 / 登录 / 会话持久化 |
| 流式对话 | 后端 SSE 增量推送，前端打字机节奏渲染 |
| 深度思考 | 切换到支持 reasoning 的模型，独立思考过程流式展示，回答结束后可折叠 |
| 医学 RAG | 模型自主调用 `medical_rag_search` 检索本地 PubMed 文献向量库；支持多角度并行检索，界面展示每次的实际检索词 |
| 多模态图片 | 一次最多上传 9 张图片，转 base64 作为多模态输入送模型，消息中固定尺寸裁剪展示、点击可预览 |
| 会话管理 | 历史记录列表、搜索、重命名、删除 |
| 体验细节 | 深色 / 浅色主题（无闪烁切换）、流式期间可上滑阅读（粘底滚动）、复制与点赞反馈 |

---

## 技术栈

**后端**

- FastAPI 0.115 + Uvicorn
- AgentScope 2.0.8（Agent / Toolkit / 事件流）
- SQLAlchemy 2.0 async + asyncpg + PostgreSQL 16
- pgvector（医学文献向量检索）+ 智谱 `embedding-3`
- Pydantic Settings、PyJWT、pwdlib[argon2]、Pillow、httpx

**前端**

- Next.js 16（App Router / Turbopack）+ React 19 + TypeScript
- Ant Design 5（`App.useApp()` 上下文实例，非静态方法）
- Tailwind CSS 3（颜色统一走语义化 CSS 变量）
- Zustand（登录态）、react-markdown + remark-gfm

---

## 目录结构

```
friday-agent/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口：路由挂载、/files 静态目录
│   │   ├── config.py          # Pydantic Settings（全部环境变量）
│   │   ├── auth.py            # JWT 签发与校验、密码哈希
│   │   ├── auth_routes.py     # 验证码 / 注册 / 登录 / 当前用户
│   │   ├── captcha.py         # 图片验证码生成
│   │   ├── conversations.py   # 会话 CRUD
│   │   ├── chat.py            # SSE 流式对话，落库消息与元数据
│   │   ├── agent.py           # Agent 组装、模型选择、多模态消息、事件转换
│   │   ├── rag.py             # 向量检索与 medical_rag_search 工具
│   │   ├── files.py           # 图片上传与安全校验
│   │   ├── models.py          # ORM 模型
│   │   ├── schemas.py         # 请求 / 响应模型
│   │   ├── logging_config.py  # 日志初始化
│   │   └── tracing.py         # OpenTelemetry 追踪初始化（OTLP 导出）
│   ├── scripts/
│   │   ├── eval_rag.py        # RAG 检索评测（Hit@K / MRR）
│   │   ├── eval_agent.py      # Agent 工具决策与回答质量评测
│   │   └── cleanup_files.py   # 按引用关系安全清理孤儿图片
│   ├── files/                 # 上传的图片（运行时生成，不入库）
│   ├── logs/                  # 日志（运行时生成，不入库）
│   ├── requirements.txt
│   └── README.md              # 后端细节
├── frontend/
│   └── src/
│       ├── app/
│       │   ├── layout.tsx     # 根布局：服务端读 cookie 决定主题
│       │   ├── providers.tsx  # 主题上下文 + antd ConfigProvider/App
│       │   ├── globals.css    # 语义化颜色 token（亮 / 暗两套）
│       │   └── (main)/        # 共享侧边栏布局：chat / history
│       ├── components/        # Shell / ChatWorkspace / AuthPage / ThemeToggle
│       ├── lib/api.ts         # 所有后端请求集中在此
│       └── store/             # Zustand：登录态、移动端抽屉
├── docker-compose.yml         # PostgreSQL（业务库）
└── README.md                  # 本文档
```

---

## 快速开始

### 1. 环境要求

| 组件 | 版本（开发环境实测） |
| --- | --- |
| Python | 3.12（3.11+ 可用） |
| Node.js | 22（20+ 可用） |
| Docker Desktop | 用于跑 PostgreSQL 与 pgvector |

### 2. 启动数据库

```bash
# 业务库（会话、消息、用户）
docker compose up -d postgres
docker compose ps
```

**医学 RAG 还需要一个独立的 pgvector 实例**（`docker-compose.yml` 未包含，需单独启动）：

```bash
docker run -d --name med-pgvector -p 5433:5432 \
  -e POSTGRES_DB=medrag -e POSTGRES_USER=meduser -e POSTGRES_PASSWORD=medpass \
  pgvector/pgvector:pg16
```

该库需要预先导入文献语料，包含两张表：`documents`（标题 / 来源 / URL）与 `chunks`（文本块 + 向量）。
未启动或不配置 `MEDRAG_DB_URL` 时，应用其余功能正常，只是医学问题检索不到文献。

### 3. 后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

cp .env.example .env               # 按需修改（见下方环境变量）
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- 健康检查：<http://localhost:8000/health>
- 接口文档：<http://localhost:8000/docs>
- 启动时会自动建表（生产环境建议改用 Alembic）

### 4. 前端

```bash
cd frontend
npm install
cp .env.example .env               # 默认指向 http://localhost:8000/api
npm run dev
```

打开 <http://localhost:3000>（会自动跳转到 `/login`）。

---

## 环境变量

### backend/.env

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `development` | 运行环境标识 |
| `DATABASE_URL` | `postgresql+asyncpg://friday:friday@localhost:5432/friday_agent` | 业务库连接串 |
| `JWT_SECRET` | `change-me` | **上线前必须替换**为长随机串 |
| `JWT_EXPIRE_MINUTES` | `10080` | Token 有效期（分钟） |
| `CORS_ORIGINS` | `http://localhost:3000` | 允许的前端来源，逗号分隔 |
| `MODEL_PROVIDER` | `demo` | `demo` 返回本地演示流；真正调用模型设为 `deepseek` |
| `OPENAI_API_KEY` | 空 | 模型服务密钥；为空时走演示模式 |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `OPENAI_MODEL` | `deepseek-chat` | 常规对话模型 |
| `OPENAI_THINKING_MODEL` | `deepseek-v4-flash` | 深度思考模型，需支持 reasoning；留空则退化为提示词引导 |
| `ZHIPU_API_KEY` | 空 | 智谱密钥，用于查询向量化 |
| `EMBEDDING_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | 向量化接口 |
| `EMBEDDING_MODEL` | `embedding-3` | 向量模型 |
| `EMBEDDING_DIMENSIONS` | `512` | 向量维度（需与语料入库时一致） |
| `MEDRAG_DB_URL` | `postgresql://meduser:medpass@localhost:5433/medrag` | 文献向量库连接串 |
| `RAG_TOP_K` | `4` | 单次检索返回条数 |
| `RAG_MIN_SCORE` | `0.0` | 相似度过滤阈值 |
| `TRACING_ENABLED` | `false` | 是否开启 OpenTelemetry 追踪 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP 端点；grpc 用 Studio 的 4317，http 填其 Web 端口（自动补 `/v1/traces`） |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | `grpc` 或 `http` |
| `OTEL_SERVICE_NAME` | `friday-agent` | 在追踪后端里显示的服务名 |

### frontend/.env

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000/api` | 后端 API 基址 |

> 两个 `.env` 均已加入 `.gitignore`，不会提交到仓库；仓库只提供 `.env.example` 模板。

---

## API 一览

除验证码、注册、登录外，其余接口都需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/auth/captcha` | 获取图片验证码 |
| `POST` | `/api/auth/register` | 注册（用户名、密码、验证码） |
| `POST` | `/api/auth/login` | 登录 |
| `GET` | `/api/auth/me` | 当前用户 |
| `GET` | `/api/conversations` | 会话列表 |
| `POST` | `/api/conversations` | 新建会话 |
| `GET` | `/api/conversations/{id}` | 会话详情（含全部消息） |
| `PATCH` | `/api/conversations/{id}` | 重命名 |
| `DELETE` | `/api/conversations/{id}` | 删除 |
| `POST` | `/api/conversations/{id}/messages` | **SSE** 流式发送消息 |
| `POST` | `/api/files` | 上传图片（multipart，仅图片，≤5MB） |
| `GET` | `/files/{name}` | 访问已上传图片（静态目录） |

### SSE 事件类型

`POST /api/conversations/{id}/messages` 返回 `text/event-stream`，每条 `data:` 为 JSON：

| `type` | 载荷 | 说明 |
| --- | --- | --- |
| `text` | `content` | 正文增量 |
| `thinking` | `content` | 思考过程增量（深度思考模式） |
| `tool_call` | `name`、`query` | 工具调用完成，含工具名与实际检索词 |
| `search` | `content` | 联网搜索占位提示（仅演示模式） |
| `error` | `content` | 出错信息 |
| `done` | `message_id` | 本轮结束 |

请求体字段：`content`（允许为空，表示只发图片）、`deep_thinking`、`web_search`、`attachments`（文件名数组，最多 9 个）。

---

## 可观测（AgentScope Studio）

后端通过 **OpenTelemetry** 上报 Agent 运行数据，可接入 [AgentScope Studio](https://github.com/agentscope-ai/agentscope-studio)
查看 trace 树、token 用量、耗时与完整调用属性。

> 实现说明：AgentScope 1.x 用 `agentscope.init(studio_url=...)` 上报，而本项目使用的
> **2.0.8 已移除该 API**，改为纯 OpenTelemetry 方案 —— 配好全局 `TracerProvider` 后，
> 挂在 Agent 上的 `TracingMiddleware` 会自动产出符合 OpenTelemetry GenAI 语义约定的 span。
> Studio 对外暴露的正是标准 OTLP 端点，因此两者可以直接对接。

### 使用

```bash
# 1. 安装并启动 Studio（默认 Web 端口 3000、OTLP/gRPC 端口 4317）
npm install -g @agentscope/studio
PORT=3001 as_studio          # 3000 被前端占用时换个端口

# 2. 后端开启追踪
cd backend
TRACING_ENABLED=true .venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

打开 <http://localhost:3001> → **Traces** 页面即可看到每次对话的调用链：

```
invoke_agent Friday
├── chat deepseek-chat                # 第一次模型调用（决定是否检索）
├── execute_tool medical_rag_search
├── execute_tool medical_rag_search   # 多角度并行检索
└── chat deepseek-chat                # 带检索结果的第二次模型调用
```

每条 trace 记录 token 用量（input / output / total）、耗时、模型名、会话 id，
以及符合 [GenAI 语义约定](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/)的完整属性。

### 接入第三方后端

任何支持 OTLP 的平台都可以对接，改端点即可，例如 Langfuse：

```bash
export OTEL_EXPORTER_OTLP_PROTOCOL=http
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public:secret)>"
```

未开启追踪时 `TracingMiddleware` 会自动短路，对调用链几乎没有开销。

## 设计要点

- **主题不闪烁**：主题存 cookie，根布局在**服务端**读出并渲染 `<html data-theme>`，antd 也在 SSR 阶段就用正确的算法生成样式；侧边栏折叠状态无法在服务端得知，改由首帧前的内联脚本写入 `<html data-sidebar>`。
- **颜色可控**：所有颜色收敛为 `globals.css` 里的语义化 token（亮 / 暗两套），Tailwind 映射这些 token，切换主题只改变量。
- **思考与正文分离**：后端把 `thinking` 事件单独收集进 `meta.thinking`，不混入正文，前端才能折叠展示。
- **附件只存文件名**：图片本体在 `backend/files/`，消息 `meta.attachments` 只记录文件名；每次只把**当前这条消息**的图片转 base64 送模型，历史图片不重复上传。
- **工具调用可追溯**：`meta.toolCalls` 记录每次检索的工具名与检索词，随消息落库，刷新后仍在。

---

## 开发辅助

```bash
cd backend

# RAG 检索评测（Hit@K / MRR），报告写入 logs/eval_rag.json
.venv/bin/python scripts/eval_rag.py

# Agent 工具决策与回答质量评测，报告写入 logs/eval_agent.json
.venv/bin/python scripts/eval_agent.py [--verbose]

# 安全清理未被任何消息引用的孤儿图片（默认只预览，加 --delete 才真删）
.venv/bin/python scripts/cleanup_files.py
.venv/bin/python scripts/cleanup_files.py --delete

# 语法检查
python -m compileall -q app
```

```bash
cd frontend
npm run build      # 生产构建
npx tsc --noEmit   # 类型检查
```

日志：控制台输出 INFO 及以上，同时写入 `backend/logs/backend.log`（DEBUG 及以上，单文件 5MB、保留 3 个备份）。

---

## 已知限制

- **默认模型已标记 sunset**：`deepseek-chat` / `deepseek-reasoner` 在 AgentScope 模型卡中为 `status: sunset`，当前可用的推理模型是 `deepseek-v4-flash` / `deepseek-v4-pro`。建议把 `OPENAI_MODEL` 一并迁到 v4 系列。演示模式（`MODEL_PROVIDER=demo`）不需要任何密钥。
- **`/files` 静态目录无鉴权**：依靠 32 位随机 UUID 文件名不可枚举来保护。正式产品建议改为带鉴权的下载接口或签名 URL。
- **点赞状态仅保存在前端**：刷新后重置，未落库。
- **历史图片不重复送模型**：多轮追问时，模型只能看到当前这一轮附带的图片。
- **单张图片上限 5MB**：9 张同时顶满时请求体会较大，可能触碰模型 API 的体积上限。
- **文献库未随仓库提供**：`medrag` 语料需自行准备并导入。

---

## 相关文档

- **问题盘点与演进方案（记忆 / 多模态 / CI-CD / 云上线）**：[`ISSUES.md`](ISSUES.md)
- 后端细节与接口说明：[`backend/README.md`](backend/README.md)
- 前端细节：[`frontend/README.md`](frontend/README.md)
