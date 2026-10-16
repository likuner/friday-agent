"""输出侧事件翻译：AgentScope 流式事件 → 前端 SSE 事件 dict。

含工具调用参数的累积状态与 chip 去重；权限确认（park/resume）流程
属于 stream() 编排，不在此模块。多智能体模式下事件经 AgentTagMiddleware
在产生现场打上归属（reply_id → agent 名），翻译时附加到 SSE 的 agent 字段。
"""

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Callable

from agentscope.event import EventType
from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

# chip 摘要的参数键优先级：检索词 → 委派成员（TeamAssign）→ 命令 → 文件路径 →
# 搜索模式 → 目录 → 任务标题 → 技能名
TOOL_QUERY_KEYS = ("query", "member", "command", "file_path", "pattern", "path", "subject", "skill")

# 单 Agent 模式 / 归属未知时的事件来源名（与主 Agent 同名）
DEFAULT_AGENT_NAME = "Friday"


def tool_query(raw_args: str) -> str:
    """从工具调用参数 JSON 里取出展示摘要（检索词/命令/文件路径等），供前端 chip。"""
    try:
        payload = json.loads(raw_args) if raw_args else {}
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in TOOL_QUERY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return ""


class AgentTagMiddleware(MiddlewareBase):
    """多 Agent 事件归属标记：在事件产生现场把 reply_id → agent 名写入共享注册表。

    TeamPipeline 转发 leader/成员事件时不带 agent 身份（只有 reply_id），
    事后按 AgentState.reply_id 反查有竞态（同一成员顺序执行两个委派时，消费侧
    还在处理旧事件、其 state.reply_id 已指向新回复）。挂在各 Agent 的事件出口处
    打标则无此问题。每个 Agent 独立实例、共享同一注册表 dict；单 Agent 模式
    不挂，事件默认归属 Friday。
    """

    def __init__(self, names: dict[str, str]) -> None:
        self._names = names

    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async for event in next_handler(**input_kwargs):
            reply_id = getattr(event, "reply_id", None)
            if reply_id:
                self._names[reply_id] = agent.name
            yield event


class ReplyEventTranslator:
    """单次回复流的事件翻译器：增量翻译并持有工具调用的跨事件状态。

    tool_args/tool_names 跨 START/DELTA/END 累积（参数到 END 才收全）；
    announced 记录已下发过 chip 的调用（权限确认续跑后不重复发）。
    agent_names 为 reply_id → agent 名注册表（多 Agent 模式由
    AgentTagMiddleware 填充，单 Agent 模式留空恒为 Friday）。
    """

    def __init__(self, agent_names: dict[str, str] | None = None) -> None:
        # 注意不能写 `agent_names or {}`：构建时注册表还是空 dict（falsy），
        # or 会换出新 dict，与 AgentTagMiddleware 写入的注册表脱钩
        self._agent_names: dict[str, str] = agent_names if agent_names is not None else {}
        self._tool_args: dict[str, str] = {}
        self._tool_names: dict[str, str] = {}
        self._announced: set[str] = set()

    def agent_of(self, reply_id: str | None) -> str:
        """按 reply_id 解析事件归属的 agent 名（未知时归主 Agent）。"""
        return self._agent_names.get(reply_id or "", DEFAULT_AGENT_NAME)

    def feed(self, event: Any) -> dict[str, str] | None:
        """翻译单个事件；无需下发的事件（START/DELTA 等）返回 None。"""
        if event.type == EventType.TEXT_BLOCK_DELTA:
            return {"type": "text", "content": event.delta, "agent": self.agent_of(event.reply_id)}
        if event.type == EventType.THINKING_BLOCK_DELTA:
            return {"type": "thinking", "content": event.delta, "agent": self.agent_of(event.reply_id)}
        if event.type == EventType.TOOL_CALL_START:
            self._tool_args[event.tool_call_id] = ""
            self._tool_names[event.tool_call_id] = event.tool_call_name
        elif event.type == EventType.TOOL_CALL_DELTA:
            self._tool_args[event.tool_call_id] = self._tool_args.get(event.tool_call_id, "") + event.delta
        elif event.type == EventType.TOOL_CALL_END:
            if event.tool_call_id in self._announced:
                return None
            self._announced.add(event.tool_call_id)
            # 参数到 END 才收全：把工具名和检索词一起下发，前端 chip 显示检索词，
            # 多次并行检索（多角度）就不会看起来像重复的同一个 chip。
            return {
                "type": "tool_call",
                "name": self._tool_names.get(event.tool_call_id, ""),
                "query": tool_query(self._tool_args.get(event.tool_call_id, "")),
                "agent": self.agent_of(event.reply_id),
            }
        return None
