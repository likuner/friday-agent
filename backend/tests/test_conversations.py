"""conversations.py 列表分页/搜索的工具函数单测。

分页与过滤的 SQL 行为需要 DB/HTTP 基建（项目现无），以手动验证为准；
这里只钉住纯函数与页大小约定，避免搜索词把 LIKE 通配符带进查询。
"""

from app.conversations import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, _like_pattern


class TestLikePattern:
    def test_wraps_and_trims(self):
        assert _like_pattern("  周报 ") == "%周报%"

    def test_escapes_like_wildcards(self):
        # 用户输入的 % / _ 必须按字面量匹配，不能变成通配符
        assert _like_pattern("100%_完成") == "%100\\%\\_完成%"

    def test_escapes_backslash_first(self):
        assert _like_pattern("a\\b") == "%a\\\\b%"


class TestPageSize:
    def test_default_is_20(self):
        assert DEFAULT_PAGE_SIZE == 20

    def test_max_covers_default(self):
        assert MAX_PAGE_SIZE >= DEFAULT_PAGE_SIZE
