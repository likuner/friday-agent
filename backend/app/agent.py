"""AgentService：每轮无状态重建一次性 Agent 的对话编排。

各部件已按职责拆出——组装在 agent_factory（单 Agent 的 build_agent 与
多智能体的 build_team），MCP 挂载在 agent_mcps，技能加载在 agent_skills，
输入消息构造在 agent_messages，事件→SSE 翻译在 agent_events；本模块只负责
按开关组合把部件串成流式回复（含权限确认 park/resume 与演示降级）。
多智能体模式下串的是 TeamPipeline，事件循环与权限链路对两者通用。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from agentscope.credential import DeepSeekCredential
from agentscope.event import ConfirmResult, EventType, RequireUserConfirmEvent, UserConfirmResultEvent
from agentscope.permission import PermissionMode
from agentscope.state import AgentState

from .agent_events import ReplyEventTranslator, tool_query
from .agent_factory import build_agent, build_team
from .agent_mcps import mcp_client, probe_mcp_tools
from .agent_messages import replay_msgs, user_message
from .agent_skills import probe_skills
from .agent_tools import apply_session_rules, build_builtin_tools, resolve_workspace
from .config import settings
from .confirm import AskAnswer, PendingAsk, confirm_hub
from .prompts import AGENT_TOOLS_PROMPT, AGENT_TOOLS_PROMPT_HOST

logger = logging.getLogger("friday.agent")

# 前端可切换的权限模式（ChatRequest.permission_mode 的取值域）。
# BYPASS（跳过全部安全检查）永不开放。
_PERMISSION_MODES: dict[str, PermissionMode] = {
    "default": PermissionMode.DEFAULT,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "explore": PermissionMode.EXPLORE,
    "dont_ask": PermissionMode.DONT_ASK,
}


class AgentService:
    """无状态的对话服务：跨请求不持有任何 Agent 上下文（记忆的唯一事实来源是数据库）。

    每轮请求用「滚动摘要 + 回放窗口」重建一次性 Agent：
    摘要注入 AgentState.summary（框架自动前置到模型上下文），历史经 observe 回放，
    本轮消息（含图片附件）作为 reply_stream 的输入。进程级共享实例带来的
    跨会话串味、重启失忆、并发中止连锁、开关切换丢上下文随之消失。
    """

    def __init__(self) -> None:
        self._credential: DeepSeekCredential | None = None
        if settings.model_provider == "deepseek" and settings.llm_api_key:
            self._credential = DeepSeekCredential(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
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
        multi_agent: bool = False,
    ) -> AsyncIterator[dict[str, str]]:
        use_thinking = deep_thinking and bool(settings.llm_thinking_model)
        # 医学检索（medical_rag_search）已迁至 mcp-server：URL 配置即挂载；
        # web_search 仍按请求开关拼进白名单，URL 未配置时降级为提示词引导
        # （服务端临时不可达由 Toolkit 列工具时静默跳过，不影响对话）
        use_mcp = bool(settings.mcp_server_url)
        use_search = web_search and use_mcp
        # 内置工具受服务端总开关约束（安全边界见 agent_tools.py 模块说明）
        use_agent_tools = agent_tools and settings.agent_tools_enabled
        # 多智能体：请求与服务端开关全开、非带图（视觉档只有单 Agent）且至少
        # 有一名成员可组建（检索员随 MCP、执行员随 agent_tools）。关闭时走
        # 下方单 Agent 老路，行为与单 Agent 模式完全一致。
        use_team = (
            multi_agent and settings.multi_agent_enabled
            and not attachments and (use_mcp or use_agent_tools)
        )
        builtin_tools = None
        agent_tools_prompt = ""
        if self._credential is not None:
            if attachments:
                # 默认档 v4-pro 无视觉（API 把图片静默换成 [Unsupported Image] 占位文本），
                # 带图请求整轮切多模态档；思考开关随之失效，下方自动退化为提示词引导
                model = settings.llm_vision_model
                use_thinking = False
            else:
                model = settings.llm_thinking_model if use_thinking else settings.llm_model
            state_kwargs: dict[str, object] = {
                "session_id": str(conversation_id),
                "summary": f"【本会话此前对话的滚动摘要（要点记录，细节以最近消息为准）】\n{summary}" if summary else "",
            }
            permission_context = None
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
                # 自选目录没有工作区下载链路，用 HOST 版提示词（告知本地路径）
                template = AGENT_TOOLS_PROMPT_HOST if workspace_root else AGENT_TOOLS_PROMPT
                agent_tools_prompt = template.format(
                    workspace=workspace,
                    # 产出文件下载走鉴权路由（前端会拦截该前缀链接做带 token 下载）
                    public_prefix=f"/api/workspaces/{conversation_id}",
                )
            # MCP 工具探测与注册日志见 agent_mcps.probe_mcp_tools（失败仅告警，client 照常挂载）
            mcps = None
            if use_mcp:
                client = mcp_client(["medical_rag_search"] + (["web_search"] if web_search else []))
                await probe_mcp_tools(client)
                mcps = [client]
            # 技能注册探测与日志见 agent_skills.probe_skills
            await probe_skills()
            # 每轮按会话重建无状态上下文：摘要经 AgentState.summary 由框架自动前置注入。
            # 多智能体模式返回 TeamPipeline（Agent 与其都实现 reply_stream，下方
            # 事件循环与权限 park/resume 通用）+ reply_id→agent 名注册表（给 SSE
            # 事件标归属）；单 Agent 模式注册表为空，事件恒归属 Friday。
            agent_names: dict[str, str] = {}
            if use_team:
                agent, agent_names = build_team(
                    self._credential, model,
                    session_id=str(conversation_id),
                    summary=str(state_kwargs["summary"]),
                    thinking=use_thinking, web_search=use_search, memory_block=memory_block,
                    builtin_tools=builtin_tools, permission_context=permission_context,
                    agent_tools_prompt=agent_tools_prompt, mcps=mcps,
                )
                replay_target = agent.leader
            else:
                state = AgentState(**state_kwargs)  # type: ignore[arg-type]
                agent = build_agent(
                    self._credential, model, state,
                    thinking=use_thinking, web_search=use_search, memory_block=memory_block,
                    builtin_tools=builtin_tools, agent_tools_prompt=agent_tools_prompt,
                    mcps=mcps,
                )
                replay_target = agent
            user_content = content
            # 有 reasoning 模型时由模型真正产出思考过程；没有才退化成提示词引导
            if deep_thinking and not use_thinking:
                user_content = f"请深度思考后回答。\n\n{user_content}"
            if web_search and not use_search:
                user_content = f"请结合联网检索能力回答；如果无法访问网络，请明确说明。\n\n{user_content}"
            logger.info(
                "节点[Agent调用] conversation=%s replay=%s条(摘要=%s) model=%s thinking=%s search=%s agent_tools=%s perm=%s multi_agent=%s prompt=%r",
                conversation_id, len(replay), bool(summary), model, deep_thinking, web_search, use_agent_tools,
                permission_mode if use_agent_tools else "-", use_team, user_content[:100],
            )
            # 历史先回放（纯文本），本轮消息（含图片）再作为输入：附件预算只留给本轮。
            # 团队模式只回放给 leader——成员每轮全新（reset_members），看到历史反而串味
            if replay:
                await replay_target.observe(replay_msgs(replay))
            translator = ReplyEventTranslator(agent_names)
            inputs: object = user_message(user_content, attachments)
            while True:
                parked: RequireUserConfirmEvent | None = None
                async for event in agent.reply_stream(inputs):
                    if event.type == EventType.REQUIRE_USER_CONFIRM:
                        # 权限 ASK：本轮 reply 到此自然结束（parked 状态留在 agent.state），
                        # 推给前端确认后以 UserConfirmResultEvent 续跑同一个 Agent
                        parked = event
                        continue
                    sse_event = translator.feed(event)
                    if sse_event:
                        yield sse_event
                if parked is None:
                    break
                # ---- 权限确认：推给前端 → 等确认接口应答（超时自动拒绝）→ 续跑 ----
                pending = PendingAsk(
                    conversation_id=conversation_id,
                    user_id=user_id or UUID(int=0),
                    reply_id=parked.reply_id,
                    calls=[
                        {"id": call.id, "name": call.name, "query": tool_query(call.input)}
                        for call in parked.tool_calls
                    ],
                )
                confirm_hub.register(pending)
                logger.info(
                    "节点[权限确认] conversation=%s reply=%s calls=%s",
                    conversation_id, parked.reply_id, [c["name"] for c in pending.calls],
                )
                yield {
                    "type": "permission_ask",
                    "reply_id": parked.reply_id,
                    "calls": pending.calls,
                    # 归属 agent（单 Agent 模式恒为 Friday；团队模式解析出执行员等）
                    "agent": translator.agent_of(parked.reply_id),
                }
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
            # 执行段节点日志（模型/工具/耗时/token）由 AgentLoggingMiddleware 记，
            # 事件→SSE 翻译在 ReplyEventTranslator，此处只做编排
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
            "配置 LLM_API_KEY 并将 MODEL_PROVIDER 设置为 deepseek 后，会通过 AgentScope 2.0.8 调用 DeepSeek。"
        )


agent_service = AgentService()
