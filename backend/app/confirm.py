"""权限确认协调：SSE 流侧挂起等待应答，确认接口侧投递结果。

AgentScope 的 ASK 是 park-and-resume 模型：reply_stream 产出
RequireUserConfirmEvent 后本轮自然结束（parked 状态留在 Agent 实例的
state 里），应答需把 UserConfirmResultEvent 作为下一次 reply_stream 的
输入喂回同一 Agent。本模块只协调「等待方（SSE 请求）」与「应答方
（确认 HTTP 接口）」的交接：SSE 请求全程保活 Agent，前端只回布尔值，
工具调用的权威副本不经过前端（防伪造，与框架官方做法一致）。

另存每会话的「总是允许」规则：应答 always=true 时把建议规则落进
本表，后续每轮新建 Agent 时重放进 permission_context——跨轮生效，
进程内生命周期（重启失效，多副本部署需换共享存储）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from agentscope.permission import PermissionRule


@dataclass
class AskAnswer:
    approved: bool
    always: bool = False


@dataclass
class PendingAsk:
    conversation_id: UUID
    user_id: UUID
    reply_id: str
    # 前端展示用摘要（id/name/query）；真实 ToolCallBlock 留在 SSE 请求侧
    calls: list[dict[str, str]]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    answer: AskAnswer | None = None


class ConfirmHub:
    """进程内确认协调器：每会话同时至多一个等待中的确认。"""

    def __init__(self) -> None:
        self._pending: dict[UUID, PendingAsk] = {}
        self._session_rules: dict[UUID, list[PermissionRule]] = {}

    def register(self, pending: PendingAsk) -> None:
        self._pending[pending.conversation_id] = pending

    def settle(
        self,
        conversation_id: UUID,
        user_id: UUID,
        approved: bool,
        always: bool = False,
    ) -> PendingAsk | None:
        """确认接口应答：校验归属后唤醒等待方；无等待请求或非本人返回 None。"""
        pending = self._pending.get(conversation_id)
        if pending is None or pending.user_id != user_id:
            return None
        pending.answer = AskAnswer(approved=approved, always=always)
        self._pending.pop(conversation_id, None)
        pending.event.set()
        return pending

    def cancel(self, conversation_id: UUID) -> None:
        """SSE 断开/超时兜底清理：只丢弃登记（应答已 pop，此处幂等）。"""
        self._pending.pop(conversation_id, None)

    # ---- 会话级「总是允许」规则（跨轮重放，进程内生命周期） ----

    def session_rules(self, conversation_id: UUID) -> list[PermissionRule]:
        return list(self._session_rules.get(conversation_id, []))

    def add_session_rules(self, conversation_id: UUID, rules: list[PermissionRule]) -> None:
        bucket = self._session_rules.setdefault(conversation_id, [])
        existing = {(rule.tool_name, rule.rule_content) for rule in bucket}
        for rule in rules:
            if (rule.tool_name, rule.rule_content) not in existing:
                bucket.append(rule)


confirm_hub = ConfirmHub()
