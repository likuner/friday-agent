from collections.abc import AsyncIterator
import asyncio
import base64
import json
import logging
from uuid import UUID

from agentscope.agent import Agent
from agentscope.credential import DeepSeekCredential
from agentscope.event import ConfirmResult, EventType, RequireUserConfirmEvent, UserConfirmResultEvent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import AssistantMsg, Base64Source, DataBlock, Msg, TextBlock, UserMsg
from agentscope.middleware import TracingMiddleware
from agentscope.model import DeepSeekChatModel
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolBase, Toolkit

from .agent_logging import AgentLoggingMiddleware
from .agent_tools import apply_session_rules, build_builtin_tools, resolve_workspace
from .config import settings
from .confirm import AskAnswer, PendingAsk, confirm_hub
from .files import resolve_stored_image
from .prompts import AGENT_TOOLS_PROMPT, AGENT_TOOLS_PROMPT_HOST, SYSTEM_PROMPT, WEB_SEARCH_PROMPT
from .rag import medical_rag_search
# 别名避免与 _build/stream 的 web_search 布尔参数互相遮蔽
from .websearch import web_search as _web_search_tool
from .websearch import web_search_available

logger = logging.getLogger("friday.agent")

# 前端可切换的权限模式（ChatRequest.permission_mode 的取值域）。
# BYPASS（跳过全部安全检查）永不开放。
_PERMISSION_MODES: dict[str, PermissionMode] = {
    "default": PermissionMode.DEFAULT,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "explore": PermissionMode.EXPLORE,
    "dont_ask": PermissionMode.DONT_ASK,
}


def _user_message(content: str, attachments: list[str] | None) -> UserMsg:
    """有图片附件时构造多模态消息：图片读成 base64 直接传给模型。

    DeepSeek 的 OpenAI 兼容接口接受 image_url 形式的 data URI，
    AgentScope 侧由 OpenAIChatFormatter 负责把 DataBlock 转成该格式。
    """
    blocks: list[TextBlock | DataBlock] = []
    for name in attachments or []:
        resolved = resolve_stored_image(name)
        if not resolved:
            logger.warning("节点[附件跳过] 附件不存在或名称非法 name=%r", name)
            continue
        path, media_type = resolved
        blocks.append(
            DataBlock(
                type="data",
                source=Base64Source(
                    type="base64",
                    data=base64.b64encode(path.read_bytes()).decode(),
                    media_type=media_type,
                ),
                name=path.name,
            )
        )
    if not blocks:
        return UserMsg(name="user", content=content)
    # 只发图不打字时给模型一个默认指令
    blocks.insert(0, TextBlock(type="text", text=content or "请描述这张图片。"))
    logger.info("节点[多模态消息] images=%s text=%r", len(blocks) - 1, (content or "")[:60])
    return UserMsg(name="user", content=blocks)


# chip 摘要的参数键优先级：检索词 → 命令 → 文件路径 → 搜索模式 → 目录 → 任务标题
_TOOL_QUERY_KEYS = ("query", "command", "file_path", "pattern", "path", "subject")


def _tool_query(raw_args: str) -> str:
    """从工具调用参数 JSON 里取出展示摘要（检索词/命令/文件路径等），供前端 chip。"""
    try:
        payload = json.loads(raw_args) if raw_args else {}
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in _TOOL_QUERY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return ""


def _replay_msgs(replay: list[dict[str, str]]) -> list[Msg]:
    """把库中的回放窗口转成 AgentScope 消息：仅 user/assistant 纯文本（已由 context.py 清洗）。"""
    return [
        UserMsg(name="user", content=item["content"])
        if item["role"] == "user"
        else AssistantMsg(name="Friday", content=item["content"])
        for item in replay
    ]


