"""CrewAgent 组装：单 Agent 与多智能体（leader + TeamAssign 成员）两条路径。

对应原 agent_factory.py 的 build_agent / build_team：
- 单 Agent 模式：Friday 挂全部工具（内置 + MCP + Skill），一个 Crew 一个 Task；
- 多智能体模式：Friday 作 leader，只拿 TeamAssign 工具；检索员（MCP+Skill）、
  执行员（内置工具+权限门控）按开关组建。TeamAssign 内部为成员建 Task 并
  跑独立 Crew，结果作为工具结果回流 leader 汇总（同轮多次委派顺序执行）。
  成员每轮全新、看不到对话历史（委派描述须自包含），历史只进 leader 的
  Task description 与 system 上下文。

角色名即事件归属展示名（Friday / 检索员 / 执行员），SSEEventBridge 按
agent_id 路由、按 role 标归属。当前北京时间手动注入 system 内容（替代原
框架 InjectionConfig）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from crewai import Agent, Crew, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from .config import settings
from .crew_llm import build_llm
from .crew_mcps import MCPTool
from .crew_skills import skill_instructions, skill_tool
from .crew_tools import GatedTool
from .prompts import (
    AGENT_TOOLS_PROMPT,
    EXECUTOR_DESC,
    EXECUTOR_PROMPT,
    LEADER_TEAM_PROMPT,
    RESEARCHER_DESC,
    RESEARCHER_PROMPT,
    SYSTEM_PROMPT,
    WEB_SEARCH_PROMPT,
)

logger = logging.getLogger("friday.crew_agents")

_BEIJING = ZoneInfo("Asia/Shanghai")

# 成员执行结果与最终回答的期望格式说明
_MEMBER_EXPECTED = "任务的完整执行结果（中文，含过程要点、结论与必要的产出文件路径/来源）"
_LEADER_EXPECTED = "直接面向用户的中文 Markdown 回答（综合各成员结果裁定作答，不照抄成员原始输出）"


def _now_line() -> str:
    """当前北京时间（原框架 current-time 注入的等价物）。"""
    return "【当前时间】" + datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M:%S %A") + "（北京时间）"


def _history_block(replay: list[dict[str, str]]) -> str:
    if not replay:
        return ""
    lines = [f"{'用户' if item['role'] == 'user' else '助手'}：{item['content']}" for item in replay]
    return (
        "【对话历史（此前消息，仅供理解上下文，不要重复回答其中已解决的问题）】\n"
        + "\n".join(lines)
        + "\n\n"
    )


def task_description(replay: list[dict[str, str]], user_content: str) -> str:
    """leader/单 Agent 的任务描述：回放窗口 + 本轮消息（附件预算只留给本轮图片）。"""
    return f"{_history_block(replay)}【本轮用户消息】\n{user_content}"


def _agent(role: str, goal: str, backstory: str, llm: Any, tools: list[BaseTool] | None = None, max_iter: int = 25) -> Agent:
    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        llm=llm,
        tools=tools or [],
        max_iter=max_iter,
        verbose=False,
    )


class _TeamAssignArgs(BaseModel):
    member: str = Field(description="要委派的成员名（必须来自当前成员清单）")
    prompt: str = Field(description="自包含的任务描述：背景、目标、预期产出（成员看不到你的上下文与对话历史）")


class TeamAssignTool(BaseTool):
    """leader 的委派工具：为成员建 Task 并跑独立 Crew，结果作为工具结果回流。

    同轮多次 TeamAssign 由执行器顺序执行（跨成员委派串行）；成员自身的事件
    （thinking/text/tool_call）经全局事件总线带成员 agent_id 自然流入 SSE。
    """

    members: dict = Field(default_factory=dict, exclude=True)
    stop: Any = Field(default=None, exclude=True)

    def _run(self, member: str, prompt: str) -> str:
        if self.stop is not None and self.stop.is_set():
            return "Error: 本轮请求已停止，未执行委派。"
        agent = self.members.get(member.strip())
        if agent is None:
            available = "、".join(self.members) or "（无）"
            return f"Error: 成员 {member} 不存在。可用成员：{available}"
        task = Task(description=prompt, expected_output=_MEMBER_EXPECTED, agent=agent)
        result = Crew(agents=[agent], tasks=[task], verbose=False).kickoff()
        output = getattr(result, "raw", "") or ""
        logger.info("节点[委派完成] member=%s chars=%s", member, len(str(output)))
        return str(output)


def _team_assign_description(members: dict[str, str]) -> str:
    lines = "\n".join(f"- {name}：{desc}" for name, desc in members.items())
    return (
        "把一个自包含的任务委派给团队成员执行并取回其结果。可用成员（按分工选择）：\n"
        f"{lines}\n\n复杂任务先拆解，可多次调用分别委派；任务描述必须自包含"
        "（背景、目标、预期产出），成员看不到你的上下文与对话历史。"
    )


def build_single(
    model: str,
    thinking: bool,
    replay: list[dict[str, str]],
    user_content: str,
    summary: str = "",
    memory_block: str | None = None,
    web_search: bool = False,
    agent_tools_prompt: str = "",
    builtin_tools: list[GatedTool] | None = None,
    mcp_tool_list: list[MCPTool] | None = None,
    skills: bool = True,
    images: list[tuple[str, str]] | None = None,
) -> tuple[Crew, set[str]]:
    """单 Agent 模式：组装全能力的 Friday（现有行为，多智能体关闭/带图时走此路径）。"""
    backstory = (
        SYSTEM_PROMPT
        + (f"\n\n{memory_block}" if memory_block else "")
        + (f"\n\n{WEB_SEARCH_PROMPT}" if web_search else "")
        + (f"\n\n{agent_tools_prompt}" if agent_tools_prompt else "")
        + (f"\n\n{skill_instructions()}" if skills else "")
        + (f"\n\n{summary}" if summary else "")
        + f"\n\n{_now_line()}"
    )
    tools: list[BaseTool] = list(builtin_tools or [])
    if mcp_tool_list:
        tools.extend(mcp_tool_list)
    if skills:
        skill = skill_tool()
        if skill is not None:
            tools.append(skill)
    llm = build_llm(model, thinking=thinking, images=images)
    friday = _agent(
        role="Friday",
        goal="清晰、具体、可执行地回答用户问题",
        backstory=backstory,
        llm=llm,
        tools=tools,
    )
    task = Task(
        description=task_description(replay, user_content),
        expected_output="直接面向用户的中文 Markdown 回答",
        agent=friday,
    )
    crew = Crew(agents=[friday], tasks=[task], verbose=False)
    return crew, {str(friday.id)}


def build_team(
    model: str,
    thinking: bool,
    replay: list[dict[str, str]],
    user_content: str,
    summary: str = "",
    memory_block: str | None = None,
    web_search: bool = False,
    agent_tools_prompt: str = "",
    builtin_tools: list[GatedTool] | None = None,
    mcp_tool_list: list[MCPTool] | None = None,
) -> tuple[Crew, set[str]]:
    """多智能体模式：Friday 作 leader（规划派活+汇总作答），成员按开关裁剪。

    成员规则（成员名即 TeamAssign 取值，须唯一）：
    - 检索员：传入 mcp_tool_list 即组建；深度思考档只给 leader，成员用基础档；
    - 执行员：传入 builtin_tools 才组建（权限门控已由 crew_tools 包装）；
    - 两者皆无时调用方不应走到这里（use_team 已含该条件）。
    """
    members: dict[str, Agent] = {}
    descs: dict[str, str] = {}

    if mcp_tool_list:
        researcher_tools: list[BaseTool] = list(mcp_tool_list)
        skill = skill_tool()
        if skill is not None:
            researcher_tools.append(skill)
        researcher = _agent(
            role="检索员",
            goal="按委派任务完成检索与证据收集，返回带来源的结果摘要",
            backstory=(
                RESEARCHER_PROMPT
                + (f"\n\n{WEB_SEARCH_PROMPT}" if web_search else "")
                + f"\n\n{skill_instructions()}\n\n{_now_line()}"
            ),
            llm=build_llm(settings.llm_model),
            tools=researcher_tools,
            max_iter=15,
        )
        members["检索员"] = researcher
        descs["检索员"] = RESEARCHER_DESC

    if builtin_tools:
        executor = _agent(
            role="执行员",
            goal="按委派任务完成文件与命令操作，返回执行结果与产出文件路径",
            backstory=EXECUTOR_PROMPT + (f"\n\n{agent_tools_prompt}" if agent_tools_prompt else "") + f"\n\n{_now_line()}",
            llm=build_llm(settings.llm_model),
            tools=list(builtin_tools),
            max_iter=15,
        )
        members["执行员"] = executor
        descs["执行员"] = EXECUTOR_DESC

    assign = TeamAssignTool(
        name="TeamAssign",
        description=_team_assign_description(descs),
        members=members,
    )
    member_lines = "\n".join(f"- {name}：{descs[name]}" for name in members)
    leader = _agent(
        role="Friday",
        goal="作为团队负责人：简单问题直接回答，复杂任务拆解委派成员并汇总作答",
        backstory=(
            SYSTEM_PROMPT
            + (f"\n\n{memory_block}" if memory_block else "")
            + "\n\n"
            + LEADER_TEAM_PROMPT.format(members=member_lines)
            + (f"\n\n{summary}" if summary else "")
            + f"\n\n{_now_line()}"
        ),
        llm=build_llm(model, thinking=thinking),
        tools=[assign],
    )
    task = Task(
        description=task_description(replay, user_content),
        expected_output=_LEADER_EXPECTED,
        agent=leader,
    )
    crew = Crew(agents=[leader, *members.values()], tasks=[task], verbose=False)
    return crew, {str(a.id) for a in [leader, *members.values()]}
