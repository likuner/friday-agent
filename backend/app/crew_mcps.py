"""MCP 工具接入（自研直连版，CrewAI 运行时）。

不用 crewai 原生 Agent(mcps=...) 路径的两个原因：①原生工具名为
``{server}_{tool}``，会破坏提示词里引用的 ``mcp__common_tools__web_search``
既有契约；②原生执行超时硬编码 30s，而医学检索（ES 双路召回 + 智谱
embedding/rerank 各 30s）需要 90s。

行为与原 AgentScope MCPClient(is_stateful=False) 一致：
- streamable-http，每次调用一次性会话（连接-调用-关闭），并行调用无共享
  连接状态；
- 白名单过滤：medical_rag_search 恒挂载，web_search 按请求开关；
- 工具名 ``mcp__common_tools__<原始名>`` 与原版一致，提示词零调整；
- 服务端 readOnlyHint 工具自动放行（当前两个检索工具均只读）；
- 探测失败仅告警，对话降级继续（不阻塞）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from crewai.tools import BaseTool
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field, create_model

from .config import settings

logger = logging.getLogger("friday.crew_mcps")

# 工具名前缀里的服务名（与 mcp-server 的 FastMCP(name=...) 历史契约一致）
MCP_SERVER_NAME = "common_tools"

# 每调用一次性会话的超时（秒）：医学检索链路 ES 双路召回 + 智谱 embedding/rerank
# 各 30s，30s 默认会截断；sse_read_timeout 是流式读超时，给足余量。
# 注意只能传标量秒数——mcp 内部会把它与 read= 再组合成 httpx.Timeout，
# 传 httpx.Timeout 实例会在其内部触发断言。
_CALL_TIMEOUT = 90.0
_READ_TIMEOUT = 150.0


@dataclass
class MCPToolSpec:
    """一次探测到的 MCP 工具元数据（名称/描述/入参 schema/只读标记）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool


def _open_session(url: str, headers: dict[str, str]):
    return streamablehttp_client(
        url, headers=headers, timeout=_CALL_TIMEOUT, sse_read_timeout=_READ_TIMEOUT
    )


async def list_mcp_tools(enable: list[str] | None = None) -> list[MCPToolSpec]:
    """连接 mcp-server 列工具（白名单过滤）；失败抛异常由调用方降级。"""
    headers: dict[str, str] = {}
    if settings.mcp_auth_token:
        headers["Authorization"] = f"Bearer {settings.mcp_auth_token}"
    async with _open_session(settings.mcp_server_url, headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
    specs: list[MCPToolSpec] = []
    for tool in result.tools:
        if enable is not None and tool.name not in enable:
            continue
        specs.append(
            MCPToolSpec(
                name=tool.name,
                description=(tool.description or "").strip(),
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                # annotations 是 ToolAnnotations pydantic 模型（readOnlyHint 属性）
                read_only=bool(getattr(tool.annotations, "readOnlyHint", False)),
            )
        )
    return specs


def probe_mcp_tools(specs: list[MCPToolSpec]) -> None:
    """注册探测日志：服务端不可达/白名单过滤后为空时，这条日志是主线索。"""
    logger.info(
        "节点[MCP tool注册] url=%s tools=%s", settings.mcp_server_url,
        [f"{s.name}(ro)" if s.read_only else s.name for s in specs],
    )


def _args_model(spec: MCPToolSpec) -> type[BaseModel] | None:
    """把 MCP inputSchema（JSON Schema 子集）转成 pydantic 入参模型。"""
    properties = spec.input_schema.get("properties") or {}
    required = set(spec.input_schema.get("required") or [])
    if not properties:
        return None
    fields: dict[str, Any] = {}
    type_map: dict[str, Any] = {
        "string": (str, ...),
        "integer": (int, ...),
        "number": (float, ...),
        "boolean": (bool, ...),
    }
    for key, meta in properties.items():
        ann = type_map.get(str(meta.get("type", "string")), (str, ...))
        default = ... if key in required else meta.get("default", None)
        fields[key] = (ann[0], default)
    try:
        return create_model(f"{spec.name}_args", **fields)
    except Exception:  # noqa: BLE001 - schema 过于复杂时退化为无 schema
        logger.warning("MCP 工具 %s 的 inputSchema 转换失败，按无 schema 注册", spec.name)
        return None


class MCPTool(BaseTool):
    """单次调用的 MCP 工具壳：每 _run 新开会话执行（无共享连接状态）。"""

    spec_name: str = Field(default="", exclude=True)
    server_url: str = Field(default="", exclude=True)
    auth_header: str = Field(default="", exclude=True)
    call_timeout: float = Field(default=90.0, exclude=True)

    def _run(self, **kwargs: Any) -> str:
        return asyncio.run(self._acall(**kwargs))

    async def _acall(self, **kwargs: Any) -> str:
        # 过滤掉 pydantic 默认填充的空值，避免改变服务端默认行为
        payload = {k: v for k, v in kwargs.items() if v is not None}
        headers = {"Authorization": self.auth_header} if self.auth_header else {}
        try:
            async def _invoke() -> str:
                async with _open_session(self.server_url, headers) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(self.spec_name, payload)
                texts = [
                    block.text for block in (result.content or []) if getattr(block, "text", None)
                ]
                body = "\n".join(texts).strip()
                if result.isError:
                    return f"Error: MCP 工具 {self.spec_name} 返回错误：{body[:500] or '（无详情）'}"
                return body or "（工具执行成功，无文本输出）"

            return await asyncio.wait_for(_invoke(), timeout=self.call_timeout)
        except TimeoutError:
            return f"Error: MCP 工具 {self.spec_name} 执行超时（>{self.call_timeout:.0f}s）"
        except Exception as exc:  # noqa: BLE001 - 连接失败降级为工具结果，模型可换路回答
            logger.warning("节点[MCP调用失败] tool=%s error=%s", self.spec_name, exc)
            return f"Error: MCP 工具 {self.spec_name} 调用失败：{exc}"


def mcp_tools(specs: list[MCPToolSpec]) -> list[MCPTool]:
    """把探测到的工具规格包成 CrewAI 工具（命名保持 mcp__common_tools__*）。"""
    auth = f"Bearer {settings.mcp_auth_token}" if settings.mcp_auth_token else ""
    tools: list[MCPTool] = []
    for spec in specs:
        tool = MCPTool(
            name=f"mcp__{MCP_SERVER_NAME}__{spec.name}",
            description=spec.description or f"MCP 工具 {spec.name}",
            spec_name=spec.name,
            server_url=settings.mcp_server_url,
            auth_header=auth,
        )
        model = _args_model(spec)
        if model is not None:
            tool.args_schema = model
        tools.append(tool)
    return tools
