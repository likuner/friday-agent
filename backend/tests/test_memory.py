"""memory.py 纯逻辑单测：行级去重、触发计数。"""

from types import SimpleNamespace
from uuid import uuid4

from app.memory import _dedupe_lines, messages_since_extract


class TestDedupeLines:
    def test_removes_duplicates_keeps_order(self):
        content = "- 姓名：李坤\n- 偏好：游泳\n- 姓名：李坤"
        assert _dedupe_lines(content) == "- 姓名：李坤\n- 偏好：游泳"

    def test_whitespace_normalized(self):
        # 行首行尾空白与空行差异不影响去重判断
        content = "- 城市：杭州 \n\n-  城市：杭州\n- 偏好：紫色"
        assert _dedupe_lines(content) == "- 城市：杭州\n- 偏好：紫色"

    def test_empty(self):
        assert _dedupe_lines("") == ""
        assert _dedupe_lines("  \n \n") == ""

    def test_different_lines_kept(self):
        content = "- 偏好：游泳\n- 偏好：喜欢游泳"
        assert _dedupe_lines(content) == content  # 字面不同不去重（交给合并模型）


class TestMessagesSinceExtract:
    @staticmethod
    def _messages(count):
        return [SimpleNamespace(id=uuid4(), role="user", content=f"m{i}") for i in range(count)]

    def test_no_cursor_counts_all(self):
        assert messages_since_extract(self._messages(5), None) == 5

    def test_cursor_position(self):
        messages = self._messages(6)
        assert messages_since_extract(messages, messages[2].id) == 3

    def test_unknown_cursor_counts_all(self):
        # 游标指向的消息已被截断删除：视为从头重抽（与抽取器切片、摘要游标自愈语义一致），
        # 否则该会话长记忆永久停止更新
        assert messages_since_extract(self._messages(5), uuid4()) == 5

    def test_cursor_at_last(self):
        messages = self._messages(3)
        assert messages_since_extract(messages, messages[-1].id) == 0
