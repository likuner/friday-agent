"""上下文装配：按 token 预算把库中消息切分为「回放窗口 + 待压缩溢出段」。

每轮模型上下文的组成（自上而下）：
    ① system prompt（Agent 构造参数，固定）
    ② 滚动摘要（AgentState.summary，框架自动前置注入）
    ③ 回放窗口（预算内最近若干轮，纯文本 user/assistant 消息）
    ④ 本轮用户消息 + 图片附件（走 reply_stream 输入，图片只随本轮发）

本模块只做纯计算，不做 IO：入参是带 id/role/content/meta 属性的消息序列
（旧→新），出参是切分结果。溢出段（早于窗口、晚于摘要游标）交给
summarizer 异步压缩成新摘要写回 conversations 表。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# 中文字符（CJK 统一表意 + 中文标点 + 全角符号）≈1.6 字符/token，其余（英文为主）≈4 字符/token
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """字符近似估算 token 数。预算是软约束，精度够用即可，避免引入 tokenizer 依赖。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(cjk / 1.6 + other / 4) + 1


def clean_for_replay(message: Any) -> dict[str, str] | None:
    """清洗单条消息为可回放的纯文本；不可回放（空内容）返回 None。

    约束（AgentScope 回放强校验）：只允许 user/assistant 纯文本，
    tool_call/thinking 只存在于 meta 中、天然不进入回放；历史图片附件
    不重发给模型，替换为「[图片]」占位（附件预算只留给本轮）。
    """
    if message.role not in ("user", "assistant"):
        return None
    content = str(message.content or "").strip()
    if (message.meta or {}).get("attachments"):
        content = f"[图片] {content}".strip()
    if not content:
        # 中止生成的空回复、纯空白内容：既不回放也不进摘要
        return None
    return {"role": message.role, "content": content}


@dataclass
class ContextWindow:
    """一次切分的结果。replay/overflow 均按时间旧→新。"""

    replay: list[dict[str, str]] = field(default_factory=list)
    replay_tokens: int = 0
    overflow: list[Any] = field(default_factory=list)  # 原始消息对象，供摘要管线取 id 与全文
    overflow_tokens: int = 0


def build_context_window(
    messages: Sequence[Any],
    summary_upto_id: UUID | None,
    budget_tokens: int,
) -> ContextWindow:
    """把消息序列切成回放窗口与溢出段。

    - 早于等于 ``summary_upto_id``（摘要游标）的消息视为已压缩，直接跳过；
      游标在序列中找不到（如消息被「编辑重发」截断删除）时不排除任何消息。
    - 从最新向前逐条累加 token 塞满预算即停；单条超剩余预算则整条不塞
      （不切半条消息），归入溢出段。本轮用户消息不在此列（走 reply_stream 输入），
      因此窗口为空也不影响当轮对话。
    """
    eligible = list(messages)
    if summary_upto_id is not None:
        for index, message in enumerate(eligible):
            if message.id == summary_upto_id:
                eligible = eligible[index + 1 :]
                break

    window = ContextWindow()
    oldest_index: int | None = None  # 进入窗口的最老一条在 eligible 中的下标，即溢出段边界
    for index in range(len(eligible) - 1, -1, -1):
        cleaned = clean_for_replay(eligible[index])
        if cleaned is None:
            continue
        cost = estimate_tokens(cleaned["content"])
        if window.replay_tokens + cost > budget_tokens:
            break
        window.replay.insert(0, cleaned)
        window.replay_tokens += cost
        oldest_index = index

    if oldest_index is not None:
        boundary = oldest_index
    else:
        # 窗口为空（无可回放消息，或最新一条就超预算）：可回放的全部归入溢出段
        boundary = len(eligible)

    window.overflow = [m for m in eligible[:boundary] if clean_for_replay(m) is not None]
    window.overflow_tokens = sum(
        estimate_tokens(clean_for_replay(m)["content"]) for m in window.overflow
    )
    return window