class AgentService:
    """无状态的对话服务：跨请求不持有任何 Agent 上下文（记忆的唯一事实来源是数据库）。

    每轮请求用「滚动摘要 + 回放窗口」重建一次性 Agent：
    摘要注入 AgentState.summary（框架自动前置到模型上下文），历史经 observe 回放，
    本轮消息（含图片附件）作为 reply_stream 的输入。进程级共享实例带来的
    跨会话串味、重启失忆、并发中止连锁、开关切换丢上下文随之消失。
    """

    def __init__(self) -> None:
        self._credential: DeepSeekCredential | None = None
        if settings.model_provider == "deepseek" and settings.openai_api_key:
            self._credential = DeepSeekCredential(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )

    @staticmethod
    def _build(
        credential: DeepSeekCredential,
        model: str,
        state: AgentState,
        thinking: bool = False,
        web_search: bool = False,
        memory_block: str | None = None,
        builtin_tools: list[ToolBase] | None = None,
        agent_tools_prompt: str = "",
    ) -> Agent:
        # 每个 Agent 用独立 Toolkit：共享实例可能被 Agent 内部改写
        tools = [
            FunctionTool(
                medical_rag_search,
                name="medical_rag_search",
                # 只读检索，允许模型自主执行，避免权限引擎挂起等待人工确认
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="只读文献检索，自动允许",
                ),
            ),
        ]
        if web_search:
            tools.append(
                FunctionTool(
                    _web_search_tool,
                    name="web_search",
                    permission=PermissionDecision(
                        behavior=PermissionBehavior.ALLOW,
                        message="只读联网检索，自动允许",
                    ),
                ),
            )
        # 内置工具（Bash/文件/任务）：与 FunctionTool 混排注册，权限由 AgentState 的
        # DONT_ASK 上下文裁决（工作区内写入放行、敏感路径 deny、其余拒绝）
        if builtin_tools:
            tools.extend(builtin_tools)
        return Agent(
            name="Friday",
            # 长期记忆块（用户画像/事实条目，自带标注头）插在人设指令之后；
            # 其标注已声明"与最近对话冲突时以最近对话为准"，与会话上下文指令衔接
            system_prompt=SYSTEM_PROMPT
            + (f"\n\n{memory_block}" if memory_block else "")
            + (f"\n\n{WEB_SEARCH_PROMPT}" if web_search else "")
            + (f"\n\n{agent_tools_prompt}" if agent_tools_prompt else ""),
            # Tracing 未配置时自动短路；日志中间件产出执行段节点日志（模型/工具/耗时/token）
            middlewares=[TracingMiddleware(), AgentLoggingMiddleware()],
            # 每轮一次性状态：session_id 绑定会话，summary 承载滚动摘要
            state=state,
            model=DeepSeekChatModel(
                credential=credential,
                model=model,
                parameters=DeepSeekChatModel.Parameters(thinking_enable=True) if thinking else None,
                stream=True,
                # DeepSeekChatFormatter 会跳过 DataBlock（图片），换成 OpenAI 兼容 formatter；
                # DeepSeek 接口本身兼容，纯文本/工具调用路径实测同样正常。
                formatter=OpenAIChatFormatter(),
            ),
            toolkit=Toolkit(tools=tools),
        )

    async def stream(
        self,
        conversation_id: UUID,
        replay: list[dict[str, str]],
        summary: str | None,
        content: str,
        deep_thinking: bool = False,
        web_search: bool = False,
        attachments: list[str] | None = None,
        memory_block: str | None = None,
        agent_tools: bool = False,
        permission_mode: str = "default",
        workspace_root: str | None = None,
        user_id: UUID | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        use_thinking = deep_thinking and bool(settings.openai_thinking_model)
        # 联网搜索需要智谱密钥（GLM 内置 web_search 执行）；未配置时降级为提示词引导
        use_search = web_search and web_search_available()
        # 内置工具受服务端总开关约束（安全边界见 agent_tools.py 模块说明）
        use_agent_tools = agent_tools and settings.agent_tools_enabled
        builtin_tools = None
        agent_tools_prompt = ""
        if self._credential is not None:
            model = settings.openai_thinking_model if use_thinking else settings.openai_model
            state_kwargs: dict[str, object] = {
                "session_id": str(conversation_id),
                "summary": f"【本会话此前对话的滚动摘要（要点记录，细节以最近消息为准）】\n{summary}" if summary else "",
            }
            if use_agent_tools:
                # 会话自选目录优先（用户本机目录，落库时已校验存在）；默认会话隔离目录需创建
                workspace = resolve_workspace(conversation_id, workspace_root)
                if not workspace_root:
                    workspace.mkdir(parents=True, exist_ok=True)
                mode = _PERMISSION_MODES.get(permission_mode, PermissionMode.DEFAULT)
                builtin_tools, permission_context = build_builtin_tools(workspace, mode)
                # 会话级「总是允许」规则重放（前端确认时勾选 always 落进来的）
                apply_session_rules(permission_context, confirm_hub.session_rules(conversation_id))
                state_kwargs["permission_context"] = permission_context
                # 自选目录没有 /workspaces 静态下载链路，用 HOST 版提示词（告知本地路径）
                template = AGENT_TOOLS_PROMPT_HOST if workspace_root else AGENT_TOOLS_PROMPT
                agent_tools_prompt = template.format(
                    workspace=workspace,
                    public_prefix=f"/workspaces/{conversation_id}",
                )
            # 每轮按会话重建无状态上下文：摘要经 AgentState.summary 由框架自动前置注入
            state = AgentState(**state_kwargs)  # type: ignore[arg-type]
            agent = self._build(
                self._credential, model, state,
                thinking=use_thinking, web_search=use_search, memory_block=memory_block,
                builtin_tools=builtin_tools, agent_tools_prompt=agent_tools_prompt,
            )
            user_content = content
            # 有 reasoning 模型时由模型真正产出思考过程；没有才退化成提示词引导
            if deep_thinking and not use_thinking:
                user_content = f"请深度思考后回答。\n\n{user_content}"
            if web_search and not use_search:
                user_content = f"请结合联网检索能力回答；如果无法访问网络，请明确说明。\n\n{user_content}"
            logger.info(
                "节点[Agent调用] conversation=%s replay=%s条(摘要=%s) model=%s thinking=%s search=%s agent_tools=%s perm=%s prompt=%r",
                conversation_id, len(replay), bool(summary), model, deep_thinking, web_search, use_agent_tools,
                permission_mode if use_agent_tools else "-", user_content[:100],
            )
            # 历史先回放（纯文本），本轮消息（含图片）再作为输入：附件预算只留给本轮
            if replay:
                await agent.observe(_replay_msgs(replay))
            tool_args: dict[str, str] = {}
            tool_names: dict[str, str] = {}
            # 已下发过 chip 的工具调用（确认续跑后不重复发）
            announced: set[str] = set()
            inputs: object = _user_message(user_content, attachments)
            while True:
                parked: RequireUserConfirmEvent | None = None
                async for event in agent.reply_stream(inputs):
                    if event.type == EventType.REQUIRE_USER_CONFIRM:
                        # 权限 ASK：本轮 reply 到此自然结束（parked 状态留在 agent.state），
                        # 推给前端确认后以 UserConfirmResultEvent 续跑同一个 Agent
                        parked = event
                        continue
                    if event.type == EventType.TEXT_BLOCK_DELTA:
                        yield {"type": "text", "content": event.delta}
                    elif event.type == EventType.THINKING_BLOCK_DELTA:
                        yield {"type": "thinking", "content": event.delta}
                    elif event.type == EventType.TOOL_CALL_START:
                        tool_args[event.tool_call_id] = ""
                        tool_names[event.tool_call_id] = event.tool_call_name
                    elif event.type == EventType.TOOL_CALL_DELTA:
                        tool_args[event.tool_call_id] = tool_args.get(event.tool_call_id, "") + event.delta
                    elif event.type == EventType.TOOL_CALL_END:
                        if event.tool_call_id in announced:
                            continue
                        announced.add(event.tool_call_id)
                        raw_args = tool_args.get(event.tool_call_id, "")
                        # 参数到 END 才收全：把工具名和检索词一起下发，前端 chip 显示检索词，
                        # 多次并行检索（多角度）就不会看起来像重复的同一个 chip。
                        yield {
                            "type": "tool_call",
                            "name": tool_names.get(event.tool_call_id, ""),
                            "query": _tool_query(raw_args),
                        }
                if parked is None:
                    break
                # ---- 权限确认：推给前端 → 等确认接口应答（超时自动拒绝）→ 续跑 ----
                pending = PendingAsk(
                    conversation_id=conversation_id,
                    user_id=user_id or UUID(int=0),
                    reply_id=parked.reply_id,
                    calls=[
                        {"id": call.id, "name": call.name, "query": _tool_query(call.input)}
                        for call in parked.tool_calls
                    ],
                )
                confirm_hub.register(pending)
                logger.info(
                    "节点[权限确认] conversation=%s reply=%s calls=%s",
                    conversation_id, parked.reply_id, [c["name"] for c in pending.calls],
                )
                yield {"type": "permission_ask", "reply_id": parked.reply_id, "calls": pending.calls}
                timed_out = False
                try:
                    await asyncio.wait_for(
                        pending.event.wait(), timeout=settings.permission_confirm_timeout_seconds,
                    )
                except TimeoutError:
                    timed_out = True  # 无人应答：超时按拒绝续跑，流不会悬挂
                finally:
                    confirm_hub.cancel(conversation_id)
                answer = pending.answer or AskAnswer(approved=False)
                yield {"type": "permission_resolved", "approved": answer.approved, "timed_out": timed_out}
                # 「总是允许」：把建议规则存为会话级规则（后续轮次自动重放），
                # 并随确认事件喂给引擎（本 reply 内立即生效）
                rules = None
                if answer.approved and answer.always:
                    suggested = [rule for call in parked.tool_calls for rule in (call.suggested_rules or [])]
                    if suggested:
                        confirm_hub.add_session_rules(conversation_id, suggested)
                        rules = suggested
                inputs = UserConfirmResultEvent(
                    reply_id=parked.reply_id,
                    confirm_results=[
                        ConfirmResult(confirmed=answer.approved, tool_call=call, rules=rules)
                        for call in parked.tool_calls
                    ],
                )
            # 执行段节点日志（模型调用/工具调用·参数·结果/耗时/token）由
            # AgentLoggingMiddleware 在中间件钩子里记，此处只做事件 → SSE 翻译
            return

        logger.warning("节点[演示模式] 未配置模型密钥，返回本地演示流式回复 prompt=%r", content[:100])

        if deep_thinking:
            yield {"type": "thinking", "content": "正在梳理问题的上下文、约束条件和可执行步骤。"}
        if web_search:
            yield {"type": "search", "content": "已准备好联网搜索结果摘要。"}
        answer = self._demo_answer(content)
        for index in range(0, len(answer), 4):
            await asyncio.sleep(0.025)
            yield {"type": "text", "content": answer[index : index + 4]}

    @staticmethod
    def _demo_answer(prompt: str) -> str:
        return (
            "收到，我先把这个问题拆成可执行的步骤。\n\n"
            f"你当前的问题是：{prompt}\n\n"
            "建议先明确目标、输入和验收标准，再按最小闭环实现。"
            "如果这是工程问题，可以从数据结构、错误处理、性能边界和测试用例四个方面逐项确认。\n\n"
            "当前服务未配置外部模型密钥，因此这里返回的是本地演示流式回复。"
            "配置 OPENAI_API_KEY 并将 MODEL_PROVIDER 设置为 deepseek 后，会通过 AgentScope 2.0.8 调用 DeepSeek。"
        )


agent_service = AgentService()
