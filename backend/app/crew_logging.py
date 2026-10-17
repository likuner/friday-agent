"""CrewAI 执行段的结构化节点日志（全局事件监听器，替代原 AgentLoggingMiddleware）。

挂在进程级 crewai_event_bus 上的同步 handler：模型调用（开始/返回/失败，
含 token 用量）、工具调用/参数/结果/耗时都记 节点[...] 日志。与原中间件的
差异：全局监听无每轮私有状态，不再输出 rounds 计数与单轮 token 累计
（单事件 usage 已含 token 明细；总耗时仍由 chat.py 节点[流式完成] 记）。
DEBUG 明细进根 logs/backend.log（friday_shared 统一配置），控制台 INFO
不受影响。幂等安装：重复调用不重复挂载。
"""

from __future__ import annotations

import logging
from typing import Any

from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.llm_events import (
    LLMCallCompletedEvent,
    LLMCallFailedEvent,
    LLMCallStartedEvent,
)
from crewai.events.types.tool_usage_events import (
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)

logger = logging.getLogger("friday.agent")

_installed = False


def _agent_of(event: Any) -> str:
    return getattr(event, "agent_role", None) or "Friday"


def _usage_field(usage: dict[str, Any] | None, *names: str) -> Any:
    for name in names:
        if usage and usage.get(name) is not None:
            return usage[name]
    return "-"


def _output_preview(output: Any) -> str:
    text = output if isinstance(output, str) else repr(output)
    return text.strip()[:120]


def install_logging_listener() -> None:
    """幂等安装全局执行段日志监听器（main 启动时调用一次）。"""
    global _installed
    if _installed:
        return
    _installed = True

    def _on_llm_started(source: Any, event: LLMCallStartedEvent) -> None:
        logger.info(
            "节点[模型调用] agent=%s model=%s", _agent_of(event), event.model or "-"
        )

    def _on_llm_completed(source: Any, event: LLMCallCompletedEvent) -> None:
        usage = event.usage or {}
        logger.info(
            "节点[模型返回] agent=%s input_tokens=%s output_tokens=%s reasoning_tokens=%s finished=%s",
            _agent_of(event),
            _usage_field(usage, "prompt_tokens", "input_tokens"),
            _usage_field(usage, "completion_tokens", "output_tokens"),
            _usage_field(usage, "reasoning_tokens", "completion_tokens_details.reasoning_tokens"),
            event.finish_reason or "-",
        )

    def _on_llm_failed(source: Any, event: LLMCallFailedEvent) -> None:
        logger.warning("节点[模型异常] agent=%s error=%s", _agent_of(event), event.error)

    def _on_tool_started(source: Any, event: ToolUsageStartedEvent) -> None:
        logger.info(
            "节点[工具调用] agent=%s 自主决策调用工具 name=%s args=%s",
            _agent_of(event), event.tool_name, str(event.tool_args)[:200],
        )

    def _on_tool_finished(source: Any, event: ToolUsageFinishedEvent) -> None:
        duration = (event.finished_at - event.started_at).total_seconds()
        if event.failure is not None:
            logger.warning(
                "节点[工具结果] name=%s state=error 耗时=%.2fs preview=%r",
                event.tool_name, duration, _output_preview(event.output),
            )
        else:
            logger.info(
                "节点[工具结果] name=%s state=ok 耗时=%.2fs", event.tool_name, duration
            )

    def _on_tool_error(source: Any, event: ToolUsageErrorEvent) -> None:
        logger.warning(
            "节点[工具异常] name=%s error=%s", event.tool_name, str(event.error)[:200]
        )

    for etype, fn in (
        (LLMCallStartedEvent, _on_llm_started),
        (LLMCallCompletedEvent, _on_llm_completed),
        (LLMCallFailedEvent, _on_llm_failed),
        (ToolUsageStartedEvent, _on_tool_started),
        (ToolUsageFinishedEvent, _on_tool_finished),
        (ToolUsageErrorEvent, _on_tool_error),
    ):
        crewai_event_bus.on(etype)(fn)
