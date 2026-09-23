import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .agent import agent_service
from .auth import current_user
from .config import settings
from .context import build_context_window
from .db import SessionLocal, get_db
from .memory import extract_user_memory, load_memory_block, messages_since_extract
from .models import Conversation, Message, User
from .schemas import ChatRequest
from .summarizer import update_rolling_summary

logger = logging.getLogger("friday.chat")

router = APIRouter(prefix="/conversations", tags=["chat"])


async def _save_partial(conversation_id: UUID, content: str, meta: dict) -> None:
    """客户端中止生成时，用**独立 session** 落库已生成的部分内容。

    不能复用请求级 session：流被取消时它已随请求一起失效。
    """
    async with SessionLocal() as session:
        message = Message(conversation_id=conversation_id, role="assistant", content=content, meta=meta)
        session.add(message)
        await session.commit()
        logger.info(
            "节点[中止保存] conversation=%s message_id=%s chars=%s",
            conversation_id, message.id, len(content),
        )


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/{conversation_id}/messages")
async def chat(
    conversation_id: UUID,
    payload: ChatRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.user_id == user.id)
        .options(selectinload(Conversation.messages))
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")
    if not payload.content.strip() and not payload.attachments:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="消息内容不能为空")

    # 附件只存文件名，图片本体在 backend/files 里
    user_message = Message(conversation_id=conversation.id, role="user", content=payload.content, meta={"attachments": payload.attachments})
    db.add(user_message)
    if conversation.title == "新的对话":
        conversation.title = payload.content.strip()[:30] or "图片对话"
    await db.commit()

    # 用户级长期记忆：活跃条目全量注入 system prompt（条数/token 双上限，读取失败降级为 None）
    memory_block = await load_memory_block(db, user.id)

    # 会话级记忆：从库中装配本轮上下文 = 滚动摘要（AgentState.summary 注入）
    # + 回放窗口（预算内最近若干轮纯文本）+ 溢出段（早于窗口，待压缩）。
    # conversation.messages 是本轮用户消息落库前加载的列表，恰好为「历史」。
    window = build_context_window(
        conversation.messages, conversation.summary_upto_message_id, settings.context_token_budget
    )
    logger.info(
        "节点[接收消息] conversation=%s user=%s replay=%s条(≈%s tok) overflow=%s条(≈%s tok) 摘要=%s 长期记忆=%s content=%r deep_thinking=%s web_search=%s",
        conversation.id, user.username,
        len(window.replay), window.replay_tokens, len(window.overflow), window.overflow_tokens,
        bool(conversation.summary), bool(memory_block), payload.content[:100], payload.deep_thinking, payload.web_search,
    )
    # 溢出攒够阈值：后台异步压缩成滚动摘要落库。不阻塞本轮、不依赖回复成败，
    # 失败只记日志，下一轮发现溢出仍在会自动重试（游标幂等）。
    if window.overflow_tokens >= settings.summary_min_overflow_tokens:
        asyncio.create_task(update_rolling_summary(conversation.id))
    # 长期记忆抽取：会话游标后新增消息攒够阈值，后台异步抽取稳定事实落库（同自愈模式）
    if messages_since_extract(conversation.messages, conversation.memory_extracted_upto) >= settings.memory_extract_min_messages:
        asyncio.create_task(extract_user_memory(conversation.id, user.id))

    async def generate() -> AsyncIterator[str]:
        answer: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict[str, str]] = []
        event_count = 0
        started = time.monotonic()
        logger.info("节点[流式开始] conversation=%s", conversation.id)
        try:
            # 流首事件：回传用户消息落库后的真实 id。前端发送时用本地随机 id 乐观渲染，
            # 替换成库里的 id 后，「编辑重发」的截断接口才能按 id 定位到这条消息。
            yield sse({"type": "sent", "message_id": str(user_message.id)})
            async for event in agent_service.stream(
                conversation_id=conversation.id,
                replay=window.replay,
                summary=conversation.summary,
                content=payload.content,
                deep_thinking=payload.deep_thinking,
                web_search=payload.web_search,
                attachments=payload.attachments,
                memory_block=memory_block,
            ):
                # 思考过程单独收集，不混进正文，前端才能折叠展示
                if event.get("type") == "thinking":
                    thinking.append(event.get("content", ""))
                else:
                    answer.append(event.get("content", ""))
                event_count += 1
                if event.get("type") == "tool_call":
                    tool_calls.append({"name": event.get("name", ""), "query": event.get("query", "")})
                # 暂时关闭流式 delta 逐条事件日志（噪音大，需要时取消注释即可恢复）
                # logger.debug(
                #     "节点[模型事件] conversation=%s seq=%s type=%s delta=%r",
                #     conversation.id, event_count, event.get("type"), event.get("content", ""),
                # )
                yield sse(event)
                await asyncio.sleep(0.05)
            assistant = Message(
                conversation_id=conversation.id,
                role="assistant",
                content="".join(answer),
                # toolCalls / thinking 一并入库，刷新或重新进入会话时不会丢
                meta={
                    "deep_thinking": payload.deep_thinking,
                    "web_search": payload.web_search,
                    "toolCalls": tool_calls,
                    "thinking": "".join(thinking),
                },
            )
            db.add(assistant)
            await db.commit()
            logger.info(
                "节点[保存回复] conversation=%s message_id=%s chars=%s",
                conversation.id, assistant.id, len(assistant.content),
            )
            yield sse({"type": "done", "message_id": str(assistant.id)})
            logger.info(
                "节点[流式完成] conversation=%s events=%s chars=%s duration=%.2fs",
                conversation.id, event_count, len(assistant.content), time.monotonic() - started,
            )
        except (asyncio.CancelledError, GeneratorExit):
            # 用户点了「停止生成」：把已生成的部分落库（独立 session + shield，
            # 保证保存任务不被这次取消打断），随后继续向上传播取消。
            partial = "".join(answer)
            if partial:
                meta = {
                    "deep_thinking": payload.deep_thinking,
                    "web_search": payload.web_search,
                    "toolCalls": tool_calls,
                    "thinking": "".join(thinking),
                    "stopped": True,
                }
                task = asyncio.create_task(_save_partial(conversation.id, partial, meta))
                try:
                    await asyncio.shield(task)
                except BaseException:
                    pass  # 取消已发生，保存任务仍在后台完成
            logger.info(
                "节点[流式中止] conversation=%s events=%s chars=%s duration=%.2fs",
                conversation.id, event_count, len(partial), time.monotonic() - started,
            )
            raise
        except Exception as exc:
            logger.exception(
                "节点[流式异常] conversation=%s events=%s chars=%s duration=%.2fs error=%s",
                conversation.id, event_count, len("".join(answer)), time.monotonic() - started, exc,
            )
            await db.rollback()
            yield sse({"type": "error", "content": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
