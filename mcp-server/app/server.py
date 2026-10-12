"""FastMCP 实例：服务名、说明与鉴权在此组装；不 import 任何工具模块（避免循环依赖）。

工具注册由 app/tools 包完成：tools/__init__.py 会 import 全部子模块，
子模块内 from app.server import mcp 后用 @mcp.tool 装饰即完成注册。
"""

from fastmcp import FastMCP

from .auth import build_auth

mcp = FastMCP(
    name="common-tools",
    instructions=(
        "通用公共工具服务（无状态）：联网搜索等公共能力集中在此，"
        "供 agent 后端与本地 MCP 客户端复用。"
    ),
    auth=build_auth(),
)
