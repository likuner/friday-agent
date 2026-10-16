"""Agent 组装：把凭证、提示词、中间件、模型、工具/技能装配成一次性 Agent。

每轮请求由 agent.py 的 AgentService.stream() 调用，无跨请求共享状态。
多智能体模式经 build_team() 组装 TeamPipeline（Friday 作 leader + 检索员/
执行员成员），成员按开关裁剪；Agent 与 TeamPipeline 都实现 reply_stream
协议，stream() 的事件循环对两者通用。
"""

from agentscope.agent import Agent, InjectionConfig
from agentscope.credential import DeepSeekCredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.mcp import MCPClient
from agentscope.middleware import MiddlewareBase, TracingMiddleware
from agentscope.model import DeepSeekChatModel
from agentscope.permission import PermissionContext
from agentscope.pipeline import TeamMember, TeamPipeline
from agentscope.state import AgentState
from agentscope.tool import ToolBase, Toolkit

from .agent_events import AgentTagMiddleware
from .agent_logging import AgentLoggingMiddleware
from .agent_skills import SKILL_LOADER
from .config import settings
from .prompts import (
    EXECUTOR_DESC,
    EXECUTOR_PROMPT,
    LEADER_TEAM_PROMPT,
    RESEARCHER_DESC,
    RESEARCHER_PROMPT,
    SYSTEM_PROMPT,
    WEB_SEARCH_PROMPT,
)


def _assemble_agent(
    *,
    name: str,
    system_prompt: str,
    credential: DeepSeekCredential,
    model: str,
    state: AgentState,
    thinking: bool = False,
    tools: list[ToolBase] | None = None,
    mcps: list[MCPClient] | None = None,
    skills: bool = False,
    extra_middlewares: list[MiddlewareBase] | None = None,
) -> Agent:
    """单 Agent 装配：build_agent（单 Agent 模式）与 build_team（leader/成员）共用。

    每个 Agent 用独立 Toolkit：共享实例可能被 Agent 内部改写。
    MCP 检索工具（medical_rag_search/web_search）服务端已声明只读自动放行；
    内置工具（Bash/文件/任务）的权限由 AgentState 的 permission_context 裁决。
    """
    middlewares: list[MiddlewareBase] = [TracingMiddleware(), AgentLoggingMiddleware()]
    if extra_middlewares:
        middlewares.extend(extra_middlewares)
    return Agent(
        name=name,
        system_prompt=system_prompt,
        # Tracing 未配置时自动短路；日志中间件产出执行段节点日志（模型/工具/耗时/token）
        middlewares=middlewares,
        # 框架默认以 UTC 注入 <current-time>，模型会先答 UTC 再自行换算；
        # 改成东八区后直接报告北京时间
        injection_config=InjectionConfig(timezone="Asia/Shanghai"),
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
        toolkit=Toolkit(
            tools=tools or [],
            mcps=mcps,
            skills_or_loaders=[SKILL_LOADER] if skills else None,
        ),
    )


def build_agent(
    credential: DeepSeekCredential,
    model: str,
    state: AgentState,
    thinking: bool = False,
    web_search: bool = False,
    memory_block: str | None = None,
    builtin_tools: list[ToolBase] | None = None,
    agent_tools_prompt: str = "",
    mcps: list[MCPClient] | None = None,
) -> Agent:
    """单 Agent 模式：组装全能力的 Friday（现有行为，多智能体开关关闭时走此路径）。"""
    system_prompt = (
        SYSTEM_PROMPT
        + (f"\n\n{memory_block}" if memory_block else "")
        + (f"\n\n{WEB_SEARCH_PROMPT}" if web_search else "")
        + (f"\n\n{agent_tools_prompt}" if agent_tools_prompt else "")
    )
    return _assemble_agent(
        name="Friday",
        # 长期记忆块（用户画像/事实条目，自带标注头）插在人设指令之后；
        # 其标注已声明"与最近对话冲突时以最近对话为准"，与会话上下文指令衔接
        system_prompt=system_prompt,
        credential=credential,
        model=model,
        state=state,
        thinking=thinking,
        tools=builtin_tools,
        mcps=mcps,
        skills=True,
    )


def build_team(
    credential: DeepSeekCredential,
    model: str,
    session_id: str,
    summary: str = "",
    thinking: bool = False,
    web_search: bool = False,
    memory_block: str | None = None,
    builtin_tools: list[ToolBase] | None = None,
    permission_context: PermissionContext | None = None,
    agent_tools_prompt: str = "",
    mcps: list[MCPClient] | None = None,
) -> tuple[TeamPipeline, dict[str, str]]:
    """多智能体模式：Friday 作 leader（规划派活+汇总作答），成员按开关裁剪。

    成员规则（成员名即 leader 的 TeamAssign 取值，须唯一）：
    - 检索员：传入 mcps（服务端可用）即组建；web_search 只控制是否多挂联网
      搜索工具，不影响成员存在（与单 Agent 模式的 MCP 白名单行为一致）；
    - 执行员：传入 builtin_tools/permission_context（agent_tools 开启）才组建；
    - 两者皆无时调用方不应走到这里（use_team 已含该条件）。

    leader/成员各自独立 AgentState 与 Toolkit；历史回放由调用方 observe 到
    leader（成员每轮全新，TeamPipeline reset_members 默认即如此）。成员用
    基础档模型（settings.llm_model），深度思考档只给 leader。

    返回 (TeamPipeline, reply_id → agent 名注册表)：注册表由各 Agent 上的
    AgentTagMiddleware 在事件产生现场填充，供 ReplyEventTranslator 给 SSE
    事件加 agent 字段（单 Agent 模式无此注册表，事件恒归属 Friday）。
    """
    names: dict[str, str] = {}
    tagger = AgentTagMiddleware(names)
    members: list[TeamMember] = []

    if mcps:
        researcher = _assemble_agent(
            name="检索员",
            system_prompt=RESEARCHER_PROMPT + (f"\n\n{WEB_SEARCH_PROMPT}" if web_search else ""),
            credential=credential,
            model=settings.llm_model,
            state=AgentState(session_id=session_id),
            mcps=mcps,
            skills=True,
            extra_middlewares=[tagger],
        )
        members.append(TeamMember(agent=researcher, description=RESEARCHER_DESC))

    if builtin_tools and permission_context:
        executor = _assemble_agent(
            name="执行员",
            system_prompt=EXECUTOR_PROMPT + (f"\n\n{agent_tools_prompt}" if agent_tools_prompt else ""),
            credential=credential,
            model=settings.llm_model,
            state=AgentState(session_id=session_id, permission_context=permission_context),
            tools=builtin_tools,
            extra_middlewares=[tagger],
        )
        members.append(TeamMember(agent=executor, description=EXECUTOR_DESC))

    member_lines = "\n".join(f"- {member.agent.name}：{member.description}" for member in members)
    leader = _assemble_agent(
        name="Friday",
        system_prompt=(
            SYSTEM_PROMPT
            + (f"\n\n{memory_block}" if memory_block else "")
            + "\n\n"
            + LEADER_TEAM_PROMPT.format(members=member_lines)
        ),
        credential=credential,
        model=model,
        state=AgentState(session_id=session_id, summary=summary),
        thinking=thinking,
        extra_middlewares=[tagger],
    )
    return TeamPipeline(leader, members), names
