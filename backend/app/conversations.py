from datetime import datetime
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .auth import current_user
from .db import get_db
from .models import Conversation, Message, User
from .schemas import ConversationCreate, ConversationDetail, ConversationSummary, MessageResponse

logger = logging.getLogger("friday.conversations")

router = APIRouter(prefix="/conversations", tags=["conversations"])


def iso(value: datetime) -> str:
    return value.isoformat() if value else ""


def summary(conversation: Conversation) -> ConversationSummary:
    last = conversation.messages[-1] if conversation.messages else None
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        created_at=iso(conversation.created_at),
        updated_at=iso(conversation.updated_at),
        message_count=len(conversation.messages),
        last_message=last.content[:100] if last else None,
    )


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    result = await db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .options(selectinload(Conversation.messages))
        .order_by(Conversation.updated_at.desc())
    )
    return [summary(item) for item in result.all()]


@router.post("", response_model=ConversationSummary)
async def create_conversation(
    payload: ConversationCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = Conversation(user_id=user.id, title=payload.title.strip() or "新的对话")
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    logger.info("节点[创建对话] user=%s conversation=%s title=%r", user.username, conversation.id, conversation.title)
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        created_at=iso(conversation.created_at),
        updated_at=iso(conversation.updated_at),
        message_count=0,
        last_message=None,
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
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
    base = summary(conversation)
    return ConversationDetail(
        **base.model_dump(),
        messages=[
            MessageResponse(
                id=item.id,
                role=item.role,
                content=item.content,
                meta=item.meta or {},
                created_at=iso(item.created_at),
            )
            for item in conversation.messages
        ],
    )


@router.patch("/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: UUID,
    payload: ConversationCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await db.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id).options(selectinload(Conversation.messages))
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")
    conversation.title = payload.title.strip() or conversation.title
    await db.commit()
    await db.refresh(conversation)
    logger.info("节点[重命名对话] user=%s conversation=%s new_title=%r", user.username, conversation.id, conversation.title)
    return summary(conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id))
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")
    await db.delete(conversation)
    await db.commit()
    logger.info("节点[删除对话] user=%s conversation=%s", user.username, conversation_id)


@router.delete("/{conversation_id}/messages/{message_id}")
async def truncate_messages(
    conversation_id: UUID,
    message_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除指定消息及其后的全部消息（「编辑重发」截断历史用），返回删除条数。

    消息按 created_at 排序，截断条件为 >= 目标消息时间；同轮对话的消息提交时间
    均有微秒级间隔，不会误删更早的消息。
    """
    target = await db.scalar(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.id == message_id,
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    result = await db.execute(
        delete(Message).where(
            Message.conversation_id == conversation_id,
            Message.created_at >= target.created_at,
        )
    )
    await db.commit()
    logger.info(
        "节点[截断消息] user=%s conversation=%s from=%s deleted=%s",
        user.username, conversation_id, message_id, result.rowcount,
    )
    return {"deleted": result.rowcount}
