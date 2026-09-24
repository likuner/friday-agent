"""会话工作区的原生目录选择与校验。

DeepSeek Harness 式交互：后端跑在用户本机时，点「添加工作区」由后端弹
系统原生目录选择框（macOS 走 osascript 的 choose folder，Linux 走 zenity），
对话框出现在运行后端的那台机器屏幕上——本机部署即用户本人。远程部署时
对话框弹在服务器上，该形态退化为不可用（前端提示）。

选定路径存 conversation_settings.workspace_root（会话级），成为内置工具的
写自动放行区与 Bash 起始目录；deny 规则（敏感路径、应用源码）不受影响。
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends

from .agent_tools import _SENSITIVE_PATTERNS
from .auth import current_user
from .models import User

logger = logging.getLogger("friday.workspace")

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

# 进程级串行：连点「添加工作区」不至于在屏幕上摞出多个选择框
_pick_lock = asyncio.Lock()


def validate_workspace_root(path: str) -> Path | None:
    """校验自定义工作区路径：绝对路径、存在且是目录、不在敏感清单内。

    合法返回 resolve 后的 Path，否则 None（纯函数，供路由与单测共用）。
    """
    if not path or not path.strip():
        return None
    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        return None
    resolved = candidate.resolve()
    if not resolved.is_dir():
        return None
    # 敏感目录本身不可作为工作区（.git/.ssh/.gnupg 等；其子路径同样被 deny 规则封着）
    text = str(resolved)
    if any(fnmatch.fnmatch(text, pattern) for pattern in _SENSITIVE_PATTERNS):
        return None
    return resolved


def _pick_command() -> list[str] | None:
    """按平台选出原生目录选择命令；不可用返回 None。"""
    if shutil.which("osascript"):
        return [
            "osascript", "-e",
            'POSIX path of (choose folder with prompt "选择工作区目录")',
        ]
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory", "--filename=."]
    return None


async def pick_directory() -> str | None:
    """弹原生目录选择框，返回选定目录的绝对路径；取消/失败/平台不支持返回 None。"""
    command = _pick_command()
    if command is None:
        logger.warning("节点[工作区选择] 当前平台无原生目录选择工具（osascript/zenity）")
        return None
    async with _pick_lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # 用户慢慢挑目录不算超时场景，给足 10 分钟兜底防泄漏
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("节点[工作区选择] 原生选择框执行失败 error=%s", exc)
            return None
    if proc.returncode != 0:
        return None  # 用户取消（osascript 返回非零）或选择器异常
    picked = stdout.decode().strip()
    # osascript 的 POSIX path 以斜杠结尾，归一化掉
    return str(Path(picked).resolve()) if picked else None


@router.post("/pick")
async def pick_workspace(user: User = Depends(current_user)) -> dict[str, str | None]:
    """弹系统原生目录选择框（后端所在机器屏幕），返回选定路径或 null。

    返回后由前端调 PUT /conversations/{id}/workspace 落库（pick 只取路径不存储，
    用户可能切会话再决定用不用）。
    """
    path = await pick_directory()
    if path is None:
        return {"path": None}
    logger.info("节点[工作区选择] user=%s picked=%s", user.username, path)
    return {"path": path}
