from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    conversations: Mapped[list[Conversation]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    # 会话级记忆：滚动摘要 + 摘要进度游标（已覆盖到哪条消息；早于游标的消息不再回放）
    summary: Mapped[str | None] = mapped_column(Text)
    summary_upto_message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    # 用户级记忆：抽取进度游标（该会话已抽取到哪条消息；之后的新消息攒够阈值触发下一次抽取）
    memory_extracted_upto: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")
    # 一对一设置（工作区/权限模式）；列表与详情一次性带出，免逐会话查询
    setting: Mapped[ConversationSetting | None] = relationship(cascade="all, delete-orphan", uselist=False)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class UserMemory(Base):
    """用户长期记忆：每用户一行聚合文本（"- " 分行的条目清单）。

    抽取时由 GLM 把「旧记忆 + 新消息段」合并重写全文——去重、新信息覆盖
    旧信息、体积由提示词限定。无向量召回，每轮全量注入 system prompt。
    """

    __tablename__ = "user_memories"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ConversationSetting(Base):
    """会话设置：自定义工作区 + 会话记住的权限模式。

    新表而非给 conversations 加列：项目用 create_all 建表，不会 ALTER 已有表；
    每会话至多一行，无行 = 全部默认值（工作区回落 backend/workspaces/<会话id>/）。
    """

    __tablename__ = "conversation_settings"

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True,
    )
    # Agent 内置工具的工作区根目录（绝对路径，本机部署下即用户自选的本地目录）
    workspace_root: Mapped[str | None] = mapped_column(Text)
    # 会话记住的权限模式（default/accept_edits/explore/dont_ask；空 = 尚未选择，按 default）
    permission_mode: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
