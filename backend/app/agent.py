"""AgentService：每轮无状态重建一次性 Crew 的对话编排。

各部件按职责拆出——组装在 crew_agents（单 Agent 的 build_single 与多智能体
的 build_team），LLM 构造在 crew_llm，MCP 接入在 crew_mcps，技能加载在
crew_skills，权限引擎在 crew_permissions，内置工具与 ASK 流在 crew_tools，
事件→SSE 翻译桥在 crew_events；本模块只负责按开关组合把部件串成流式回复：

crew 在 asyncio.to_thread 的工作线程里 kickoff，事件（token 增量/思考增量/
工具调用）经全局事件总线 → SSEEventBridge（按 agent_id 路由）→ StreamHub
（线程→协程队列）流回本协程逐条下发；权限确认在执行员工具内阻塞等待
（AskFlow 经同一 StreamHub 推 permission_ask/permission_resolved），SSE 流
全程保活。客户端中止：置 stop 标志（工具入口拒跑）+ 停止消费；crew 无原生
取消，靠模型侧拿到「请求已停止」结果自然收尾。
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from crewai.events.event_bus import crewai_event_bus
from crewai.utilities.string_utils import sanitize_tool_name

from .config import settings
from .confirm import confirm_hub
from .crew_agents import build_single, build_team
from .crew_events import SSEEventBridge, StreamHub
from .crew_logging import install_logging_listener
from .crew_mcps import list_mcp_tools, mcp_tools, probe_mcp_tools
from .crew_permissions import PermissionMode
from .crew_skills import probe_skills
from .crew_tools import AskFlow, apply_session_rules, build_builtin_tools, resolve_workspace
from .files import resolve_stored_image
from .prompts import AGENT_TOOLS_PROMPT, AGENT_TOOLS_PROMPT_HOST

logger = logging.getLogger("friday.agent")

# 进程级安装一次执行段日志监听器（幂等）
install_logging_listener()

# 前端可切换的权限模式（ChatRequest.permission_mode 的取值域）。
# BYPASS（跳过全部安全检查）永不开放。
_PERMISSION_MODES: dict[str, PermissionMode] = {
    "default": PermissionMode.DEFAULT,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "explore": PermissionMode.EXPLORE,
    "dont_ask": PermissionMode.DONT_ASK,
}


def _load_images(attachments: list[str] | None) -> list[tuple[str, str]]:
    """附件文件名 → (base64, media_type) 列表；缺失/非法附件跳过并告警。"""
    images: list[tuple[str, str]] = []
    for name in attachments or []:
        resolved = resolve_stored_image(name)
        if not resolved:
            logger.warning("节点[附件跳过] 附件不存在或名称非法 name=%r", name)
            continue
        path, media_type = resolved
        images.append((base64.b64encode(path.read_bytes()).decode(), media_type))
    return images


def _tool_name_map(crew: Any) -> dict[str, str]:
    """sanitized 工具名 → 原始名：CrewAI 发给模型/事件用的名字会被小写下划线化，
    前端 chip 按原始名（TeamAssign/Bash/mcp__common_tools__web_search）匹配。"""
    mapping: dict[str, str] = {}
    for agent in crew.agents:
        for tool in agent.tools or []:
            mapping[sanitize_tool_name(tool.name)] = tool.name
    return mapping


class AgentService:
    """无状态的对话服务：跨请求不持有任何 Agent 上下文（记忆的唯一事实来源是数据库）。

    每轮请求用「滚动摘要 + 回放窗口」重建一次性 Crew：摘要注入 leader/单
    Agent 的 system 上下文，历史拼进 Task description，本轮消息（含图片）
    由 vision 档 LLM 注入。进程级共享实例带来的跨会话串味、重启失忆、并发
    中止连锁、开关切换丢上下文随之消失。
    """

    def __init__(self) -> None:
        self._ready: bool = settings.model_provider == "deepseek" and bool(settings.llm_api_key)

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
        if not self._ready:
            async for event in self._demo_stream(content, deep_thinking, web_search):
                yield event
            return

        use_thinking = deep_thinking and bool(settings.llm_thinking_model)
        # 医学检索（medical_rag_search）已迁至 mcp-server：URL 配置即挂载；
        # web_search 仍按请求开关拼进白名单，URL 未配置时降级为提示词引导
        # （服务端临时不可达按降级处理，不影响对话）
        use_mcp = bool(settings.mcp_server_url)
        use_search = web_search and use_mcp
        # 内置工具受服务端总开关约束（安全边界见 crew_tools.py 模块说明）
        use_agent_tools = agent_tools and settings.agent_tools_enabled
        # 多智能体：请求与服务端开关全开、非带图（视觉档只有单 Agent）且至少
        # 有一名成员可组建（检索员随 MCP、执行员随 agent_tools）。关闭时走
        # 单 Agent 路径，行为与单 Agent 模式完全一致。
        use_team = (
            multi_agent and settings.multi_agent_enabled
            and not attachments and (use_mcp or use_agent_tools)
        )
        images = _load_images(attachments) if attachments else []
        if images:
            # 默认档 v4-pro 无视觉（API 把图片静默换成 [Unsupported Image] 占位文本），
            # 带图请求整轮切多模态档；思考开关随之失效，下方自动退化为提示词引导
            model = settings.llm_vision_model
            use_thinking = False
        else:
            model = settings.llm_thinking_model if use_thinking else settings.llm_model
        summary_text = (
            f"【本会话此前对话的滚动摘要（要点记录，细节以最近消息为准）】\n{summary}"
            if summary else ""
        )

        hub = StreamHub()
        hub.bind()
        # 权限确认归属：团队模式 ASK 只会来自执行员的工具，单 Agent 模式是 Friday
        ask = AskFlow(
            conversation_id=conversation_id,
            user_id=user_id or UUID(int=0),
            agent_name="执行员" if use_team else "Friday",
            emit=hub.emit,
        )
        builtin_tools = None
        agent_tools_prompt = ""
        if use_agent_tools:
            # 会话自选目录优先（用户本机目录，落库时已校验存在）；默认会话隔离目录需创建
            workspace = resolve_workspace(conversation_id, workspace_root)
            if not workspace_root:
                workspace.mkdir(parents=True, exist_ok=True)
            mode = _PERMISSION_MODES.get(permission_mode, PermissionMode.DEFAULT)
            builtin_tools, permission_context = build_builtin_tools(
                workspace, mode, ask=ask, stop=hub.stop
            )
            # 会话级「总是允许」规则重放（前端确认时勾选 always 落进来的）
            apply_session_rules(permission_context, confirm_hub.session_rules(conversation_id))
            # 自选目录没有工作区下载链路，用 HOST 版提示词（告知本地路径）
            template = AGENT_TOOLS_PROMPT_HOST if workspace_root else AGENT_TOOLS_PROMPT
            agent_tools_prompt = template.format(
                workspace=workspace,
                # 产出文件下载走鉴权路由（前端会拦截该前缀链接做带 token 下载）
                public_prefix=f"/api/workspaces/{conversation_id}",
            )
        # MCP 工具探测与注册日志见 crew_mcps（连接失败仅告警，本轮降级为无检索工具）
        mcp_tool_list = []
        if use_mcp:
            enable = ["medical_rag_search"] + (["web_search"] if web_search else [])
            try:
                specs = await list_mcp_tools(enable)
                probe_mcp_tools(specs)
                mcp_tool_list = mcp_tools(specs)
            except Exception as exc:  # noqa: BLE001 - 服务端不可达降级继续对话
                logger.warning("节点[MCP注册失败] conversation=%s error=%s", conversation_id, exc)
        # 技能注册探测与日志见 crew_skills.probe_skills
        probe_skills()

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
        if use_team:
            crew, agent_ids = build_team(
                model, use_thinking, replay, user_content,
                summary=summary_text, memory_block=memory_block, web_search=use_search,
                agent_tools_prompt=agent_tools_prompt,
                builtin_tools=builtin_tools, mcp_tool_list=mcp_tool_list,
            )
        else:
            crew, agent_ids = build_single(
                model, use_thinking, replay, user_content,
                summary=summary_text, memory_block=memory_block, web_search=use_search,
                agent_tools_prompt=agent_tools_prompt,
                builtin_tools=builtin_tools, mcp_tool_list=mcp_tool_list,
                images=images or None,
            )
        bridge = SSEEventBridge(hub, agent_ids, _tool_name_map(crew))

        # crew 在工作线程执行；工具/调用类事件经总线线程池异步分发，
        # kickoff 结束后先 flush 再放哨兵，保证尾部 chip 不丢
        failure: list[BaseException] = []

        def _kickoff_then_flush() -> None:
            crew.kickoff()
            crewai_event_bus.flush(timeout=10.0)

        async def _run() -> None:
            try:
                await asyncio.to_thread(_kickoff_then_flush)
            except BaseException as exc:  # noqa: BLE001 - 转交消费侧统一处理
                failure.append(exc)
            finally:
                hub.close()

        task = asyncio.create_task(_run())
        try:
            async for event in hub.events():
                yield event
        finally:
            # 中止（CancelledError/GeneratorExit）或消费结束：停工具、注销桥、
            # 不再等待后台任务（to_thread 无法强杀，靠 stop 标志自然收尾）
            hub.stop.set()
            bridge.close()
            if not task.done():
                task.cancel()
        if failure:
            raise failure[0]
        return

    async def _demo_stream(self, content: str, deep_thinking: bool, web_search: bool) -> AsyncIterator[dict[str, str]]:
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
            "配置 LLM_API_KEY 并将 MODEL_PROVIDER 设置为 deepseek 后，会通过 CrewAI 调用 DeepSeek。"
        )


agent_service = AgentService()
