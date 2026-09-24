"""agent_tools.py 权限边界单测：deny 规则覆盖面与 DONT_ASK 模式下的引擎裁决。

不触网不落库：工作区用 pytest tmp_path，Bash 只走权限判定分支不执行命令。
风格沿用 test_agent_logging.py——asyncio.run 包同步用例。
"""

import asyncio
import fnmatch
from uuid import uuid4

import pytest
from agentscope.permission import PermissionBehavior, PermissionEngine
from agentscope.tool import ToolBase

from app.agent import _tool_query
from app.agent_tools import _deny_patterns, apply_session_rules, build_builtin_tools, conversation_workspace_dir


def _decide(tools: list[ToolBase], context, name: str, tool_input: dict) -> str:
    """在给定权限上下文里对某个内置工具做一次裁决，返回行为字符串。"""
    tool = next(t for t in tools if t.name == name)
    engine = PermissionEngine(context)
    decision = asyncio.run(engine.check_permission(tool, tool_input))
    return decision.behavior.value


class TestDenyPatterns:
    def test_sensitive_paths_covered(self):
        patterns = _deny_patterns()
        assert any(".env" in p for p in patterns)
        assert any(p.endswith(".db") for p in patterns)
        assert any(".git" in p for p in patterns)
        assert any(".ssh" in p for p in patterns)

    def test_app_source_covered(self):
        # backend/ 顶层动态枚举：源码目录必在保护范围（fnmatch 的 * 跨 /，覆盖子树）
        patterns = _deny_patterns()
        assert any(p.endswith("/app*") for p in patterns)

    def test_workspace_is_the_only_hole(self):
        # 工作区是唯一豁免：任何 deny 模式都不能命中工作区子树内的路径
        patterns = _deny_patterns()
        sample = str(conversation_workspace_dir(uuid4()) / "out.csv")
        for pattern in patterns:
            assert not fnmatch.fnmatch(sample, pattern), pattern

    def test_backend_files_still_denied(self):
        patterns = _deny_patterns()
        assert any(fnmatch.fnmatch("/x/backend/.env", p) for p in patterns)
        assert any(fnmatch.fnmatch("/home/u/proj/.ssh/config", p) for p in patterns)


