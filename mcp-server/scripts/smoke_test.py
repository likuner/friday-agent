"""冒烟测试：stdio 内存直连（工具列表 + 真实联网搜索 + 无 key 提示）与 http 模式鉴权正反用例。

运行：.venv/bin/python scripts/smoke_test.py
依赖 .env 中的 ZHIPU_API_KEY（真实调用 GLM 联网检索）；http 用例依赖 MCP_AUTH_TOKEN。
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import fastmcp
import httpx
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

import app.tools  # noqa: F401  触发工具注册
from app.config import settings
from app.server import mcp


def _result_text(result) -> str:
    """兼容不同版本的 call_tool 结果结构，取文本。"""
    data = getattr(result, "data", None)
    if isinstance(data, str) and data:
        return data
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return content[0].text
    return str(result)


async def stdio_cases() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = sorted(tool.name for tool in tools)
        assert "web_search" in names, f"web_search 未注册，当前工具: {names}"
        print(f"[ok] stdio list_tools: {names}")

        result = await client.call_tool(
            "web_search", {"query": "智谱 AI 最新发布的 GLM 模型", "count": 3}
        )
        text = _result_text(result)
        assert "网页搜索结果" in text or "GLM 检索摘要" in text, f"返回内容异常: {text[:200]}"
        print(f"[ok] stdio 真实联网搜索成功，返回 {len(text)} 字符（含来源列表）")

    original_key = settings.zhipu_api_key
    settings.zhipu_api_key = ""
    try:
        async with Client(mcp) as client:
            result = await client.call_tool("web_search", {"query": "测试"})
            text = _result_text(result)
            assert "ZHIPU_API_KEY" in text, f"无 key 提示异常: {text[:200]}"
            print("[ok] stdio 未配置 key 时返回可读提示")
    finally:
        settings.zhipu_api_key = original_key


async def _wait_http_ready(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as probe:
        while time.monotonic() < deadline:
            try:
                await probe.get(url)
                return  # 任何 HTTP 响应（含 4xx/405）都说明端口已就绪
            except httpx.TransportError:
                await asyncio.sleep(0.3)
    raise TimeoutError(f"http 服务 {timeout}s 内未就绪: {url}")


async def http_cases() -> None:
    if not settings.auth_token_list:
        print("[skip] 未配置 MCP_AUTH_TOKEN，跳过 http 鉴权用例")
        return
    token = settings.auth_token_list[0]
    url = f"http://127.0.0.1:{settings.mcp_port}{fastmcp.settings.streamable_http_path}"
    env = {**os.environ, "MCP_TRANSPORT": "http"}
    server = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv/bin/python"), "-m", "app"],
        cwd=PROJECT_ROOT, env=env,
    )
    try:
        await _wait_http_ready(url)
        print(f"[ok] http 服务已就绪: {url}")

        # 反例：不带 token / 错误 token 均应被拒
        for label, auth in [("无 token", None), ("错误 token", BearerAuth("wrong-token"))]:
            try:
                async with Client(url, auth=auth, timeout=10) as client:
                    await client.list_tools()
                raise AssertionError(f"{label} 竟然通过了鉴权")
            except AssertionError:
                raise
            except Exception as exc:
                print(f"[ok] http {label} 被拒绝（{type(exc).__name__}）")

        # 正例：正确 token 可列工具并真实调用
        async with Client(url, auth=BearerAuth(token), timeout=90) as client:
            tools = await client.list_tools()
            assert "web_search" in [t.name for t in tools]
            result = await client.call_tool("web_search", {"query": "Python 3.14 新特性", "count": 2})
            text = _result_text(result)
            assert "网页搜索结果" in text or "GLM 检索摘要" in text, f"返回内容异常: {text[:200]}"
            print(f"[ok] http 正确 token 调用 web_search 成功，返回 {len(text)} 字符")
    finally:
        server.terminate()
        server.wait(timeout=10)


async def main() -> None:
    await stdio_cases()
    await http_cases()
    print("\n全部冒烟用例通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
