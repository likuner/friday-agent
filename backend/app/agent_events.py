"""输出侧事件翻译：AgentScope 流式事件 → 前端 SSE 事件 dict。

含工具调用参数的累积状态与 chip 去重；权限确认（park/resume）流程
属于 stream() 编排，不在此模块。
"""

import json
from typing import Any

from agentscope.event import EventType

# chip 摘要的参数键优先级：检索词 → 命令 → 文件路径 → 搜索模式 → 目录 → 任务标题 → 技能名
TOOL_QUERY_KEYS = ("query", "command", "file_path", "pattern", "path", "subject", "skill")


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


class ReplyEventTranslator:
    """单次 reply 的事件翻译器：增量翻译并持有工具调用的跨事件状态。

    tool_args/tool_names 跨 START/DELTA/END 累积（参数到 END 才收全）；
    announced 记录已下发过 chip 的调用（权限确认续跑后不重复发）。
    """

    def __init__(self) -> None:
        self._tool_args: dict[str, str] = {}
        self._tool_names: dict[str, str] = {}
        self._announced: set[str] = set()

    def feed(self, event: Any) -> dict[str, str] | None:
        """翻译单个事件；无需下发的事件（START/DELTA 等）返回 None。"""
        if event.type == EventType.TEXT_BLOCK_DELTA:
            return {"type": "text", "content": event.delta}
        if event.type == EventType.THINKING_BLOCK_DELTA:
            return {"type": "thinking", "content": event.delta}
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
            }
        return None
