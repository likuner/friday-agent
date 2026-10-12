"""服务入口：日志走 stderr（stdio 模式下 stdout 是 JSON-RPC 通道，绝不能污染），
按配置选择传输运行。import app.tools 触发全部工具注册。
"""

import logging
import sys

from .config import settings
from .server import mcp


def _setup_logging() -> None:
    level = logging.DEBUG if settings.mcp_transport == "stdio" else logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    _setup_logging()
    import app.tools  # noqa: F401  触发 @mcp.tool 注册

    if settings.mcp_transport == "http":
        logging.getLogger("mcp-server").info(
            "启动 http 模式 %s:%s auth=%s",
            settings.mcp_host, settings.mcp_port, "on" if settings.auth_token_list else "off",
        )
        mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
