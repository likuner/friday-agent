"""联网搜索工具：直连智谱开放平台独立搜索 API。

链路：消费方（agent/客户端）调用 web_search(query)
   → httpx 调智谱 /web_search API（search_std/pro 引擎），传 search_query + count
   → 返回 search_result（标题/链接/摘要/媒体/发布时间）格式化为工具结果
   → 由消费方引用来源作答。

历史：曾借 chat/completions 的 tools=[{type:"web_search"}] 内置联网（顺带拿 GLM 检索摘要），
2026-10-13 起该内置检索在服务端不再执行——各模型均静默不联网、只回"无法联网检索"，
故改为直连独立搜索 API：不依赖模型行为，延迟也从 10~25s 降到 3s 内。
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

# 搜索结果的网页摘要里偶有 <think>…</think> 思考片段泄漏，返回前清掉
_THINK_SPAN = re.compile(r"<think>.*?</think>", re.DOTALL)
# 单条网页摘要截断长度，避免工具结果无限膨胀
_SNIPPET_LIMIT = 800


async def _zhipu_web_search(query: str, count: int) -> list[dict]:
    """调智谱独立搜索 API，返回原始网页搜索结果。"""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.glm_base_url}/web_search",
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
            json={
                "search_engine": settings.glm_search_engine,
                "search_query": query,
                "count": count,
            },
        )
        resp.raise_for_status()
    results = resp.json().get("search_result") or []
    logger.info(
        "节点[联网搜索完成] engine=%s hits=%s latency=%.2fs query=%r",
        settings.glm_search_engine, len(results), time.monotonic() - started, query[:80],
    )
    return results


def _format_results(results: list[dict]) -> str:
    lines = ["【网页搜索结果】"]
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
    """联网搜索最新信息。当问题涉及时效性内容时调用：新闻与热点事件、最新版本/发布/价格等动态数据、近期政策法规、人物或公司近况，以及任何你不确定是否已过时的常识；检索后结合结果回答并注明来源链接与发布时间。本地知识能稳定回答的问题不要调用。"""
    logger.info("节点[联网搜索开始] query=%r count=%s", query[:120], count)
    if not settings.zhipu_api_key:
        return "联网搜索不可用：服务端未配置 ZHIPU_API_KEY，请基于已有知识回答并说明未能联网检索。"
    try:
        results = await _zhipu_web_search(query, count)
    except Exception as exc:
        logger.exception("节点[联网搜索异常] query=%r", query[:120])
        return f"联网搜索失败：{exc}"
    if not results:
        return "联网搜索未返回网页结果，请基于已有知识回答，并说明未能获取到联网检索结果。"
    for index, item in enumerate(results, 1):
        logger.debug(
            "节点[联网搜索命中] #%s title=%r link=%s date=%s",
            index, str(item.get("title") or "")[:80], item.get("link"), item.get("publish_date"),
        )
    return _format_results(results)
