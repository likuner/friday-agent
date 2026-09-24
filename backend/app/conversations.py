from datetime import datetime
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .auth import current_user
from .db import get_db
from .models import Conversation, ConversationSetting, Message, User
from .schemas import ConversationCreate, ConversationDetail, ConversationPage, ConversationSummary, MessageResponse, PermissionModeSetRequest, WorkspaceSetRequest
from .workspace_picker import validate_workspace_root

logger = logging.getLogger("friday.conversations")

router = APIRouter(prefix="/conversations", tags=["conversations"])

# 分页默认页大小（前后端一致的「每页 20 条」）；上限防止一次拉爆
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _like_pattern(keyword: str) -> str:
    """搜索词 → ILIKE 模式：转义 LIKE 通配符，用户输入的 % / _ 按字面量匹配。"""
    escaped = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def iso(value: datetime) -> str:
    return value.isoformat() if value else ""


def summary(conversation: Conversation) -> ConversationSummary:
    last = conversation.messages[-1] if conversation.messages else None
    setting = conversation.setting
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        created_at=iso(conversation.created_at),
        updated_at=iso(conversation.updated_at),
        message_count=len(conversation.messages),
        last_message=last.content[:100] if last else None,
        workspace_root=setting.workspace_root if setting else None,
        permission_mode=setting.permission_mode if setting else None,
    )


@router.get("", response_model=ConversationPage)
async def list_conversations(
    q: str | None = Query(default=None, max_length=100, description="标题关键词（服务端 ILIKE 过滤，忽略大小写）"),
    kind: Literal["all", "workspace", "plain"] = Query(
        default="all", description="all=全部；workspace=有工作区的会话；plain=无工作区的普通对话",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量（第 n 页 = (n-1) * limit）"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """会话分页列表：服务端过滤（标题关键词 + 工作区分组）+ 分页，按最近更新倒序。

    侧边栏「工作区 / 对话」两组各自独立分页（kind 区分），历史记录页用 q 做服务端搜索。
    数据量大时先取总数再取一页，避免把全部会话与其消息一次性读进内存。
    """
    conditions = [Conversation.user_id == user.id]
    if q and q.strip():
        conditions.append(Conversation.title.ilike(_like_pattern(q), escape="\\"))
    if kind == "workspace":
        conditions.append(ConversationSetting.workspace_root.isnot(None))
    elif kind == "plain":
        conditions.append(ConversationSetting.workspace_root.is_(None))

    setting_join = ConversationSetting.conversation_id == Conversation.id
    total = await db.scalar(
        select(func.count())
        .select_from(Conversation)
        .outerjoin(ConversationSetting, setting_join)
        .where(*conditions)
    ) or 0
    result = await db.scalars(
        select(Conversation)
        .outerjoin(ConversationSetting, setting_join)
        .where(*conditions)
        .options(selectinload(Conversation.messages), selectinload(Conversation.setting))
        # updated_at 可能重复（同一秒建的会话）：补 id 兜底，保证 offset 分页不跳条不重条
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [summary(item) for item in result.all()]
    logger.info(
        "节点[会话列表] user=%s kind=%s q=%r offset=%s limit=%s 返回=%s 总数=%s",
        user.username, kind, q or "", offset, limit, len(items), total,
    )
    return ConversationPage(items=items, total=total, has_more=offset + len(items) < total)


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
        .options(selectinload(Conversation.messages), selectinload(Conversation.setting))
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
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id).options(selectinload(Conversation.messages), selectinload(Conversation.setting))
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


@router.put("/{conversation_id}/workspace")
async def set_workspace(
    conversation_id: UUID,
    payload: WorkspaceSetRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """设置会话工作区：**选定后不可更改**（已设置返回 409，请新建对话）。

    选定目录成为内置工具的写自动放行区与 Bash 起始目录；deny 规则不受影响。
    """
    conversation = await db.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    existing = await db.scalar(
        select(ConversationSetting).where(ConversationSetting.conversation_id == conversation_id)
    )
    if existing is not None and existing.workspace_root:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="工作区已设定，不可更改；请新建工作区",
        )

    resolved = validate_workspace_root(payload.path)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="工作区路径无效：需要存在的目录绝对路径，且不能是敏感目录",
        )
    if existing is None:
        existing = ConversationSetting(conversation_id=conversation_id, workspace_root=str(resolved))
        db.add(existing)
    else:
        existing.workspace_root = str(resolved)
    await db.commit()
    logger.info(
        "节点[工作区设置] user=%s conversation=%s workspace=%s", user.username, conversation_id, resolved
    )
    return {"workspace_root": str(resolved)}


@router.put("/{conversation_id}/permission-mode")
async def set_permission_mode(
    conversation_id: UUID,
    payload: PermissionModeSetRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """记住会话的权限模式（随时可改；chat 生效优先级：会话设置 > 请求参数 > default）。"""
    conversation = await db.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")

    setting = await db.scalar(
        select(ConversationSetting).where(ConversationSetting.conversation_id == conversation_id)
    )
    if setting is None:
        setting = ConversationSetting(conversation_id=conversation_id, permission_mode=payload.mode)
        db.add(setting)
    else:
        setting.permission_mode = payload.mode
    await db.commit()
    logger.info(
        "节点[权限模式] user=%s conversation=%s mode=%s", user.username, conversation_id, payload.mode
    )
    return {"permission_mode": payload.mode}


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
