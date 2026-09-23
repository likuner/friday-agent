"""滚动摘要：把被回放预算截断的早期历史异步压缩成会话摘要并落库。

链路：chat.py 装配上下文窗口时发现溢出段超过阈值
   → 后台任务重算窗口（与主流程同参数，天然幂等）
   → GLM（glm-4-flash，复用联网搜索的智谱密钥）合并「旧摘要 + 溢出段」为新摘要
   → 写回 conversations.summary，并把 summary_upto_message_id 推进到溢出段末条（进度游标）

失败策略：任何异常只记日志、绝不抛出——下一轮发现溢出仍在会自动重试；
未配置智谱密钥时摘要停用，回放窗口照常截断（退化为丢弃早期上下文）。
"""

import asyncio
import logging
import weakref
from uuid import UUID

import httpx
from sqlalchemy import select

from .config import settings
from .context import build_context_window, clean_for_replay, estimate_tokens
from .db import SessionLocal
from .models import Conversation, Message
from .websearch import web_search_available

logger = logging.getLogger("friday.summarizer")

# 每会话一把锁：防同一会话并发压缩；无引用时自动回收，不随会话数泄漏
_summary_locks: "weakref.WeakValueDictionary[UUID, asyncio.Lock]" = weakref.WeakValueDictionary()
_warned_no_key = False

_SUMMARY_PROMPT = """你是对话摘要助手。请把「已有摘要」与「新增对话片段」合并为一份滚动摘要，作为后续对话的历史背景。
要求：
1. 用中文分条陈述，保留：用户身份与偏好、已确认的事实与结论、关键决策、未决问题；
2. 新信息覆盖旧信息（用户改口、更正时以最新为准，不要并存矛盾条目）；
3. 省略寒暄、重复与过程性细节，不要逐句复述；
4. 总长不超过约 {max_chars} 字。只输出摘要正文，不要任何前缀或说明。"""


def _lock_for(conversation_id: UUID) -> asyncio.Lock:
    lock = _summary_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _summary_locks[conversation_id] = lock
    return lock


def _render_segment(items: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{'用户' if item['role'] == 'user' else 'Friday'}：{item['content']}" for item in items
    )


async def _compress(old_summary: str | None, segment: list[dict[str, str]]) -> str | None:
    """调 GLM 把旧摘要与溢出段合并为新摘要；返回空值视为失败（交由下轮重试）。"""
    prompt = _SUMMARY_PROMPT.format(max_chars=int(settings.summary_max_tokens * 1.6))
    parts = []
    if old_summary:
        parts.append(f"【已有摘要】\n{old_summary}")
    parts.append(f"【新增对话片段】\n{_render_segment(segment)}")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.glm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
            json={
                "model": settings.glm_model,
                "messages": [{"role": "user", "content": f"{prompt}\n\n" + "\n\n".join(parts)}],
                "temperature": 0.2,
                "max_tokens": settings.summary_max_tokens * 2,
            },
        )
        resp.raise_for_status()
    content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    return content or None


async def update_rolling_summary(conversation_id: UUID) -> None:
    """后台压缩溢出段并写回会话摘要。永不抛出：失败记日志，下一轮自愈重试。"""
    global _warned_no_key
    if not web_search_available():
        if not _warned_no_key:
            logger.warning("节点[摘要停用] 未配置 ZHIPU_API_KEY，跳过滚动摘要（回放窗口照常截断）")
            _warned_no_key = True
        return

    lock = _lock_for(conversation_id)
    if lock.locked():
        return  # 同会话已有压缩在跑：跳过本轮，下一轮自愈补上
    async with lock:
        try:
            async with SessionLocal() as session:
                conversation = await session.scalar(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
                if not conversation:
                    return
                messages = (
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at)
                    )
                ).all()
                # 与主流程同参数重算窗口：只压缩「当前预算下确实放不下」的部分
                window = build_context_window(
                    messages, conversation.summary_upto_message_id, settings.context_token_budget
                )
                if not window.overflow or window.overflow_tokens < settings.summary_min_overflow_tokens:
                    return
                segment = [item for item in (clean_for_replay(m) for m in window.overflow) if item]
                if not segment:
                    return
                new_summary = await _compress(conversation.summary, segment)
                if not new_summary:
                    logger.warning("节点[摘要空回] conversation=%s 下轮将重试", conversation_id)
                    return
                conversation.summary = new_summary
                conversation.summary_upto_message_id = window.overflow[-1].id
                await session.commit()
                logger.info(
                    "节点[摘要落库] conversation=%s 覆盖至=%s 压缩前≈%s tok 摘要≈%s tok",
                    conversation_id, window.overflow[-1].id,
                    window.overflow_tokens, estimate_tokens(new_summary),
                )
        except Exception:
            logger.exception("节点[摘要异常] conversation=%s 下轮将自动重试", conversation_id)
