from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agent_tools import WORKSPACES_DIR
from .auth_routes import router as auth_router
from .config import settings
from .conversations import router as conversations_router
from .db import init_db
from .chat import router as chat_router
from .files import FILES_DIR, router as files_router
from .logging_config import setup_logging
from .tracing import setup_tracing, shutdown_tracing
from .workspace_picker import router as workspaces_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    setup_tracing()
    await init_db()
    logging.getLogger("friday").info("服务启动完成 model_provider=%s", settings.model_provider)
    yield
    shutdown_tracing()


app = FastAPI(title="Friday Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router, prefix="/api")
app.include_router(conversations_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(workspaces_router, prefix="/api")

# 上传的图片按 /files/<name> 直接对外提供（文件名是随机 UUID，不可枚举）
FILES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

# Agent 内置工具的产出文件按 /workspaces/<conversation_id>/<文件名> 提供：
# 目录名是会话 UUID，不可枚举，与 /files 同安全水位（无逐文件鉴权，改进项同存）
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/workspaces", StaticFiles(directory=WORKSPACES_DIR), name="workspaces")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
