"""权限确认协调：SSE 流侧挂起等待应答，确认接口侧投递结果。

CrewAI 运行时下权限 ASK 是「工具内阻塞等待」模型：执行员的工作线程在
工具里注册 PendingAsk 并阻塞在 threading.Event 上，SSE 请求协程只负责
把 permission_ask 事件推给前端并保持流不中断；确认 HTTP 接口 settle 时
置位 Event 唤醒工具线程（批准则继续执行，拒绝/超时返回拒绝结果，模型
换路完成）。前端只回布尔值，工具调用的权威副本不经过前端（防伪造）。

另存每会话的「总是允许」规则：应答 always=true 时把建议规则落进本表，
后续每轮新建权限上下文时重放——跨轮生效，进程内生命周期（重启失效，
多副本部署需换共享存储）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import UUID

from .crew_permissions import SessionRule


@dataclass
class AskAnswer:
    approved: bool
    always: bool = False


@dataclass
class PendingAsk:
    conversation_id: UUID
    user_id: UUID
    reply_id: str
    # 前端展示用摘要（id/name/query）；真实工具入参留在工具线程侧
    calls: list[dict[str, str]]
    # threading.Event：工具所在工作线程阻塞等待，确认接口（协程）置位唤醒
    event: threading.Event = field(default_factory=threading.Event)
    answer: AskAnswer | None = None


class ConfirmHub:
    """进程内确认协调器：每会话同时至多一个等待中的确认。"""

    def __init__(self) -> None:
        self._pending: dict[UUID, PendingAsk] = {}
        self._session_rules: dict[UUID, list[SessionRule]] = {}

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

    def session_rules(self, conversation_id: UUID) -> list[SessionRule]:
        return list(self._session_rules.get(conversation_id, []))

    def add_session_rules(self, conversation_id: UUID, rules: list[SessionRule]) -> None:
        bucket = self._session_rules.setdefault(conversation_id, [])
        existing = {(rule.tool_name, rule.rule_content) for rule in bucket}
        for rule in rules:
            if (rule.tool_name, rule.rule_content) not in existing:
                bucket.append(rule)


confirm_hub = ConfirmHub()
