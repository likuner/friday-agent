"""agent_tools.py / permissions.py 权限边界单测：deny 规则覆盖面与各模式裁决。

不触网不落库：工作区用 pytest tmp_path，Bash 只走权限判定分支不执行命令
（工具执行行为另由 TestToolExecution 覆盖）。裁决是纯同步函数，直接调用。
"""

import fnmatch
from uuid import uuid4

import pytest

from app.agent_tools import _deny_patterns, apply_session_rules, build_builtin_tools, conversation_workspace_dir
from app.graph import _tool_query
from app.permissions import PermissionMode, SessionRule, decide


def _decide(context, name: str, tool_input: dict) -> str:
    """在给定权限上下文里对某个工具做一次裁决，返回行为字符串。"""
    return decide(name, tool_input, context).behavior.value


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
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "allow"

    def test_write_outside_workspace_denied(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Write", {"file_path": "/tmp/friday-elsewhere.txt", "content": "x"}) == "deny"

    def test_write_app_source_denied_by_deny_rules(self, tmp_path):
        # deny 规则基于真实 backend 目录动态生成：应用源码必须挡在写入放行区外
        import app.agent_tools as agent_tools_module
        from pathlib import Path

        backend_app_main = Path(agent_tools_module.__file__).resolve().parent / "main.py"
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Write", {"file_path": str(backend_app_main), "content": "x"}) == "deny"

    def test_read_env_denied(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Read", {"file_path": str(tmp_path / ".env")}) == "deny"

    def test_read_normal_file_allowed(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Read", {"file_path": "/etc/hosts"}) == "allow"

    def test_task_tools_always_allowed(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "TaskCreate", {"subject": "整理数据", "description": "生成 CSV"}) == "allow"

    def test_bash_readonly_command_allowed(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "ls -la"}) == "allow"

    def test_bash_compound_readonly_allowed(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "git status && git diff"}) == "allow"

    def test_bash_redirection_not_readonly(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "ls > out.txt"}) == "deny"

    def test_bash_injection_structures_ask(self, tmp_path):
        # $(...) 等动态结构无法静态分析：DONT_ASK 下拒绝
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "echo $(whoami)"}) == "deny"

    def test_bash_dangerous_command_denied(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "rm -rf /"}) == "deny"

    def test_bash_open_denied_by_default(self, tmp_path):
        # 未配置放行前缀时，open 这类 GUI 命令落在 DONT_ASK 兜底拒绝里
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "open -a WeChat"}) == "deny"

    def test_bash_allow_prefix_grants_local_dev_escape(self, tmp_path, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "agent_tools_bash_allow_prefixes", "open -a,osascript")
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "open -a WeChat"}) == "allow"

    def test_bash_allow_prefix_does_not_override_deny(self, tmp_path, monkeypatch):
        # allow 规则在 deny/安全检查之后判定：危险命令即使撞上放行子串也必须拒绝
        from app.config import settings

        monkeypatch.setattr(settings, "agent_tools_bash_allow_prefixes", "rm")
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "rm -rf /"}) == "deny"

    def test_bash_filesystem_command_in_workspace_allowed(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": f"mkdir {tmp_path}/sub"}) == "allow"

    def test_bash_filesystem_command_outside_workspace_denied(self, tmp_path):
        _tools, context = self._setup(tmp_path)
        assert _decide(context, "Bash", {"command": "mkdir /tmp/friday-elsewhere"}) == "deny"


class TestPermissionModes:
    """请求侧模式参数：同一工作区写入在不同模式下的裁决差异。"""

    def test_default_mode_asks_for_workspace_write(self, tmp_path):
        _tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
        assert _decide(context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "ask"

    def test_accept_edits_allows_workspace_write(self, tmp_path):
        _tools, context = build_builtin_tools(tmp_path, PermissionMode.ACCEPT_EDITS)
        assert _decide(context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "allow"

    def test_explore_denies_all_writes(self, tmp_path):
        _tools, context = build_builtin_tools(tmp_path, PermissionMode.EXPLORE)
        assert _decide(context, "Write", {"file_path": str(tmp_path / "a.txt"), "content": "x"}) == "deny"

    def test_explore_allows_reads(self, tmp_path):
        _tools, context = build_builtin_tools(tmp_path, PermissionMode.EXPLORE)
        assert _decide(context, "Read", {"file_path": str(tmp_path / "a.txt")}) == "allow"
        assert _decide(context, "Bash", {"command": "ls -la"}) == "allow"

    def test_default_mode_still_denies_dangerous(self, tmp_path):
        # 危险命令是 bypass-immune 安全 ASK：DEFAULT 下是 ask（等确认），deny 规则路径仍是 deny
        _tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
        assert _decide(context, "Bash", {"command": "rm -rf /"}) == "ask"
        assert _decide(context, "Write", {"file_path": str(tmp_path / ".env"), "content": "x"}) == "deny"

    def test_session_rules_replayed_into_context(self, tmp_path):
        # 会话级「总是允许」规则重放后，DEFAULT 模式下同类调用不再询问
        _tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
        apply_session_rules(context, [SessionRule(tool_name="Bash", rule_content="python", source="session")])
        assert _decide(context, "Bash", {"command": "python gen.py"}) == "allow"

    def test_suggestions_for_bash_prefix_and_file_dir(self):
        from app.permissions import generate_suggestions

        bash_rules = generate_suggestions("Bash", {"command": "git commit -m x"})
        assert any(r.rule_content == "git commit:*" for r in bash_rules)
        file_rules = generate_suggestions("Write", {"file_path": "/ws/report.csv"})
        assert file_rules and file_rules[0].rule_content == "/ws/**"


class TestToolExecution:
    """LangChain 工具的真实执行行为（tmp_path 内操作，不触网）。"""

    def _tool(self, tools, name):
        return next(t for t in tools if t.name == name)

    def test_write_read_edit_roundtrip(self, tmp_path):
        import asyncio

        tools, _ctx = build_builtin_tools(tmp_path)
        write = self._tool(tools, "Write")
        read = self._tool(tools, "Read")
        edit = self._tool(tools, "Edit")
        target = tmp_path / "demo.txt"

        async def scenario():
            w = await write.ainvoke({"file_path": str(target), "content": "hello\nworld"})
            r = await read.ainvoke({"file_path": str(target)})
            e = await edit.ainvoke({"file_path": str(target), "old_string": "world", "new_string": "friday"})
            r2 = await read.ainvoke({"file_path": str(target)})
            return w, r, e, r2

        w, r, e, r2 = asyncio.run(scenario())
        assert "已写入" in w
        assert "1\thello" in r and "2\tworld" in r  # cat -n 行号格式
        assert "已替换 1 处" in e
        assert "friday" in r2 and "world" not in r2

    def test_edit_not_unique_fails_without_replace_all(self, tmp_path):
        import asyncio

        tools, _ctx = build_builtin_tools(tmp_path)
        write = self._tool(tools, "Write")
        edit = self._tool(tools, "Edit")
        target = tmp_path / "dup.txt"

        async def scenario():
            await write.ainvoke({"file_path": str(target), "content": "x x x"})
            return await edit.ainvoke({"file_path": str(target), "old_string": "x", "new_string": "y"})

        result = asyncio.run(scenario())
        assert result.startswith("Error") and "3 次" in result

    def test_task_store_lifecycle(self, tmp_path):
        import asyncio

        tools, _ctx = build_builtin_tools(tmp_path)
        create = self._tool(tools, "TaskCreate")
        update = self._tool(tools, "TaskUpdate")
        listing = self._tool(tools, "TaskList")
        get = self._tool(tools, "TaskGet")

        async def scenario():
            c = await create.ainvoke({"subject": "整理数据", "description": "生成 CSV"})
            u = await update.ainvoke({"task_id": "1", "status": "in_progress"})
            l = await listing.ainvoke({})
            g = await get.ainvoke({"task_id": "1"})
            return c, u, l, g

        c, u, l, g = asyncio.run(scenario())
        assert "已创建任务 1" in c
        assert "in_progress" in u
        assert "整理数据" in l
        assert "生成 CSV" in g

    def test_grep_and_glob(self, tmp_path):
        import asyncio

        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        (tmp_path / "b.md").write_text("# hello world\n")
        tools, _ctx = build_builtin_tools(tmp_path)
        grep = self._tool(tools, "Grep")
        glob = self._tool(tools, "Glob")

        async def scenario():
            g = await grep.ainvoke({"pattern": "hello", "path": str(tmp_path), "output_mode": "files_with_matches"})
            c = await grep.ainvoke({"pattern": "def", "path": str(tmp_path), "output_mode": "content"})
            f = await glob.ainvoke({"pattern": "**/*.py", "path": str(tmp_path)})
            return g, c, f

        g, c, f = asyncio.run(scenario())
        assert "a.py" in g and "b.md" in g
        assert "def hello():" in c
        assert str(tmp_path / "a.py") in f and "b.md" not in f

    def test_bash_executes_in_workspace(self, tmp_path):
        import asyncio

        tools, _ctx = build_builtin_tools(tmp_path)
        bash = self._tool(tools, "Bash")

        async def scenario():
            return await bash.ainvoke({"command": "pwd"})

        result = asyncio.run(scenario())
        assert str(tmp_path) in result

    def test_bash_timeout_kills(self, tmp_path):
        import asyncio

        tools, _ctx = build_builtin_tools(tmp_path)
        bash = self._tool(tools, "Bash")

        async def scenario():
            return await bash.ainvoke({"command": "sleep 5", "timeout": 500})

        result = asyncio.run(scenario())
        assert result.startswith("Error") and "超时" in result


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

    def test_dict_input_supported(self):
        # graph 侧直接传 dict 参数（AIMessage.tool_calls 的 args）
        assert _tool_query({"query": "哮喘 治疗", "top_k": 3}) == "哮喘 治疗"

    def test_priority_query_over_command(self):
        assert _tool_query('{"command": "ls", "query": "检索词"}') == "检索词"

    def test_non_string_value_skipped(self):
        assert _tool_query('{"query": 5, "command": "ls"}') == "ls"

    def test_truncated_to_40_chars(self):
        assert _tool_query('{"command": "%s"}' % ("a" * 100)) == "a" * 40

    def test_invalid_or_empty(self):
        assert _tool_query("not json") == ""
        assert _tool_query("") == ""
        assert _tool_query([1, 2]) == ""
