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
from .db import SessionLocal, get_db
from .models import Conversation, Message, User
from .schemas import ChatRequest

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

    history = [{"role": item.role, "content": item.content} for item in conversation.messages]
    history.append({"role": "user", "content": payload.content})
    logger.info(
        "节点[接收消息] conversation=%s user=%s history_len=%s content=%r deep_thinking=%s web_search=%s",
        conversation.id, user.username, len(history), payload.content[:100], payload.deep_thinking, payload.web_search,
    )

    async def generate() -> AsyncIterator[str]:
        answer: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict[str, str]] = []
        event_count = 0
        started = time.monotonic()
        logger.info("节点[流式开始] conversation=%s", conversation.id)
        try:
            async for event in agent_service.stream(history, payload.deep_thinking, payload.web_search, payload.attachments):
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
