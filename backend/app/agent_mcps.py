"""MCP 工具挂载：公共检索工具（mcp-server）的客户端构造与可用性探测。

medical_rag_search / web_search 的实现在 mcp-server/app/tools/ 下，
backend 侧只负责注册与探测，无任何本地实现。
"""

import logging

from agentscope.mcp import HttpMCPConfig, MCPClient

from .config import settings

logger = logging.getLogger("friday.agent_mcps")


def mcp_client(enable_tools: list[str]) -> MCPClient:
    """公共工具的 MCP 客户端（mcp-server，streamable-http）。

    is_stateful=False：每次调用临时建 HTTP 会话，无需 connect/close，
    mcp-server 重启也不影响后续请求；工具只读已在服务端以 readOnlyHint
    声明（自动放行）。enable_tools 白名单注册，mcp-server 未来新增工具
    不会自动灌给模型。服务端不可达时 Toolkit 仅告警并跳过，对话退化为
    无医学检索/联网搜索工具，不会报错中断。
    """
    return MCPClient(
        name="common_tools",
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url=settings.mcp_server_url,
            headers=(
                {"Authorization": f"Bearer {settings.mcp_auth_token}"}
                if settings.mcp_auth_token
                else None
            ),
            # 需覆盖 mcp-server 内检索链路的 60s httpx 超时（GLM 联网检索、
            # RAG 的 embedding/rerank 各 30s）再留余量
            timeout=90,
        ),
        enable_tools=enable_tools,
    )


async def probe_mcp_tools(client: MCPClient) -> None:
    """实例化后先探测一次可用工具并落日志。

    Toolkit 对列工具失败只静默跳过，这条日志是排查
    「mcp-server 未启动导致检索工具悄悄消失」的主线索；探测失败不抛
    （client 仍照常挂载，由 Toolkit 侧继续静默降级）。
    """
    try:
        tool_names = [tool.name for tool in await client.list_tools()]
        logger.info(
            "节点[MCP工具注册] server=%s tools=%s",
            settings.mcp_server_url, tool_names,
        )
    except Exception as exc:
        logger.warning(
            "节点[MCP工具注册失败] server=%s error=%s（本轮对话无医学检索/联网搜索工具，继续）",
            settings.mcp_server_url, exc,
        )
