"""用户长期记忆：每用户一行聚合文本，会话中异步抽取合并，每轮全量注入 system prompt。

与滚动摘要（summarizer.py）同构的合并模式：GLM 把「旧记忆全文 + 新消息段」
合并重写为新记忆全文——去重、新信息覆盖旧信息、体积有提示词上限。

护栏：写回前按行精确去重（拦截合并模型复读同一行）；读取时同样去重兜底。
抽取触发：chat.py 每轮检查会话游标后的新消息数，攒够阈值后台执行
（独立 session + 每会话锁 + 游标幂等 + 失败下轮自愈）。
"""

import asyncio
import logging
import weakref
from collections.abc import Sequence
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal
from .models import Conversation, Message, UserMemory
from .websearch import web_search_available

logger = logging.getLogger("friday.memory")

# 每会话一把锁：防同一会话并发抽取；无引用时自动回收
_extract_locks: "weakref.WeakValueDictionary[UUID, asyncio.Lock]" = weakref.WeakValueDictionary()
_warned_no_key = False

_MEMORY_HEADER = "【用户长期记忆（来自历史对话，可能过时；与最近对话冲突时以最近对话为准）】"

# 单次抽取最多处理的消息条数，防长会话一次塞爆 prompt
_EXTRACT_SEGMENT_LIMIT = 30

_MERGE_PROMPT = """你是用户记忆维护器。把「已有记忆」与「新增对话片段」合并为一份新的用户记忆清单。

最重要的规则——最新事实优先：片段中出现了与已有记忆冲突或更替的信息（搬家、换工作、改口、关系变化）时，必须用新事实改写对应条目并删除旧条目，绝不允许新旧两行同时保留（例如已有「- 城市：杭州」而片段说搬到上海，输出只能是「- 城市：上海」）。

事实来源——只记用户：要收录的事实只能来自「用户」的发言；「Friday」的回复仅作理解上下文，其中出现的医学知识、疾病科普、药物信息、建议一律不收录。用户自述的个人健康状况（所患疾病、用药）可以收录，这是医疗助手的必要信息。

合并规则：
1. 片段中值得长期记住的新信息要并入：身份与称呼、所在城市、职业、长期偏好、自述的健康状况与用药、重要的人际与宠物；用户明确说"记住…"的必收；
2. 不收录：临时上下文（当天天气）、一次性任务与提醒（如「明天复查」）、寒暄，以及任何出自 Friday 回复的知识性内容；已有记忆中不符合收录范围的旧条目也一并删除——疾病的治疗方案、缓解与并发症知识、检查项目、急症识别、药物知识、日程与活动量建议都属于医学知识或建议，不是用户事实；用户记忆只留用户自身的状况（是谁、在哪、做什么、患什么病、吃什么药、喜欢什么）；
3. 新信息与旧条目冲突时以新为准（见最上面的最重要规则），删除被取代的旧条目；语义重复的只留一条；
4. 每行一条、以"- "开头、一句话独立成立（如"- 城市：杭州"），总长不超过约 {max_chars} 字；
5. 片段中没有值得记住的信息时，原样输出已有记忆（但仍需按规则 2 剔除越界条目）。

示例：
已有记忆：
- 城市：杭州
- 职业：前端工程师
新增对话片段：
用户：我搬到上海了，转做后端了
正确的输出：
- 城市：上海
- 职业：后端工程师

只输出清单本身，不要任何解释、标题或围栏。"""


# ---------------------------------------------------------------------------
# 读取侧（每轮注入）
# ---------------------------------------------------------------------------

def _dedupe_lines(content: str) -> str:
    """按行去重（忽略行内空白差异，保留首次出现顺序）——拦截合并模型复读同一行。"""
    seen: set[str] = set()
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key = "".join(stripped.split())
        if key in seen:
            continue
        seen.add(key)
        lines.append(stripped)
    return "\n".join(lines)


def messages_since_extract(messages: Sequence, extracted_upto_id: UUID | None) -> int:
    """抽取游标之后的消息条数（触发抽取的判断依据）。

    游标在序列中找不到（如「编辑重发」截断删掉了游标消息）时视为需要从头重抽，
    与 extract_user_memory 的切片语义及摘要游标（context.py）的自愈行为一致；
    若此处返回 0，该会话的长记忆将永久停止更新。
    """
    if extracted_upto_id is None:
        return len(messages)
    for index, message in enumerate(messages):
        if message.id == extracted_upto_id:
            return len(messages) - index - 1
    return len(messages)


