"""confirm.py 确认协调器单测：登记/应答/归属校验/清理 + 会话级规则。不触网。"""

import asyncio
from uuid import uuid4

from agentscope.permission import PermissionBehavior, PermissionRule

from app.confirm import AskAnswer, ConfirmHub, PendingAsk


def _pending(conversation_id, user_id):
    return PendingAsk(
        conversation_id=conversation_id,
        user_id=user_id,
        reply_id="reply-1",
        calls=[{"id": "call-1", "name": "Bash", "query": "open -a WeChat"}],
    )


class TestSettle:
    def test_settle_wakes_waiter(self):
        hub = ConfirmHub()
        cid, uid = uuid4(), uuid4()
        pending = _pending(cid, uid)
        hub.register(pending)

        async def scenario():
            settled = hub.settle(cid, uid, approved=True, always=False)
            assert settled is pending
            await asyncio.wait_for(pending.event.wait(), timeout=1)
            return pending.answer

        answer = asyncio.run(scenario())
        assert answer == AskAnswer(approved=True, always=False)
        assert hub.settle(cid, uid, approved=True) is None  # 应答后登记即出队

    def test_settle_rejects_other_user(self):
        hub = ConfirmHub()
        cid, owner = uuid4(), uuid4()
        hub.register(_pending(cid, owner))
        assert hub.settle(cid, uuid4(), approved=True) is None  # 非本人不能应答
        assert hub.settle(cid, owner, approved=True) is not None  # 等待中的请求不受影响，本人仍可应答

    def test_settle_without_pending_returns_none(self):
        hub = ConfirmHub()
        assert hub.settle(uuid4(), uuid4(), approved=True) is None

    def test_cancel_is_idempotent(self):
        hub = ConfirmHub()
        cid, owner = uuid4(), uuid4()
        hub.register(_pending(cid, owner))
        hub.cancel(cid)
        hub.cancel(cid)  # 超时清理与断开清理都可能触发，必须幂等
        assert hub.settle(cid, owner, approved=True) is None  # 已清理：应答落空

    def test_no_answer_defaults_to_deny(self):
        # 超时路径：answer 保持 None，由调用方回落为「拒绝」
        hub = ConfirmHub()
        cid = uuid4()
        pending = _pending(cid, uuid4())
        hub.register(pending)
        hub.cancel(cid)
        assert (pending.answer or AskAnswer(approved=False)).approved is False


class TestSessionRules:
    def _rule(self, content):
        return PermissionRule(
            tool_name="Bash", rule_content=content,
            behavior=PermissionBehavior.ALLOW, source="session",
        )

    def test_add_and_dedupe(self):
        hub = ConfirmHub()
        cid = uuid4()
        hub.add_session_rules(cid, [self._rule("open -a"), self._rule("python")])
        hub.add_session_rules(cid, [self._rule("open -a")])  # 同名同内容不重复
        rules = hub.session_rules(cid)
        assert [r.rule_content for r in rules] == ["open -a", "python"]

    def test_rules_are_per_conversation(self):
        hub = ConfirmHub()
        cid = uuid4()
        hub.add_session_rules(cid, [self._rule("open -a")])
        assert hub.session_rules(uuid4()) == []

    def test_session_rules_snapshot_is_copy(self):
        hub = ConfirmHub()
        cid = uuid4()
        hub.add_session_rules(cid, [self._rule("open -a")])
        snapshot = hub.session_rules(cid)
        snapshot.clear()
        assert len(hub.session_rules(cid)) == 1  # 重放方拿到的是副本，改不动内部状态