class TestPermissionDecisions:
    """DONT_ASK 模式的关键裁决：工作区内写放行、敏感与越界路径拒绝、危险命令拒绝。"""

    @pytest.fixture(autouse=True)
    def _pin_allow_prefixes(self, monkeypatch):
        # 隔离开发者本机 .env：权限裁决用例默认无 Bash 放行前缀（个别用例自行覆盖）
        from app.config import settings

        monkeypatch.setattr(settings, "agent_tools_bash_allow_prefixes", "")

    def _setup(self, tmp_path):
        return build_builtin_tools(tmp_path)

    def test_write_inside_workspace_allowed(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "allow"

    def test_write_outside_workspace_denied(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Write", {"file_path": "/tmp/friday-elsewhere.txt", "content": "x"}) == "deny"

    def test_write_app_source_denied_even_in_cwd(self, tmp_path):
        # 框架把进程 cwd 也当工作目录：deny 规则必须把应用源码挡在写入放行区外。
        # deny 模式基于真实 backend 目录生成，这里取 backend/app/main.py 验证。
        import app.agent_tools as agent_tools_module
        from pathlib import Path

        backend_app_main = Path(agent_tools_module.__file__).resolve().parent / "main.py"
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Write", {"file_path": str(backend_app_main), "content": "x"}) == "deny"

    def test_read_env_denied(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Read", {"file_path": str(tmp_path / ".env")}) == "deny"

    def test_read_normal_file_allowed(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Read", {"file_path": "/etc/hosts"}) == "allow"

    def test_task_tools_always_allowed(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "TaskCreate", {"subject": "整理数据", "description": "生成 CSV"}) == "allow"

    def test_bash_readonly_command_allowed(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Bash", {"command": "ls -la"}) == "allow"

    def test_bash_dangerous_command_denied(self, tmp_path):
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Bash", {"command": "rm -rf /"}) == "deny"

    def test_bash_open_denied_by_default(self, tmp_path):
        # 未配置放行前缀时，open 这类 GUI 命令落在 DONT_ASK 兜底拒绝里
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Bash", {"command": "open -a WeChat"}) == "deny"

    def test_bash_allow_prefix_grants_local_dev_escape(self, tmp_path, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "agent_tools_bash_allow_prefixes", "open -a,osascript")
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Bash", {"command": "open -a WeChat"}) == "allow"

    def test_bash_allow_prefix_does_not_override_deny(self, tmp_path, monkeypatch):
        # allow 规则在 deny/安全检查之后判定：危险命令即使撞上放行子串也必须拒绝
        from app.config import settings

        monkeypatch.setattr(settings, "agent_tools_bash_allow_prefixes", "rm")
        tools, context = self._setup(tmp_path)
        assert _decide(tools, context, "Bash", {"command": "rm -rf /"}) == "deny"


class TestPermissionModes:
    """请求侧模式参数：同一工作区写入在不同模式下的裁决差异。"""

    def test_default_mode_asks_for_workspace_write(self, tmp_path):
        from agentscope.permission import PermissionMode

        tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
        assert _decide(tools, context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "ask"

    def test_accept_edits_allows_workspace_write(self, tmp_path):
        from agentscope.permission import PermissionMode

        tools, context = build_builtin_tools(tmp_path, PermissionMode.ACCEPT_EDITS)
        assert _decide(tools, context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "allow"

    def test_explore_denies_all_writes(self, tmp_path):
        from agentscope.permission import PermissionMode

        tools, context = build_builtin_tools(tmp_path, PermissionMode.EXPLORE)
        assert _decide(tools, context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "deny"

    def test_default_mode_still_denies_dangerous(self, tmp_path):
        # 危险命令是 bypass-immune 安全 ASK：DEFAULT 下是 ask（等确认），deny 规则路径仍是 deny
        from agentscope.permission import PermissionMode

        tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
        assert _decide(tools, context, "Bash", {"command": "rm -rf /"}) == "ask"
        assert _decide(tools, context, "Write", {"file_path": str(tmp_path / ".env"), "content": "x"}) == "deny"

    def test_session_rules_replayed_into_context(self, tmp_path):
        # 会话级「总是允许」规则重放后，DEFAULT 模式下同类调用不再询问
        from agentscope.permission import PermissionBehavior, PermissionMode, PermissionRule

        tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
        apply_session_rules(context, [
            PermissionRule(tool_name="Bash", rule_content="python", behavior=PermissionBehavior.ALLOW, source="session"),
        ])
        assert _decide(tools, context, "Bash", {"command": "python gen.py"}) == "allow"


class TestToolQuery:
    """_tool_query 泛化：各参数键的优先级取值与截断。"""

    def test_query_key_first(self):
        assert _tool_query('{"query": "哮喘 治疗"}') == "哮喘 治疗"

    def test_command_key(self):
        assert _tool_query('{"command": "python gen.py"}') == "python gen.py"

    def test_file_path_key(self):
        assert _tool_query('{"file_path": "/ws/report.csv"}') == "/ws/report.csv"

    def test_subject_key(self):
        assert _tool_query('{"subject": "整理数据"}') == "整理数据"

    def test_priority_query_over_command(self):
        assert _tool_query('{"command": "ls", "query": "检索词"}') == "检索词"

    def test_non_string_value_skipped(self):
        assert _tool_query('{"query": 5, "command": "ls"}') == "ls"

    def test_truncated_to_40_chars(self):
        assert _tool_query('{"command": "%s"}' % ("a" * 100)) == "a" * 40

    def test_invalid_or_empty(self):
        assert _tool_query("not json") == ""
        assert _tool_query("") == ""
        assert _tool_query('[1, 2]') == ""