async def load_memory_block(session: AsyncSession, user_id: UUID) -> str | None:
    """每轮注入入口：查该用户唯一一行记忆 → 去重 → 套标注头。失败降级为 None，不阻塞对话。"""
    if not settings.memory_enabled:
        return None
    try:
        content = await session.scalar(select(UserMemory.content).where(UserMemory.user_id == user_id))
        content = _dedupe_lines(content or "")
        if not content:
            return None
        return f"{_MEMORY_HEADER}\n{content}"
    except Exception:
        logger.exception("节点[记忆读取异常] user=%s 本轮不注入长期记忆", user_id)
        return None


# ---------------------------------------------------------------------------
# 写入侧（GLM 合并管线）
# ---------------------------------------------------------------------------

def _render_segment(messages: Sequence) -> str:
    """只喂用户发言：Friday 回复里的知识/建议从源头进不了合并器。

    曾经整段喂入导致助手侧医学知识（疾病科普、药物知识）被当成用户事实
    收进记忆；只留用户发言后，无论模型是否遵守提示词的来源规则都污染不进来。
    """
    return "\n".join(
        f"用户：{str(m.content or '').strip()}" for m in messages if m.role == "user"
    )


async def _merge_via_glm(old_content: str, segment_text: str) -> str:
    """调 GLM 合并旧记忆与新消息段为新记忆全文；空回视为失败（交由下轮重试）。"""
    prompt = _MERGE_PROMPT.format(max_chars=int(settings.memory_max_tokens * 1.6))
    payload = (
        f"{prompt}\n\n【已有记忆】\n{old_content or '（暂无）'}\n\n"
        f"【新增对话片段】\n{segment_text}"
    )
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.glm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
            json={
                "model": settings.glm_model,
                "messages": [{"role": "user", "content": payload}],
                "temperature": 0.1,
                "max_tokens": settings.memory_max_tokens * 2,
            },
        )
        resp.raise_for_status()
    return (resp.json()["choices"][0]["message"].get("content") or "").strip()


def _lock_for(conversation_id: UUID) -> asyncio.Lock:
    lock = _extract_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _extract_locks[conversation_id] = lock
    return lock


async def extract_user_memory(conversation_id: UUID, user_id: UUID) -> None:
    """后台抽取合并该会话新增消息中的稳定事实。永不抛出：失败记日志，下轮自愈重试。"""
    global _warned_no_key
    if not settings.memory_enabled:
        return
    if not web_search_available():
        if not _warned_no_key:
            logger.warning("节点[记忆停用] 未配置 ZHIPU_API_KEY，跳过长记忆抽取（读取注入不受影响）")
            _warned_no_key = True
        return

    lock = _lock_for(conversation_id)
    if lock.locked():
        return  # 同会话已有抽取在跑：跳过本轮，下一轮自愈补上
    async with lock:
        try:
            async with SessionLocal() as session:
                conversation = await session.scalar(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
                if not conversation or conversation.user_id != user_id:
                    return
                messages = (
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at)
                    )
                ).all()
                segment = list(messages)
                if conversation.memory_extracted_upto is not None:
                    for index, message in enumerate(segment):
                        if message.id == conversation.memory_extracted_upto:
                            segment = segment[index + 1 :]
                            break
                segment = segment[:_EXTRACT_SEGMENT_LIMIT]
                if not segment:
                    return
                segment_text = _render_segment(segment)
                if not segment_text:
                    # 片段里只有助手回复（用户发言为空）：跳过且不推游标，下轮与新用户消息一起处理
                    return

                row = await session.scalar(select(UserMemory).where(UserMemory.user_id == user_id))
                old_content = row.content if row else ""
                merged = _dedupe_lines(await _merge_via_glm(old_content, segment_text))
                if not merged:
                    # 空回视为模型失误：不动库、不推游标，下一轮重试同一段
                    logger.warning("节点[记忆空回] conversation=%s 下轮重试", conversation_id)
                    return
                if row is None:
                    session.add(UserMemory(user_id=user_id, content=merged))
                else:
                    row.content = merged
                conversation.memory_extracted_upto = segment[-1].id
                await session.commit()
                logger.info(
                    "节点[记忆合并] user=%s conversation=%s 条目数=%s 游标推进至=%s 内容=[%s]",
                    user_id, conversation_id, merged.count("\n") + 1, segment[-1].id,
                    " | ".join(merged.splitlines()),
                )
        except Exception:
            logger.exception("节点[记忆抽取异常] conversation=%s 下轮将自动重试", conversation_id)
