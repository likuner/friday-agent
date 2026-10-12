"""服务入口：控制台日志走 stderr（stdio 模式下 stdout 是 JSON-RPC 通道，绝不能污染），
按配置选择传输运行。import app.tools 触发全部工具注册。

文件日志由 friday_shared 统一配置：滚动写仓库根 logs/mcp-server.log，
与传输方式无关（stdio 模式被客户端拉起时也有持久日志）。
"""

import logging

from friday_shared import setup_service_logging

from .config import settings
from .server import mcp


def _setup_logging() -> None:
    setup_service_logging(
        service="mcp-server",
        logger_name="mcp-server",
        # 控制台级别沿用原行为：stdio 交互调试开 DEBUG，http 常驻服务用 INFO；
        # 文件始终收 DEBUG 全量
        console_level=logging.DEBUG if settings.mcp_transport == "stdio" else logging.INFO,
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
