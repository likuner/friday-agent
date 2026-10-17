"""CrewAI LLM 构造：DeepSeek OpenAI 兼容端点 + 思考流（reasoning_content）。

CrewAI 1.15 的 OpenAI 兼容流式路径只解析 delta.content / delta.tool_calls，
不认 DeepSeek 的 delta.reasoning_content（思考增量会被静默丢弃）。这里子类化
OpenAICompatibleCompletion，在流式迭代外再包一层：把 reasoning_content 以
LLMThinkingChunkEvent 发到事件总线（与 content 的 LLMStreamChunkEvent 同路），
供 crew_events 翻译成 SSE thinking 事件。包一层生成器而非复制父类解析逻辑，
升级 crewai 时仅需回归思考流冒烟。

思考开关经 additional_params 注入请求体顶层 {"thinking": {"type": "enabled"}}
（与原 agentscope DeepSeekChatModel.Parameters(thinking_enable=True) 的线上
行为一致）；未开启时不发该参数（模型默认不思考）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from crewai.llms.providers.openai_compatible.completion import (
    OpenAICompatibleCompletion,
)
from pydantic import Field

from .config import settings

logger = logging.getLogger("friday.crew_llm")

# 深度思考档的请求体参数（DeepSeek v4 系列在请求体顶层接收 thinking 对象）。
# openai SDK 的 create() 不认非标关键字，须包在 extra_body 里透传；
# deepseek-v4-pro 默认即输出 reasoning_content，未开深度思考时显式关闭
# （与原 agentscope _get_disable_thinking_kwargs 的线上行为一致）。
_THINKING_ENABLED = {"extra_body": {"thinking": {"type": "enabled"}}}
_THINKING_DISABLED = {"extra_body": {"thinking": {"type": "disabled"}}}


class DeepSeekCompletion(OpenAICompatibleCompletion):
    """OpenAICompatibleCompletion + DeepSeek 思考增量转发 + 图片注入。

    只覆写同步流式路径：本项目经 asyncio.to_thread 跑 crew.kickoff，
    所有 LLM 调用都走 sync 路径；结构化输出（response_model）分支不拦，
    本项目不用它。带图请求（vision 档）由 images 携带 (base64, media_type)
    列表，发起调用前把最后一条 user 消息的 content 换成多模态 blocks
    （DeepSeek 接受 image_url 形式的 data URI，与原 OpenAIChatFormatter
    行为一致）。
    """

    images: Any = Field(default=None, exclude=True)

    def _inject_images(self, params: dict[str, Any]) -> None:
        """把 images 拼进最后一条 user 消息（content str → 多模态 blocks）。"""
        if not self.images:
            return
        messages = params.get("messages") or []
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            text = message.get("content")
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False) if text else ""
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text or "请描述这张图片。"}]
            blocks.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                }
                for data, media_type in self.images
            )
            message["content"] = blocks
            logger.info("节点[多模态消息] images=%s text=%r", len(blocks) - 1, (text or "")[:60])
            return

    def _handle_streaming_completion(
        self,
        params: dict[str, Any],
        available_functions: dict[str, Any] | None = None,
        from_task: Any | None = None,
        from_agent: Any | None = None,
        response_model: type[Any] | None = None,
    ) -> Any:
        if response_model is not None:
            return super()._handle_streaming_completion(
                params, available_functions, from_task, from_agent, response_model
            )
        self._inject_images(params)
        # 临时替换本实例 openai client 的 create：父类解析逻辑原样跑，
        # 只是流经我们时先摘出 reasoning_content 发思考事件再放行。
        # client 为本 LLM 实例私有（每请求每 Agent 独立构建），无并发串扰。
        client = self._get_sync_client()
        original_create = client.chat.completions.create

        def _create_with_thinking(**kwargs: Any) -> Any:
            stream = original_create(**kwargs)
            return self._thinking_stream(stream, from_task, from_agent)

        client.chat.completions.create = _create_with_thinking
        try:
            return super()._handle_streaming_completion(
                params, available_functions, from_task, from_agent, response_model
            )
        finally:
            client.chat.completions.create = original_create

    def _thinking_stream(self, stream: Any, from_task: Any, from_agent: Any) -> Any:
        """逐块转发流：reasoning_content → LLMThinkingChunkEvent，其余原样。"""
        for chunk in stream:
            try:
                choices = getattr(chunk, "choices", None)
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        self._emit_thinking_chunk_event(
                            chunk=reasoning,
                            from_task=from_task,
                            from_agent=from_agent,
                            response_id=getattr(chunk, "id", None),
                        )
            except Exception:  # noqa: BLE001 - 思考流转发失败不影响正文
                logger.debug("思考增量转发异常（忽略）", exc_info=True)
            yield chunk


def build_llm(model: str, thinking: bool = False, images: list[tuple[str, str]] | None = None) -> DeepSeekCompletion:
    """按请求构造一次性 LLM 实例（无状态原则：不跨请求复用）。

    model 为 settings 里的裸模型名（deepseek-v4-pro / deepseek-flash），
    provider=deepseek 走 OpenAI 兼容路由；显式 base_url/api_key 覆盖默认。
    images 为 (base64, media_type) 列表（带图请求，vision 档专用）。
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "provider": "deepseek",
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "stream": True,
        "timeout": 300,
    }
    if thinking:
        kwargs["additional_params"] = _THINKING_ENABLED
    else:
        kwargs["additional_params"] = _THINKING_DISABLED
    if images:
        kwargs["images"] = images
    return DeepSeekCompletion(**kwargs)
