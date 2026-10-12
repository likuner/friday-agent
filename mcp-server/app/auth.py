"""http 传输的 Bearer 鉴权：独立静态 token，与 backend 等外部系统完全解耦。

token 值只做进门比对，无签名派生、无会话落地——泄漏后改 MCP_AUTH_TOKEN 重启即完成轮换；
轮换期可在 MCP_AUTH_TOKEN 里逗号并列新旧值，消费方切换完毕再删旧值。
stdio 传输不受鉴权影响（FastMCP 仅在 http/sse 上校验）。
"""

import logging

from .config import settings

logger = logging.getLogger("mcp-server.auth")


def build_auth():
    """按 MCP_AUTH_TOKEN 构建静态 token 校验器；未配置时返回 None（不鉴权）。"""
    tokens = settings.auth_token_list
    if not tokens:
        logger.warning("未配置 MCP_AUTH_TOKEN：http 模式将不鉴权，仅建议绑定回环地址使用")
        return None
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    # 每个 token 映射一个消费方身份，便于日志与后续按消费方授权扩展
    verifier = StaticTokenVerifier(
        tokens={
            token: {"client_id": f"consumer-{index}", "scopes": []}
            for index, token in enumerate(tokens, 1)
        }
    )
    logger.info("http 鉴权已启用：%s 个有效 token", len(tokens))
    return verifier
