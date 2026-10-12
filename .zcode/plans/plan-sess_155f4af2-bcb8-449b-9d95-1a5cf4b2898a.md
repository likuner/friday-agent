# backend 联网搜索改走 mcp-server + 启停脚本纳管 MCP 服务

只改 main 分支（AgentScope 2.0.8 版代码）。核心思路：backend 不再直连 GLM 搜索，改用 AgentScope 原生 `MCPClient`（streamable-http）接 mcp-server；start.sh/stop.sh 托管 mcp-server HTTP 服务。

## 任务 A：mcp-server 侧（1 处小改）

**`mcp-server/app/tools/web_search.py`**：`@mcp.tool` 装饰器加 `annotations=ToolAnnotations(readOnlyHint=True)`。
原因：AgentScope 的 `MCPTool` 对无 readOnlyHint 注解的工具默认 `ASK`（前端弹权限卡片），加注解后自动 `ALLOW`，与现有"只读检索自动放行"体验一致。实现时先验证 fastmcp 4.1.0 装饰器签名支持 annotations（不支持则改用 `mcp.add_tool(...)` 等价写法）。

## 任务 B：backend 改造

**1. `backend/app/config.py`** — 联网搜索配置组（现 glm_base_url/glm_model/glm_search_engine 三行）替换为：
```python
# 联网搜索：转交 mcp-server（streamable-http）执行；token 与 mcp-server/.env 的 MCP_AUTH_TOKEN 一致，服务端未配鉴权则留空
mcp_server_url: str = "http://127.0.0.1:8806/mcp"
mcp_auth_token: str = ""
```
实现时 grep 确认 `glm_base_url`/`glm_search_engine` 是否还有其他使用方（记忆摘要只用 `glm_model`，`zhipu_api_key` 保留给 embedding/记忆），仅 websearch 用的就删除，`glm_model` 保留。

**2. 删除 `backend/app/websearch.py`**（GLM 直连实现整体下线，git 可找回）。

**3. `backend/app/agent.py`**：
- 删 `from .websearch import ...` 两个 import（:26-28）
- `_build()` 里 web_search 的 `FunctionTool` 注册（:143-153）替换为构造 MCPClient：
```python
mcps = [MCPClient(
    name="common_tools",               # 工具名将为 mcp__common_tools__web_search
    is_stateful=False,                 # 每次调用临时建会话，无需 connect/close，mcp-server 重启无影响
    mcp_config=HttpMCPConfig(
        url=settings.mcp_server_url,
        headers={"Authorization": f"Bearer {settings.mcp_auth_token}"} if settings.mcp_auth_token else None,
        timeout=90,                    # 覆盖 mcp-server 内 GLM 检索 60s 超时 + 余量
    ),
    enable_tools=["web_search"],       # 白名单：mcp-server 未来加工具不自动灌入
)]
```
- `Toolkit(tools=tools)`（:179）改为 `Toolkit(tools=tools, mcps=mcps)`（仅 use_search 时非空）
- `use_search` 判据（:198-199）由 `web_search_available()`（查 zhipu key）改为 `bool(settings.mcp_server_url)`；未配置 URL 时保留现有提示词降级分支
- 核对 `prompts.py` 的 `WEB_SEARCH_PROMPT` 是否写死了工具名，必要时把名称描述改为与新工具名一致

mcp-server 挂掉时的行为：Toolkit 列工具失败只 warning 并跳过（不打崩对话），agent 表现为"没开联网搜索"，天然降级。

**4. 配置文件**：`backend/.env.example` 加 `MCP_SERVER_URL` / `MCP_AUTH_TOKEN` 示例与注释；`backend/.env` 直接补上这两个键（token 值从 `mcp-server/.env` 抄同一值，两个文件都已在本地存在）。

**依赖无变化**：agentscope 2.0.8 硬依赖 mcp<2.0.0，main 分支 requirements.txt 已含两者。

## 任务 C：start.sh / stop.sh 纳管 mcp-server

**`start.sh`** — 在「4. 后端」之前插入新的一节（原 4/5 顺延为 5/6），完全复用 backend 段的既有模式：
1. `cd "$ROOT/mcp-server"`；无 `.venv` 则 `python3 -m venv` + 升级 pip；无 `.env` 则从 `.env.example` 复制并 warn 提示配置 `ZHIPU_API_KEY`/`MCP_AUTH_TOKEN`
2. 端口 8806 已监听 → warn 并跳过（幂等）
3. requirements 哈希幂等同步（`.venv/.req.stamp` 戳记，同 backend 模式）
4. 导入预检 `.venv/bin/python -c "import app.main"`，失败输出 `.run/mcp_import_check.err`
5. 启动：`MCP_TRANSPORT=http nohup .venv/bin/python -m app > "$RUN_DIR/mcp-server.log" 2>&1 &`，PID 写 `.run/mcp-server.pid`（pydantic-settings 读进程环境变量，覆盖 .env 的 stdio 默认）
6. 就绪等待：每 2s 查 `port_listening 8806`，同时 `kill -0` 检测进程退出（退出即 tail 日志 fail，不等满超时）；60s 总超时。不用 curl /mcp 判 200（streamable-http 端点对普通 GET 不返回 200）
7. 脚本头注释与末尾汇总块补 mcp-server :8806 一行

**`stop.sh`** — 在 `stop_service "后端"` 之后加一行 `stop_service "MCP 工具服务" "mcp-server.pid"`（先停消费方再停服务）。

**`README.md`** — 一键启动段落补 mcp-server（:8806，承载联网搜索）说明。**`mcp-server/README.md`** 补一句"HTTP 模式由根 start.sh 托管启动/停止"。

## 验证

1. `cd mcp-server && .venv/bin/python scripts/smoke_test.py`（含 http 鉴权正反用例，同时确认 readOnlyHint 注解生效）
2. `./start.sh` 全量启动 → `.run/mcp-server.pid`/`mcp-server.log` 存在，8806 监听；重复跑 `./start.sh` 验证幂等跳过
3. 前端开联网搜索开关问时效性问题（如"今天有什么新闻"）：确认走 `mcp__common_tools__web_search`、无权限确认弹窗、回答带来源链接；关掉开关后工具不注册
4. `./stop.sh` 后确认 mcp-server 进程退出、pid 文件清理
