from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from . import models  # noqa: F401  确保模型注册到 metadata

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all 只建新表，不改已存在的表：会话记忆两列在此幂等补齐（等价于一次性迁移）
        await connection.execute(
            text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary TEXT")
        )
        await connection.execute(
            text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary_upto_message_id UUID")
        )
        await connection.execute(
            text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS memory_extracted_upto UUID")
        )
