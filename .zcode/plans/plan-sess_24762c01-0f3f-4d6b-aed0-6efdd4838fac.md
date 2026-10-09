# 迁移方案：AgentScope → LangGraph + LangChain（保持功能一致）

## 总体思路

服务层（FastAPI 路由、SSE 协议、落库）、记忆体系（context/summarizer/memory）、RAG/联网搜索工具**全部不动**——它们不依赖 agentscope。迁移面集中在 4 个模块（agent.py / agent_tools.py / agent_logging.py / confirm.py）+ 3 个测试 + 1 个脚本。

用**自建 LangGraph StateGraph**（agent → 权限门 → tools 循环）替代 AgentScope 的 `Agent.reply_stream()` ReAct 循环；权限引擎自研复刻（语义矩阵从 agentscope 源码逆向确认）；10 个内置工具自研简化版（工具名/参数 schema/描述保持一致，内部用标准库实现）；人工确认用 LangGraph 原生 `interrupt()` + `Command(resume=...)`，ConfirmHub 协调逻辑保留。

## 依赖变更（backend/requirements.txt）

- 移除：`agentscope==2.0.8`、`mcp==1.30.0`（项目代码未使用）
- 新增：`langgraph`、`langchain`、`langchain-openai`（安装时取最新稳定版并回填固定版本号）
- 保留：`openai`、opentelemetry 三件套（tracing 用）

## 文件级变更

### 新增 `app/permissions.py`（权限引擎，~350 行）
- `PermissionMode`（default/accept_edits/explore/dont_ask，BYPASS 不开放）、`Behavior`（ALLOW/DENY/ASK）、`Decision`、`SessionRule(tool_name, rule_content, source)` 数据类替代 agentscope.permission 的类型
- `decide(tool_name, tool_input, context)`，判定顺序复刻 agentscope 引擎：①deny 规则（文件类工具 glob 匹配 / Bash 子串匹配）→ ②只读快路径（Read/Glob/Grep、Task 四件套、Bash 只读白名单命令 → ALLOW）→ ③工具特判（Write/Edit 与 Bash 文件变更命令：路径全在工作区内且 mode ∈ {accept_edits, dont_ask} → ALLOW，否则落入后续）→ ④allow 规则（含会话级 always 规则重放、AGENT_TOOLS_BASH_ALLOW_PREFIXES）→ ⑤模式兜底（default/accept_edits → ASK，explore → DENY，dont_ask → DENY）
- `medical_rag_search`/`web_search`/Task 四件套无条件 ALLOW（对应现在的显式 ALLOW permission）
- Bash 只读判定：复制 agentscope 的 `READ_ONLY_COMMANDS` 清单（ls/cat/git status/docker ps 等约 60 项），shlex 解析替代 tree-sitter；复合命令（&&/||/;/|）拆分后逐一判定，含 `>` 重定向一律非只读
- `generate_suggestions()`："always" 建议规则——Bash 取前两词 `"git commit:*"` 前缀式、Write/Edit 取目录 `"dir/**"` glob 式（与 agentscope 策略一致）

### 重写 `app/agent_tools.py`（10 个 LangChain 工具，~700 行）
- 工具名、参数 schema、description 文本从 agentscope 源码原样提取（模型侧行为不变，提示词无需改）：`Bash(command, description, timeout)`、`Read(file_path, offset, limit)`、`Write(file_path, content)`、`Edit(file_path, old_string, new_string, replace_all)`、`Glob(pattern, path)`、`Grep(pattern, path, output_mode)`、`TaskCreate/TaskGet/TaskList/TaskUpdate`
- 内部实现：Bash 用 `asyncio.create_subprocess_shell`（cwd=工作区、timeout 强杀、输出截断 ~30KB）；Read 按行号 cat -n 格式（去 PDF 分页）；Edit 精确匹配+唯一性校验；Task 四件套操作每请求独立的 `TaskStore`（闭包注入，单轮有效——与 AgentState.tasks_context 生命周期一致）
- `resolve_workspace` / `_deny_patterns`（敏感路径 glob + backend/ 动态枚举）逻辑原样保留；`build_builtin_tools(workspace, mode)` 签名不变，返回 `(list[BaseTool], PermissionContext)`

