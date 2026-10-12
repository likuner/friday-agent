# mcp-server：通用公共工具 MCP 服务

基于 [FastMCP](https://gofastmcp.com) 的**无状态**通用工具服务：不绑定任何单一工具，所有公共工具集中在 `app/tools/` 下按模块实现并自动注册，供 agent Python 后端（http）与本地 MCP 客户端（stdio）复用。首个工具为 `web_search`——转交智谱 GLM 内置联网检索执行。

## 目录结构

```
mcp-server/
├── requirements.txt        # fastmcp / httpx / pydantic-settings
├── .env.example            # 配置模板（复制为 .env 使用）
├── scripts/smoke_test.py   # 冒烟测试（工具列表、真实搜索、http 鉴权正反用例）
└── app/
    ├── config.py           # 配置（.env 锚定本目录，不随 CWD 漂移）
    ├── auth.py             # http 模式 Bearer 鉴权（独立静态 token）
    ├── server.py           # FastMCP 实例组装（name / instructions / auth）
    ├── main.py             # 入口：stderr 日志 + 按配置选传输运行
    └── tools/              # 公共工具目录（自动注册）
        └── web_search.py   # 联网搜索（GLM 联网检索）
```

## 快速开始

```bash
cd mcp-server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # 填入 ZHIPU_API_KEY；http 模式再配 MCP_AUTH_TOKEN
chmod 600 .env
```

验证：

```bash
.venv/bin/python scripts/smoke_test.py
```

## 运行

- **stdio（默认）**：`MCP_TRANSPORT` 不配即为 stdio，由客户端作为子进程拉起，无需手动运行
- **http**：`MCP_TRANSPORT=http .venv/bin/python -m app`，监听 `MCP_HOST:MCP_PORT`（默认 `127.0.0.1:8806`），端点路径 `/mcp`
- **一键托管**：仓库根目录 `./start.sh` / `./stop.sh` 已把 http 模式纳入统一启停（PID 与日志在 `.run/mcp-server.*`），日常无需手动运行；backend 的联网搜索即经此 http 端点消费（`backend/.env` 的 `MCP_AUTH_TOKEN` 需与本服务一致）

stdio 模式下 stdout 是 JSON-RPC 通道，服务日志一律写 stderr。

## 鉴权（http 模式）

- `MCP_AUTH_TOKEN` 配置 Bearer token；支持**逗号分隔多个值**；留空则不鉴权（仅建议配合默认回环地址）
- stdio 模式不受鉴权影响（协议层无鉴权，本地子进程通信）
- token 生成：`openssl rand -hex 32`

**轮换流程（零停机）**：`MCP_AUTH_TOKEN` 中并列新旧值 → 重启 → 消费方全部切到新 token → 删除旧值再重启。token 只做进门比对，无签名派生、无会话落地，替换即刻生效。

**外网部署检查清单**：

1. `MCP_AUTH_TOKEN` 必须配置强随机值（不配则任何能访问端口的人都能消耗你的智谱配额）
2. `MCP_HOST=0.0.0.0` 前置于 HTTPS 反向代理（Nginx/Caddy），不要裸 HTTP 暴露
3. 若需面向用户级消费方（非机器对机器），升级 `app/auth.py` 为 `JWTVerifier`（JWKS 对接认证服务）或 OAuth Proxy——只换 auth 模块，工具代码零改动

## 消费方接入

### 本地客户端（stdio，ZCode / Claude Desktop 风格）

```json
{
  "mcpServers": {
    "common-tools": {
      "command": "/Users/likun/Documents/friday-agent/mcp-server/.venv/bin/python",
      "args": ["-m", "app"],
      "cwd": "/Users/likun/Documents/friday-agent/mcp-server"
    }
  }
}
```

### Python 后端（http + Bearer）

```python
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

client = Client(
    "http://127.0.0.1:8806/mcp",
    auth=BearerAuth("<MCP_AUTH_TOKEN>"),
    timeout=90,
)
async with client:
    tools = await client.list_tools()
    result = await client.call_tool("web_search", {"query": "GLM 最新模型", "count": 5})
```

等价的 curl：

```bash
curl -N http://127.0.0.1:8806/mcp \
  -H "Authorization: Bearer <MCP_AUTH_TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## 新增公共工具

在 `app/tools/` 下新建模块文件即可，**无需改动任何既有代码**：

```python
# app/tools/my_tool.py
from typing import Annotated
from pydantic import Field

from ..server import mcp

@mcp.tool
async def my_tool(query: Annotated[str, Field(description="参数说明")]) -> str:
    """工具描述：MCP 客户端（大模型）据此判断何时调用，写清「何时该用 / 何时别用」。"""
    ...
```

约定：

- 文件名即模块名，以下划线开头的模块不会被自动加载（预留给内部辅助代码）
- 保持无状态：不在模块级维护跨调用可变状态，每次调用自包含
- 工具失败时返回可读的错误文本而不是抛异常，让调用方模型能继续对话
- docstring 与参数 `Field(description=...)` 会进入 MCP 工具 schema，认真写

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ZHIPU_API_KEY` | 空 | 智谱开放平台密钥，`web_search` 依赖；未配置时工具返回可读提示 |
| `GLM_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | 智谱 API 地址 |
| `GLM_MODEL` | `glm-4-flash` | 执行联网检索的模型 |
| `GLM_SEARCH_ENGINE` | `search_std` | 搜索引擎（`search_std` 0.01元/次 / `search_pro` 0.03元/次） |
| `MCP_TRANSPORT` | `stdio` | `stdio` / `http` |
| `MCP_HOST` | `127.0.0.1` | 仅 http 模式生效；外网部署前必读上方检查清单 |
| `MCP_PORT` | `8806` | 仅 http 模式生效 |
| `MCP_AUTH_TOKEN` | 空 | http 模式 Bearer token，逗号分隔可配多个 |
