"""图片上传：文件落在 backend/files，仅接受真实图片内容（不信客户端后缀与 Content-Type）。"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from .auth import current_user
from .models import User

logger = logging.getLogger("friday.files")

FILES_DIR = Path(__file__).resolve().parent.parent / "files"
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Pillow 嗅探出的图片格式 -> 落盘后缀
IMAGE_FORMATS: dict[str, str] = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}
# 后缀 -> MIME（发给多模态模型时用）
EXT_MEDIA: dict[str, str] = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}
# 只接受本服务生成的文件名，杜绝路径穿越
SAFE_NAME = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif|bmp)$")

router = APIRouter(prefix="/files", tags=["files"])


def resolve_stored_image(name: str) -> tuple[Path, str] | None:
    """把客户端传来的附件名解析成本地路径 + MIME；非法或不存在返回 None。"""
    if not SAFE_NAME.match(name or ""):
        return None
    path = FILES_DIR / name
    if not path.is_file():
        return None
    return path, EXT_MEDIA[name.rsplit(".", 1)[1]]


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_image(file: UploadFile = File(...), user: User = Depends(current_user)) -> dict[str, object]:
    """上传一张图片，返回落盘文件名、访问 URL 与大小。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB",
        )
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            probe.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="只支持图片文件") from exc

    ext = IMAGE_FORMATS.get(fmt)
    if not ext:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"不支持的图片格式：{fmt or '未知'}")

    name = f"{uuid4().hex}.{ext}"
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    (FILES_DIR / name).write_bytes(raw)
    logger.info("节点[图片上传] user=%s name=%s size=%s format=%s", user.username, name, len(raw), fmt)
    return {"name": name, "url": f"/files/{name}", "size": len(raw)}