### 新增 `app/graph.py`（StateGraph + 流式，~250 行）
- 状态：`messages`（`add_messages`）+ `denied_calls: list[str]`
- `agent_node`：`llm.astream()` 逐 chunk——`chunk.content` → 写 `{"type":"text"}`、`additional_kwargs["reasoning_content"]` → 写 `{"type":"thinking"}`（`get_stream_writer()`）；流结束聚合 AIMessage，若含 tool_calls 则按序写 `{"type":"tool_call", name, query}` chip（参数收全才发，与现在 TOOL_CALL_END 时机一致）；节点内嵌 `节点[模型调用/返回]` 日志（token 用量取自 `stream_usage=True` 的 usage_metadata，cache 字段 best-effort）
- `gate_node`：对 AIMessage 的每个 tool_call 调 `permissions.decide()`；有 ASK 时 `interrupt({"reply_id", "calls":[{id,name,query}], "suggestions":[...]})`，resume 后合并裁决；被拒调用记入 `denied_calls`
- `tools_node`：允许的调用并行执行（`asyncio.gather` 保序回填 ToolMessage）；被拒调用合成 `ToolMessage("权限请求被拒绝…")` 让模型继续生成（等价现在的 confirmed=False 续跑）；节点内嵌 `节点[工具调用/结果/异常]` 日志
- 边：START→agent；agent（有 tool_calls→gate，无→END）；gate→tools；tools→agent
- 编译时挂 `MemorySaver`（每请求新建实例，仅服务本轮 interrupt/resume，不跨轮持久——保持"每轮从库重建"的无状态设计）
- 删除 `app/agent_logging.py`（日志职责并入节点，`节点[...]` 格式与 logger 名不变）

### 重写 `app/agent.py`（AgentService，对 chat.py 的公开签名不变）
- 消息装配：`SystemMessage(系统提示词 + 长期记忆块 + 滚动摘要块「【本会话此前对话的滚动摘要（要点记录，细节以最近消息为准）】…」)` + 回放窗口（HumanMessage/AIMessage）+ 多模态 `HumanMessage`（图片转 `image_url` data URI，等价 OpenAIChatFormatter）
- 模型：`ChatOpenAI(base_url, api_key, model, stream_usage=True)`；深度思考切换 `openai_thinking_model`
- 流循环：`graph.astream(inputs, config, stream_mode=["custom","updates"])`——custom 事件直译 SSE dict；updates 里 `__interrupt__` → 走保留的 ConfirmHub 流程（permission_ask → `await asyncio.wait_for(120s)` → permission_resolved → always 则 `confirm_hub.add_session_rules` → `Command(resume={"approved","always"})` 续跑；超时按拒绝），announced 去重集合不再需要（interrupt 恢复不会重跑 agent 节点）
- 演示模式（无密钥）路径原样保留

### 新增 `app/tracing_callback.py`（~120 行）
LangChain `BaseCallbackHandler`：模型/工具调用输出基础 OTel span（模型名、耗时、token、工具名/状态），复用现有 `tracing.py` 的 provider 初始化；未启用时零开销短路。

### 其余改动
- `app/confirm.py`：`PermissionRule` import 换成 `permissions.SessionRule`，其余不动
- `scripts/eval_agent.py`：重写为直接驱动 `AgentService`（现已引用不存在的 `_get_agent`，本就是坏代码），去掉 ConsoleRenderer
- 测试：`test_agent_tools.py` 重写为权限判定矩阵 + 工具执行测试；`test_agent_logging.py` 改写为 `test_graph.py`（假 LLM 流式 → SSE 翻译/interrupt-resume/日志断言）；`test_confirm.py` 换 import；context/memory/workspace/conversations 测试不动
- `README.md` / `backend/ARCHITECTURE.md`：技术栈、权限模型章节更新
- `app/tracing.py` 文档字符串更新（TracingMiddleware → 自研 Callback）

### 明确不动
chat.py（SSE 8 种事件协议、落库、中止保存）、conversations.py、main.py、context.py、summarizer.py、memory.py、rag.py、websearch.py、prompts.py、config.py、models.py、schemas.py、files.py、workspace_picker.py、auth*、db.py、**前端全部**。

## 实施顺序与验证

1. 改 requirements、装依赖、卸载 agentscope（卸载前先从 venv 提取各工具的完整 description/schema 文本）
2. `permissions.py` + `agent_tools.py` + 单测
3. `graph.py` + `agent.py` + `confirm.py` 调整 + 单测（假 LLM 驱动整图，含 interrupt/resume）
4. tracing callback
5. 全量 pytest；启动 postgres + backend + 前端做端到端冒烟：普通对话 / 深度思考流 / 联网搜索 / 医学 RAG chip / 图片多模态 / agent_tools 写文件+Bash+任务清单 / 权限确认卡片（允许、拒绝、超时、总是允许的跨轮重放）
6. 重写 eval_agent.py、更新文档

## 已知取舍（与用户确认过）
- 内置工具为自研简化版：shlex 替代 tree-sitter 命令解析、Read 无 PDF 分页、输出合理截断——模型侧 schema 与描述不变，安全语义（deny/工作区/模式矩阵）一致
- OTel span 为基础版（非 GenAI 完整语义约定），默认关闭
