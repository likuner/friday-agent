"""Agent 执行段的结构化节点日志中间件。

原先内嵌在 ``AgentService.stream()`` 事件循环里的 节点[...] 日志，收敛为挂在
Agent 上的中间件：模型调用、工具调用/参数/结果、耗时、token 用量都在钩子里记，
``stream()`` 只保留「事件 → SSE 翻译」职责。边界：服务层上下文（回放条数、摘要、
开关）仍在 agent.py；工具内部过程日志仍在 rag.py / websearch.py 等工具模块。
"""

import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Callable

from agentscope.event import EventType
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger("friday.agent")


def _content_preview(blocks: list[Any] | None) -> str:
    """工具结果内容块的文本预览，供错误信息排查。"""
    texts = [getattr(block, "text", "") for block in blocks or []]
    return "".join(t for t in texts if isinstance(t, str)).strip()[:120]


class AgentLoggingMiddleware(MiddlewareBase):
    """AgentScope 中间件：Agent 执行段的 节点[...] 日志。"""

    def __init__(self) -> None:
        # 每轮对话独立构建 Agent、独立中间件实例，轮次计数与 token 累计随实例隔离，无并发串味
        self._round = 0
        self._input_tokens = 0
        self._output_tokens = 0

    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        # 回复开始不另记：stream() 的 节点[Agent调用] 已带 replay/摘要/开关等服务层上下文
        start = time.perf_counter()
        async for item in next_handler(**input_kwargs):
            yield item
        logger.info(
            "节点[Agent返回] 模型流结束 rounds=%s input_tokens=%s output_tokens=%s 耗时=%.2fs session=%s",
            self._round, self._input_tokens, self._output_tokens,
            time.perf_counter() - start, agent.state.session_id,
        )

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        self._round += 1
        async for event in next_handler(**input_kwargs):
            # 流末尾可能带最终 Msg（无 type 字段），防御性取值
            event_type = getattr(event, "type", None)
            if event_type == EventType.MODEL_CALL_START:
                logger.info(
                    "节点[模型调用] round=%s model=%s session=%s",
                    self._round, event.model_name, agent.state.session_id,
                )
            elif event_type == EventType.MODEL_CALL_END:
                self._input_tokens += event.input_tokens
                self._output_tokens += event.output_tokens
                logger.info(
                    "节点[模型返回] round=%s input_tokens=%s output_tokens=%s cache_read=%s cache_write=%s finished=%s",
                    self._round, event.input_tokens, event.output_tokens,
                    event.cache_input_tokens, event.cache_creation_input_tokens,
                    event.finished_reason,
                )
            # 逐 token 增量日志噪音大，默认关闭；排查需要时取消注释
            # （DEBUG 级只进 logs/backend.log，控制台是 INFO 不受影响）
            # elif event_type == EventType.TEXT_BLOCK_DELTA:
            #     logger.debug("节点[模型增量] round=%s type=text delta=%r", self._round, event.delta)
            # elif event_type == EventType.THINKING_BLOCK_DELTA:
            #     logger.debug("节点[模型增量] round=%s type=thinking delta=%r", self._round, event.delta)
            yield event

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        if not isinstance(tool_call, ToolCallBlock):
            async for item in next_handler(**input_kwargs):
                yield item
            return
        logger.info(
            "节点[工具调用] model 自主决策调用工具 name=%s call_id=%s args=%s",
            tool_call.name, tool_call.id, (tool_call.input or "")[:200],
        )
        start = time.perf_counter()
        last_item = None
        try:
            async for item in next_handler(**input_kwargs):
                last_item = item
                yield item
        except Exception:
            logger.exception(
                "节点[工具异常] name=%s call_id=%s 耗时=%.2fs",
                tool_call.name, tool_call.id, time.perf_counter() - start,
            )
            raise
        state = getattr(last_item, "state", None)
        if state == ToolResultState.ERROR:
            logger.warning(
                "节点[工具结果] name=%s call_id=%s state=%s 耗时=%.2fs preview=%r",
                tool_call.name, tool_call.id, state, time.perf_counter() - start,
                _content_preview(getattr(last_item, "content", None)),
            )
        else:
            logger.info(
                "节点[工具结果] name=%s call_id=%s state=%s 耗时=%.2fs",
                tool_call.name, tool_call.id, state, time.perf_counter() - start,
            )
