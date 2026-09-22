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
- `OPENAI_API_KEY`：DeepSeek API Key
- `OPENAI_BASE_URL`：默认 `https://api.deepseek.com`
- `OPENAI_MODEL`：默认 `deepseek-chat`

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

`app/agent.py` 的系统提示词要求 Agent 对医疗/医学/健康类问题先调用 `medical_rag_search`
检索本地医学文献向量库（RAG），再结合检索结果作答并注明来源；非医学问题不调用该工具。
工具实现见 `app/rag.py`（med-pgvector + 智谱 embedding-3）。

## 检查

```bash
python -m compileall -q app
```

应用启动时会自动创建 PostgreSQL 表；生产环境建议后续使用 Alembic 管理数据库迁移。
