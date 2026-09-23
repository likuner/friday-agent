"""context.py 纯逻辑单测：token 估算、预算切分、游标排除、消息清洗。"""

from types import SimpleNamespace
from uuid import uuid4

from app.context import build_context_window, clean_for_replay, estimate_tokens


def make_message(role="user", content="", attachments=None, mid=None):
    return SimpleNamespace(
        id=mid or uuid4(),
        role=role,
        content=content,
        meta={"attachments": attachments or []},
    )


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_chinese(self):
        # 8 个汉字：8/1.6=5.0，+1 得 6
        assert estimate_tokens("一二三四五六七八") == 6

    def test_ascii(self):
        # 8 个英文字符：8/4=2，+1 得 3
        assert estimate_tokens("abcdefgh") == 3

    def test_mixed_not_zero(self):
        assert estimate_tokens("你好 world") > 0


class TestCleanForReplay:
    def test_plain(self):
        message = make_message(role="user", content="你好")
        assert clean_for_replay(message) == {"role": "user", "content": "你好"}

    def test_attachment_placeholder(self):
        message = make_message(role="user", content="看这张", attachments=["a.png"])
        cleaned = clean_for_replay(message)
        assert cleaned["content"] == "[图片] 看这张"

    def test_image_only(self):
        message = make_message(role="user", content="", attachments=["a.png"])
        cleaned = clean_for_replay(message)
        assert cleaned["content"] == "[图片]"  # 纯图片消息保留占位，不丢弃

    def test_empty_content_dropped(self):
        assert clean_for_replay(make_message(role="assistant", content="  ")) is None

    def test_unknown_role_dropped(self):
        assert clean_for_replay(make_message(role="tool", content="x")) is None


class TestBuildContextWindow:
    def test_short_conversation_full_replay(self):
        messages = [make_message(role="user", content="问题1"), make_message(role="assistant", content="回答1")]
        window = build_context_window(messages, None, budget_tokens=1000)
        assert [m["content"] for m in window.replay] == ["问题1", "回答1"]
        assert window.overflow == []
        assert window.overflow_tokens == 0

    def test_budget_cuts_oldest(self):
        # 每条 ≈ 63 token（100 汉字），预算 100：只放得下最新一条
        messages = [
            make_message(role="user", content="古" * 100),
            make_message(role="assistant", content="答" * 100),
            make_message(role="user", content="新" * 100),
        ]
        window = build_context_window(messages, None, budget_tokens=100)
        assert [m["content"][0] for m in window.replay] == ["新"]
        assert [m.content[0] for m in window.overflow] == ["古", "答"]
        assert window.overflow_tokens > 0

    def test_single_oversize_message_all_overflow(self):
        messages = [make_message(role="user", content="旧" * 100), make_message(role="user", content="新" * 500)]
        window = build_context_window(messages, None, budget_tokens=50)
        assert window.replay == []
        assert len(window.overflow) == 2  # 最新一条超预算也整条归入溢出，不切半条

    def test_summary_cursor_excludes_covered(self):
        cursor = uuid4()
        messages = [
            make_message(role="user", content="已压缩1", mid=uuid4()),
            make_message(role="assistant", content="已压缩2", mid=cursor),
            make_message(role="user", content="未压缩"),
        ]
        window = build_context_window(messages, cursor, budget_tokens=1000)
        # 游标及更早的消息既不回放也不进溢出段（已被摘要覆盖）
        assert [m["content"] for m in window.replay] == ["未压缩"]
        assert window.overflow == []

    def test_unknown_cursor_excludes_nothing(self):
        messages = [make_message(role="user", content="a"), make_message(role="user", content="b")]
        window = build_context_window(messages, uuid4(), budget_tokens=1000)
        assert len(window.replay) == 2

    def test_unreplayable_messages_skipped_everywhere(self):
        messages = [
            make_message(role="user", content=""),
            make_message(role="assistant", content="回答"),
            make_message(role="user", content=""),  # 空内容（中止空回复）
        ]
        window = build_context_window(messages, None, budget_tokens=10)
        assert [m["content"] for m in window.replay] == ["回答"]
        assert window.overflow == []

    def test_segments_partition(self):
        """不变量：可回放消息 = 溢出段 + 回放窗口，无重叠、顺序保持。"""
        messages = [make_message(role="user", content=f"消息{i:03d}") for i in range(10)]
        window = build_context_window(messages, None, budget_tokens=25)
        assert window.replay  # 至少回放了一条
        assert len(window.overflow) + len(window.replay) == 10
        # 窗口是新消息、溢出是旧消息
        assert window.replay[-1]["content"] == "消息009"
        assert window.overflow[0] is messages[0]

    def test_empty_input(self):
        window = build_context_window([], None, budget_tokens=100)
        assert window.replay == [] and window.overflow == []
