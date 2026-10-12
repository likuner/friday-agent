"""统一日志配置：backend / mcp-server 共用。

约定（与 start.sh 的目录布局对齐）：
- 仓库根 ``logs/`` 收全部日志：本模块写滚动业务日志 ``logs/{service}.log``，
  start.sh 的 nohup 重定向写控制台镜像 ``logs/{service}.console.log``
  （uvicorn access log / banner / 崩溃 traceback 等，重启截断）
- 只配置各服务自己的业务 logger（backend="friday"、mcp-server="mcp-server"），
  框架日志（uvicorn / fastmcp / httpx）不进滚动文件，只在控制台镜像出现
- console handler 固定走 stderr：mcp-server stdio 模式下 stdout 是 JSON-RPC
  通道，绝不能污染；文件 handler 与传输方式无关，stdio 模式也有持久日志

日志目录按本文件位置上溯仓库根定位，与进程 CWD 无关（mcp-server 被任意
目录下的 MCP 客户端以 stdio 拉起也能写对位置）。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# friday_shared/logging_setup.py → parents[1]=shared → parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = _REPO_ROOT / "logs"
FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"


def setup_service_logging(
    service: str,
    logger_name: str,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> None:
    """初始化一个服务的业务日志器。

    Args:
        service: 服务名，用作日志文件名（``logs/{service}.log``），
            如 "backend" / "mcp-server"。
        logger_name: 该服务的业务 logger 名（backend="friday"、
            mcp-server="mcp-server"），只配它，不动 root / 框架 logger。
        console_level: 控制台（stderr）级别，默认 INFO。
        file_level: 滚动文件级别，默认 DEBUG（文件收全量，控制台不受影响）。
    """
    logger = logging.getLogger(logger_name)
    if logger.handlers:
        return  # 幂等：--reload 热载或重复调用时不重复添加 handler
    logger.setLevel(min(console_level, file_level))
    formatter = logging.Formatter(FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    LOG_DIR.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_DIR / f"{service}.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
