"""LangChain 回调版轻量 OTel 追踪。

替代原 AgentScope ``TracingMiddleware`` 的 GenAI span 导出：模型调用与工具
调用各产出一条基础 span（模型名、耗时、token 用量、工具名与结果状态），
经 tracing.py 初始化的全局 ``TracerProvider`` 按 OTLP 导出，可接
AgentScope Studio / Jaeger / Langfuse 等任意后端。

与原实现的差异：非完整 GenAI 语义约定 span 树（无 reply/reasoning 层级），
只覆盖模型与工具两级；``TRACING_ENABLED=false``（默认）时零开销短路。
挂在 ``llm.callbacks`` 上（模型级事件），工具执行事件由 graph.py 的
``节点[...]`` 日志承载。
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

logger = logging.getLogger("friday.tracing")

_TRACER = None


def _tracer():
    """惰性取全局 tracer：tracing 未启用时返回 None（回调全部短路）。"""
    global _TRACER
    if _TRACER is None:
        try:
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            # 未配置时是 ProxyTracerProvider（无真实导出），照样能跑但导不出去；
            # tracing.py 已启用时会换成真 provider，这里不重复判断
            _TRACER = trace.get_tracer("friday.agent")
        except Exception:  # opentelemetry 未安装：完全禁用
            _TRACER = False
    return _TRACER or None


class TracingCallbackHandler(BaseCallbackHandler):
    """模型调用的基础 span：gen_ai.friday.llm，属性带模型名/耗时/token。"""

    def __init__(self) -> None:
        self._spans: dict[int, Any] = {}
        self._starts: dict[int, float] = {}

    def _key(self, run_id: Any) -> int | None:
        try:
            return hash(run_id)
        except TypeError:
            return None

    def on_chat_model_start(self, serialized: dict, prompts: list[list[BaseMessage]], **kwargs: Any) -> None:
        tracer = _tracer()
        if tracer is None:
            return
        key = self._key(kwargs.get("run_id"))
        if key is None:
            return
        invocation = kwargs.get("invocation_params") or {}
        model = invocation.get("model") or invocation.get("model_name") or "unknown"
        span = tracer.start_span(f"gen_ai.friday.llm {model}")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.prompt.messages", len(prompts[0]) if prompts else 0)
        self._spans[key] = span
        self._starts[key] = time.perf_counter()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        key = self._key(kwargs.get("run_id"))
        span = self._spans.pop(key, None)
        start = self._starts.pop(key, None)
        if span is None:
            return
        usage = getattr(response, "llm_output", None) or {}
        token_usage = usage.get("token_usage") or {}
        if token_usage:
            span.set_attribute("gen_ai.usage.input_tokens", token_usage.get("prompt_tokens", 0))
            span.set_attribute("gen_ai.usage.output_tokens", token_usage.get("completion_tokens", 0))
        if start is not None:
            span.set_attribute("duration_ms", round((time.perf_counter() - start) * 1000, 1))
        span.set_attribute("gen_ai.response.id", str(uuid4().hex[:8]))
        span.end()

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        key = self._key(kwargs.get("run_id"))
        span = self._spans.pop(key, None)
        self._starts.pop(key, None)
        if span is not None:
            span.set_attribute("error.type", type(error).__name__)
            span.record_exception(error)
            span.end()


def make_tracing_callbacks() -> list[BaseCallbackHandler] | None:
    """按配置构造回调列表：未启用时返回 None（调用方不挂任何回调）。"""
    from .config import settings

    if not settings.tracing_enabled:
        return None
    logger.info("节点[Tracing] LangChain 回调已挂载（OTLP 导出）")
    return [TracingCallbackHandler()]
