# Friday Agent 后端

基于 FastAPI、AgentScope 2.0.8、SQLAlchemy async、Pydantic Settings 和 PostgreSQL 的 AI Agent 服务。

## 环境要求

- Python 3.11+
- Docker Desktop
- PostgreSQL 16 镜像
- DeepSeek API Key（可选；未配置时使用本地演示流式回复）

## 创建虚拟环境

后端使用项目自己的虚拟环境，避免污染全局 Python 包：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

后续后端命令请在激活 `.venv` 后执行，或直接使用 `backend/.venv/bin/python`。

## 配置

```bash
cp .env.example .env
```

主要配置项：

- `DATABASE_URL`：PostgreSQL 异步连接地址
- `JWT_SECRET`：JWT 签名密钥，生产环境必须替换
- `CORS_ORIGINS`：前端地址，默认 `http://localhost:3000`
- `MODEL_PROVIDER`：默认 `demo`；真实模型设置为 `deepseek`
- `LLM_API_KEY`：DeepSeek API Key
- `LLM_BASE_URL`：默认 `https://api.deepseek.com`
- `LLM_MODEL`：默认 `deepseek-v4-pro`

## 启动 PostgreSQL

在项目根目录执行：

```bash
docker compose up -d postgres
docker compose ps
```

## 启动 API

在 `backend` 目录并激活虚拟环境后执行：

```bash
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

API 文档：<http://localhost:8000/docs>

## 用 PyCharm 断点调试

断点只对**由 PyCharm 以 Debug 方式启动**的进程生效。若后端是 `./start.sh` 拉起的
（nohup uvicorn 常驻），Apifox / 前端的请求进的是那个进程，PyCharm 断点不会命中。
调试前先停掉常驻后端，其余服务（PostgreSQL / ES / MCP / 前端）保留不动：

```bash
# 项目根目录执行；--reload 的子进程会随之退出
kill "$(cat .run/backend.pid)"
# 确认 8000 已释放（应无输出）
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

然后新建 Run/Debug Configuration（Run → Edit Configurations → ＋ → Python）：

| 配置项 | 值 |
|---|---|
| 运行方式 | Module name（不是 Script path）：`uvicorn` |
| Parameters | `app.main:app --host 0.0.0.0 --port 8000` |
| Working directory | `backend/`（即 PyCharm 项目根） |
| Python interpreter | `backend/.venv` |

点 Debug 启动，控制台出现 `Uvicorn running on ...` 后再用 Apifox 或前端请求，断点即可命中。

注意：

- Parameters **不要带 `--reload`**。reload 会派生 multiprocessing 子进程运行应用，
  调试器挂在父进程上，断点同样不会命中；调试期间改代码后重新点一次 Debug 重启即可。
- 调试期间跑 `./start.sh` 无害：它检测到 8000 被占用会跳过后端，其余服务照常启动。
- 调试结束想恢复常驻模式，先停掉 PyCharm 里的运行，再跑一次 `./start.sh`。

## 主要接口

- `GET /api/auth/captcha`：获取图片验证码
- `POST /api/auth/register`：用户名、密码、验证码注册
- `POST /api/auth/login`：用户名、密码、验证码登录
- `GET /api/auth/me`：获取当前用户
- `GET /api/conversations`：获取当前用户历史对话
- `POST /api/conversations`：创建对话
- `GET /api/conversations/{id}`：获取对话详情
- `POST /api/conversations/{id}/messages`：SSE 流式发送消息
- `DELETE /api/conversations/{id}/messages/{message_id}`：截断该条消息及其后全部消息（前端「编辑重发」用）

## 医学 RAG

`app/agent.py` 把 `medical_rag_search` 经 MCP 客户端从 mcp-server 注册进来（与 `web_search`
同链路），系统提示词要求 Agent 对医疗/医学/健康类问题先检索本地医学文献库
（Elasticsearch 混合检索：稠密+BM25 RRF 融合 → rerank 精排），再结合检索结果作答并注明来源；
非医学问题不调用该工具。
检索实现与 ES/embedding/rerank 配置都在 mcp-server 侧（`mcp-server/app/tools/rag_search.py`），
完整流程说明见根目录 [`RAG.md`](../RAG.md)。

## 检查

```bash
python -m compileall -q app
```

应用启动时会自动创建 PostgreSQL 表；生产环境建议后续使用 Alembic 管理数据库迁移。
