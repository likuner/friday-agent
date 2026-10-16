"""多智能体编排单测：build_team 成员裁剪、提示词组装、事件归属标记与翻译。

不触网不落库：MCP 客户端指向不可达端口（Toolkit 列工具失败会静默降级，
Agent 构造本身不发起请求）；工作区用 pytest tmp_path。
风格沿用 test_agent_tools.py——asyncio.run 包同步用例。
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from agentscope.credential import DeepSeekCredential
from agentscope.event import EventType
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.permission import PermissionMode

from app.agent_events import AgentTagMiddleware, ReplyEventTranslator
from app.agent_factory import build_team
from app.agent_tools import build_builtin_tools
from app.schemas import ChatRequest

# 占位凭证（拼出来的非真实密钥）：DeepSeekCredential 构造要求非空 key，
# 测试全程不发起任何模型请求
_DUMMY_KEY = "test" + "-key"
_DUMMY_BASE = "https://example.invalid"


def _credential() -> DeepSeekCredential:
    return DeepSeekCredential(api_key=_DUMMY_KEY, base_url=_DUMMY_BASE)


# 指向不可达端口的 MCP 客户端：只验证挂载关系，不真正调用工具
def _mcp() -> MCPClient:
    return MCPClient(
        name="common_tools",
        is_stateful=False,
        mcp_config=HttpMCPConfig(url="http://127.0.0.1:59999/mcp", timeout=1),
        enable_tools=["medical_rag_search"],
    )


def _builtin(tmp_path):
    return build_builtin_tools(tmp_path, PermissionMode.DEFAULT)


def _team(mcps, builtin, agent_tools_prompt=""):
    """构造团队并返回（build_team 是同步函数，Agent 构造不触网）。"""
    permission_context = builtin[1] if builtin else None
    return build_team(
        _credential(),
        "deepseek-v4-pro",
        session_id=str(uuid4()),
        summary="",
        builtin_tools=builtin[0] if builtin else None,
        permission_context=permission_context,
        agent_tools_prompt=agent_tools_prompt,
        mcps=mcps,
    )


class TestBuildTeamMembers:
    """成员裁剪：检索员随 MCP、执行员随 agent_tools，leader 恒在。"""

    def test_full_team(self, tmp_path):
        team, _names = _team([_mcp()], _builtin(tmp_path), agent_tools_prompt="【Agent 内置工具】x")
        assert [m.agent.name for m in team.members.values()] == ["检索员", "执行员"]
        assert team.leader.name == "Friday"

    def test_researcher_only_without_agent_tools(self, tmp_path):
        team, _names = _team([_mcp()], None)
        assert [m.agent.name for m in team.members.values()] == ["检索员"]

    def test_executor_only_without_mcp(self, tmp_path):
        team, _names = _team(None, _builtin(tmp_path))
        assert [m.agent.name for m in team.members.values()] == ["执行员"]

    def _tool_names(self, agent) -> list[str]:
        schemas = asyncio.run(agent.toolkit.get_tool_schemas())
        return sorted(s["function"]["name"] for s in schemas)

    def test_member_toolkits(self, tmp_path):
        """检索员只挂检索（Skill 元工具 + MCP），执行员挂内置工具，leader 注册前无工具。"""
        team, _names = _team([_mcp()], _builtin(tmp_path))
        researcher_tools = self._tool_names(team.members["检索员"].agent)
        assert "Skill" in researcher_tools  # MCP 不可达时静默降级，只剩技能元工具
        executor_tools = self._tool_names(team.members["执行员"].agent)
        assert "Bash" in executor_tools and "Write" in executor_tools and "Read" in executor_tools
        # TeamAssign 由 pipeline 在首次 reply_stream 惰性注册，组装阶段 leader 无工具
        assert self._tool_names(team.leader) == []


class TestBuildTeamPrompts:
    """提示词组装：成员清单进 leader 提示词，被裁剪的成员不得出现。"""

    def test_leader_prompt_lists_members(self, tmp_path):
        team, _names = _team([_mcp()], _builtin(tmp_path))
        prompt = team.leader._system_prompt
        assert "多智能体协作" in prompt and "TeamAssign" in prompt
        assert "检索员" in prompt and "执行员" in prompt
        # 成员描述进清单（leader 路由依据）
        assert "医学文献与联网检索" in prompt and "文件与命令执行" in prompt

    def test_cropped_member_absent_from_leader_prompt(self, tmp_path):
        """调度规则不硬编码成员名：裁剪后提示词不引用不存在的成员。"""
        team, _names = _team(None, _builtin(tmp_path))
        assert "检索员" not in team.leader._system_prompt
        team2, _names2 = _team([_mcp()], None)
        assert "执行员" not in team2.leader._system_prompt

    def test_worker_prompts_compose(self, tmp_path):
        team, _names = _team([_mcp()], _builtin(tmp_path), agent_tools_prompt="【Agent 内置工具】占位")
        researcher_prompt = team.members["检索员"].agent._system_prompt
        assert "检索员" in researcher_prompt and "检索规则" in researcher_prompt
        # web_search 关闭时不追加联网搜索指令（基础提示词只有条件式提及，追加块的
        # 专属标记是工具名说明）
        assert "mcp__common_tools__web_search" not in researcher_prompt
        executor_prompt = team.members["执行员"].agent._system_prompt
        assert "执行员" in executor_prompt and "执行规则" in executor_prompt
        assert "【Agent 内置工具】占位" in executor_prompt

    def test_web_search_prompt_appended_to_researcher(self):
        team, _names = build_team(
            _credential(),
            "deepseek-v4-pro",
            session_id=str(uuid4()),
            web_search=True,
            mcps=[_mcp()],
        )
        assert "联网搜索" in team.members["检索员"].agent._system_prompt


class TestAgentAttribution:
    """事件归属：AgentTagMiddleware 打标 + ReplyEventTranslator 翻译出 agent 字段。"""

    def test_tag_middleware_registers_reply_id(self):
        names: dict[str, str] = {}
        middleware = AgentTagMiddleware(names)

        async def run():
            fake_agent = SimpleNamespace(name="检索员")
            events = [
                SimpleNamespace(reply_id="r1", type=EventType.REPLY_START),
                SimpleNamespace(reply_id="r1", delta="文本"),
                SimpleNamespace(name="最终Msg"),  # Msg 无 reply_id，跳过
            ]

            async def handler(**_kwargs):
                for event in events:
                    yield event

            seen = []
            async for event in middleware.on_reply(fake_agent, {}, handler):
                seen.append(event)
            return seen

        seen = asyncio.run(run())
        assert len(seen) == 3
        assert names == {"r1": "检索员"}

    def test_translator_resolves_agent(self):
        translator = ReplyEventTranslator({"r-leader": "Friday", "r-worker": "执行员"})
        assert translator.agent_of("r-worker") == "执行员"
        assert translator.agent_of("unknown") == "Friday"
        assert translator.agent_of(None) == "Friday"

    def test_translator_agent_field(self):
        translator = ReplyEventTranslator({"r-worker": "检索员"})
        event = SimpleNamespace(type=EventType.TEXT_BLOCK_DELTA, delta="你好", reply_id="r-worker")
        assert translator.feed(event) == {"type": "text", "content": "你好", "agent": "检索员"}
        # 单 Agent 模式（空注册表）：恒归属 Friday，协议保持一致
        single = ReplyEventTranslator()
        event2 = SimpleNamespace(type=EventType.TEXT_BLOCK_DELTA, delta="x", reply_id="any")
        assert single.feed(event2)["agent"] == "Friday"

    def test_translator_registry_is_shared_not_copied(self):
        """回归：构造时注册表为空（falsy），流式过程中才由打标中间件填充——
        翻译器必须共享同一 dict 而不是 `or {}` 换新对象。"""
        names: dict[str, str] = {}
        translator = ReplyEventTranslator(names)
        names["r1"] = "检索员"  # 模拟 AgentTagMiddleware 在事件流中打标
        event = SimpleNamespace(type=EventType.TEXT_BLOCK_DELTA, delta="x", reply_id="r1")
        assert translator.feed(event)["agent"] == "检索员"

    def test_translator_tool_chip_carries_agent(self):
        translator = ReplyEventTranslator({"r-worker": "检索员"})
        result = None
        for event in [
            SimpleNamespace(type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="medical_rag_search", reply_id="r-worker"),
            SimpleNamespace(type=EventType.TOOL_CALL_DELTA, tool_call_id="c1", delta='{"query": "高血压"}', reply_id="r-worker"),
            SimpleNamespace(type=EventType.TOOL_CALL_END, tool_call_id="c1", reply_id="r-worker"),
        ]:
            result = translator.feed(event)
        assert result == {"type": "tool_call", "name": "medical_rag_search", "query": "高血压", "agent": "检索员"}

    def test_team_assign_chip_shows_member(self):
        """leader 委派 chip 的摘要取 member 名（前端显示「已委派：检索员」）。"""
        from app.agent_events import tool_query

        assert tool_query('{"member": "检索员", "prompt": "去检索高血压文献"}') == "检索员"


class TestRequestSchema:
    def test_multi_agent_default_on(self):
        assert ChatRequest(content="hi").multi_agent is True

    def test_multi_agent_can_be_disabled(self):
        assert ChatRequest(content="hi", multi_agent=False).multi_agent is False
