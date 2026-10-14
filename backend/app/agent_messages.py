"""输入侧消息构造：把请求参数与库中历史转成 AgentScope 消息。

只做纯构造（多模态附件 → base64 DataBlock、回放窗口 → 纯文本消息），
不涉及 Agent 组装与流式编排（见 agent.py / agent_factory.py）。
"""

import base64
import logging

from agentscope.message import AssistantMsg, Base64Source, DataBlock, Msg, TextBlock, UserMsg

from .files import resolve_stored_image

logger = logging.getLogger("friday.agent_messages")


def user_message(content: str, attachments: list[str] | None) -> UserMsg:
    """有图片附件时构造多模态消息：图片读成 base64 直接传给模型。

    DeepSeek 的 OpenAI 兼容接口接受 image_url 形式的 data URI，
    AgentScope 侧由 OpenAIChatFormatter 负责把 DataBlock 转成该格式。
    """
    blocks: list[TextBlock | DataBlock] = []
    for name in attachments or []:
        resolved = resolve_stored_image(name)
        if not resolved:
            logger.warning("节点[附件跳过] 附件不存在或名称非法 name=%r", name)
            continue
        path, media_type = resolved
        blocks.append(
            DataBlock(
                type="data",
                source=Base64Source(
                    type="base64",
                    data=base64.b64encode(path.read_bytes()).decode(),
                    media_type=media_type,
                ),
                name=path.name,
            )
        )
    if not blocks:
        return UserMsg(name="user", content=content)
    # 只发图不打字时给模型一个默认指令
    blocks.insert(0, TextBlock(type="text", text=content or "请描述这张图片。"))
    logger.info("节点[多模态消息] images=%s text=%r", len(blocks) - 1, (content or "")[:60])
    return UserMsg(name="user", content=blocks)


def replay_msgs(replay: list[dict[str, str]]) -> list[Msg]:
    """把库中的回放窗口转成 AgentScope 消息：仅 user/assistant 纯文本（已由 context.py 清洗）。"""
    return [
        UserMsg(name="user", content=item["content"])
        if item["role"] == "user"
        else AssistantMsg(name="Friday", content=item["content"])
        for item in replay
    ]
