from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

# 上传的图片与 Agent 工具产出文件不再走匿名 StaticFiles 挂载，
# 分别由 GET /api/files/{name}（登录校验）与
# GET /api/workspaces/{conversation_id}/{path}（登录 + 会话归属校验）提供
FILES_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
