"""输出侧事件桥：CrewAI 事件总线 → 前端 SSE 事件 dict。

三件事：
1. StreamHub——线程→协程的事件队列桥（crew 在 asyncio.to_thread 的工作线程里
   跑，事件经 call_soon_threadsafe 送回 SSE 请求协程，FIFO 保序）；
2. tool_query——工具入参里取 chip 展示摘要（检索词/命令/文件路径等）；
3. SSEEventBridge——每请求在全局 crewai_event_bus 上挂同步 handler，按
   agent_id 路由本请求的事件并翻译成与原 AgentScope 版逐字段相同的 SSE dict
   （text/thinking/tool_call 带 agent 归属；权限确认事件由 AskFlow 直接经
   hub 发，不走这里）。请求结束必须 close() 注销 handler。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from typing import Any, Callable

from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.llm_events import LLMStreamChunkEvent, LLMThinkingChunkEvent
from crewai.events.types.tool_usage_events import ToolUsageStartedEvent

# chip 摘要的参数键优先级：检索词 → 委派成员（TeamAssign）→ 命令 → 文件路径 →
# 搜索模式 → 目录 → 任务标题 → 技能名
TOOL_QUERY_KEYS = ("query", "member", "command", "file_path", "pattern", "path", "subject", "skill")

# 单 Agent 模式 / 归属未知时的事件来源名（与主 Agent 同名）
DEFAULT_AGENT_NAME = "Friday"


def tool_query(tool_args: dict[str, Any] | str) -> str:
    """从工具调用参数（dict 或 JSON 字符串）里取出展示摘要，供前端 chip。"""
    if isinstance(tool_args, str):
        try:
            payload = json.loads(tool_args) if tool_args else {}
        except ValueError:
            return ""
    else:
        payload = tool_args
    if not isinstance(payload, dict):
        return ""
    for key in TOOL_QUERY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return ""


class StreamHub:
    """线程→协程事件桥：任意线程 emit，SSE 协程侧消费；close 后丢弃新事件。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
        self._closed = False
        # SSE 中止/超时置位：工具执行前检查，尽快让 crew 收尾
        self.stop = threading.Event()

    def bind(self) -> None:
        """在 SSE 请求协程里调用，绑定事件循环。"""
        self._loop = asyncio.get_running_loop()

    def emit(self, event: dict[str, str]) -> None:
        """任意线程调用：事件送回协程侧队列（close 后静默丢弃）。"""
        loop = self._loop
        if loop is None or self._closed:
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def close(self) -> None:
        """放入结束哨兵，消费循环退出。"""
        if self._loop is not None and not self._closed:
            self._closed = True
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def events(self) -> AsyncIterator[dict[str, str]]:
        """消费事件直到哨兵（唯一消费方：SSE 生成器）。"""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class SSEEventBridge:
    """每请求的事件翻译桥：全局总线上挂 handler → 过滤本请求 agent → 翻译 → hub。

    归属路由按 agent_id（uuid）：LLM 事件经 from_agent 填 agent_id，工具事件
    兜底读事件携带的 agent 对象。agent 名直接用 role（组装时 role 即展示名：
    Friday / 检索员 / 执行员）。

    name_map：CrewAI 会把工具名 sanitize 成小写下划线（TeamAssign →
    team_assign、mcp__common_tools__web_search → mcp_common_tools_web_search）
    后才发给模型，事件里的 tool_name 也是 sanitize 名——chip 下发前映射回
    原始名（前端按原始名匹配图标/文案）。
    """

    def __init__(self, hub: StreamHub, agent_ids: set[str], name_map: dict[str, str] | None = None) -> None:
        self._hub = hub
        self._agent_ids = agent_ids
        self._name_map = name_map or {}
        self._handlers: list[tuple[type, Callable[..., Any]]] = []

        def _on_stream_chunk(source: Any, event: LLMStreamChunkEvent) -> None:
            # 工具调用参数增量（tool_call 非空）不下发正文，chip 由
            # ToolUsageStarted 事件统一发（参数届时已解析完整）
            if self._match(event) and event.tool_call is None and event.chunk:
                self._hub.emit({
                    "type": "text",
                    "content": event.chunk,
                    "agent": event.agent_role or DEFAULT_AGENT_NAME,
                })

        def _on_thinking_chunk(source: Any, event: LLMThinkingChunkEvent) -> None:
            if self._match(event) and event.chunk:
                self._hub.emit({
                    "type": "thinking",
                    "content": event.chunk,
                    "agent": event.agent_role or DEFAULT_AGENT_NAME,
                })

        def _on_tool_started(source: Any, event: ToolUsageStartedEvent) -> None:
            if self._match(event):
                self._hub.emit({
                    "type": "tool_call",
                    "name": self._name_map.get(event.tool_name, event.tool_name),
                    "query": tool_query(event.tool_args),
                    "agent": event.agent_role or DEFAULT_AGENT_NAME,
                })

        for etype, fn in (
            (LLMStreamChunkEvent, _on_stream_chunk),
            (LLMThinkingChunkEvent, _on_thinking_chunk),
            (ToolUsageStartedEvent, _on_tool_started),
        ):
            handler = crewai_event_bus.on(etype)(fn)
            self._handlers.append((etype, handler))

    def _match(self, event: Any) -> bool:
        agent_id = getattr(event, "agent_id", None)
        if agent_id is None:
            agent = getattr(event, "agent", None)
            agent_id = getattr(agent, "id", None)
        return agent_id is not None and str(agent_id) in self._agent_ids

    def close(self) -> None:
        """注销本请求挂在全局总线上的全部 handler（必须调用，防泄漏）。"""
        for etype, handler in self._handlers:
            crewai_event_bus.off(etype, handler)
        self._handlers.clear()
