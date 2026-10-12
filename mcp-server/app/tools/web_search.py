"""联网搜索工具：转交智谱 GLM 的内置联网检索执行（移植自 backend/app/websearch.py）。

链路：消费方（agent/客户端）调用 web_search(query)
   → httpx 调 GLM chat/completions，tools 传入 type=web_search（search_std/pro 引擎）
   → GLM 检索并生成摘要；原始搜索结果（标题/链接/摘要/发布时间）在响应顶层 web_search 字段
   → 两者拼成工具结果返回，由消费方引用来源作答。
"""

import logging
import re
import time
from typing import Annotated

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from ..config import settings
from ..server import mcp

logger = logging.getLogger("mcp-server.websearch")

# GLM 搜索结果的网页摘要里偶有 <think>…</think> 思考片段泄漏，返回前清掉
_THINK_SPAN = re.compile(r"<think>.*?</think>", re.DOTALL)
# 单条网页摘要截断长度，避免工具结果无限膨胀
_SNIPPET_LIMIT = 800


async def _glm_web_search(query: str, count: int) -> tuple[str, list[dict]]:
    """调 GLM 内置联网检索，返回（GLM 检索摘要, 原始网页搜索结果）。"""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.glm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
            json={
                "model": settings.glm_model,
                "messages": [
                    {"role": "user", "content": f"请联网检索以下问题的最新信息，并用要点简要总结（附来源）：\n{query}"},
                ],
                "tools": [
                    {
                        "type": "web_search",
                        "web_search": {
                            "enable": True,
                            "search_engine": settings.glm_search_engine,
                            "search_result": True,
                            "count": count,
                        },
                    },
                ],
            },
        )
        resp.raise_for_status()
    data = resp.json()
    message = data["choices"][0]["message"]
    summary = (message.get("content") or "").strip()
    # 原始搜索结果：当前位于响应顶层 web_search 字段；tool_calls 内的位置做兼容
    results = data.get("web_search") if isinstance(data.get("web_search"), list) else []
    if not results:
        for call in message.get("tool_calls") or []:
            block = call.get("web_search") or {}
            if isinstance(block.get("search_result"), list):
                results = block["search_result"]
                break
    logger.info(
        "节点[联网搜索完成] model=%s engine=%s hits=%s latency=%.2fs query=%r",
        settings.glm_model, settings.glm_search_engine, len(results),
        time.monotonic() - started, query[:80],
    )
    return summary, results


def _format_results(summary: str, results: list[dict]) -> str:
    lines = []
    if summary:
        lines.append(f"【GLM 检索摘要】\n{summary}")
    lines.append("【网页搜索结果】")
    for index, item in enumerate(results, 1):
        snippet = _THINK_SPAN.sub("", str(item.get("content") or "")).strip()
        if len(snippet) > _SNIPPET_LIMIT:
            snippet = snippet[:_SNIPPET_LIMIT] + "…"
        origin = "，".join(part for part in (item.get("media") or "", item.get("publish_date") or "") if part)
        lines.append(
            f"[{index}] {str(item.get('title') or '').strip()}\n"
            f"来源: {item.get('link') or ''}" + (f"（{origin}）" if origin else "")
            + (f"\n摘要: {snippet}" if snippet else "")
        )
    return "\n\n".join(lines)


@mcp.tool(
    # 声明只读：AgentScope 等消费方据此自动放行，不弹人工确认（无注解默认按需确认处理）
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def web_search(
    query: Annotated[str, Field(description="搜索关键词或问题，涉及时效性内容时使用")],
    count: Annotated[int, Field(description="返回的网页结果条数", ge=1, le=20)] = 5,
) -> str:
    """联网搜索最新信息（由 GLM 联网检索执行）。当问题涉及时效性内容时调用：新闻与热点事件、最新版本/发布/价格等动态数据、近期政策法规、人物或公司近况，以及任何你不确定是否已过时的常识；检索后结合结果回答并注明来源链接与发布时间。本地知识能稳定回答的问题不要调用。"""
    logger.info("节点[联网搜索开始] query=%r count=%s", query[:120], count)
    if not settings.zhipu_api_key:
        return "联网搜索不可用：服务端未配置 ZHIPU_API_KEY，请基于已有知识回答并说明未能联网检索。"
    try:
        summary, results = await _glm_web_search(query, count)
    except Exception as exc:
        logger.exception("节点[联网搜索异常] query=%r", query[:120])
        return f"联网搜索失败：{exc}"
    if not results:
        note = "联网搜索未返回网页结果，请基于已有知识回答，并说明未能获取到联网检索结果。"
        return f"{note}\n{summary}" if summary else note
    for index, item in enumerate(results, 1):
        logger.debug(
            "节点[联网搜索命中] #%s title=%r link=%s date=%s",
            index, str(item.get("title") or "")[:80], item.get("link"), item.get("publish_date"),
        )
    return _format_results(summary, results)
