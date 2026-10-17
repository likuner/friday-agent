"""多智能体编排单测（CrewAI 版）：build_team 成员裁剪、提示词组装、
事件归属路由与翻译。

不触网不落库：MCP 工具用指向不可达端口的 MCPTool 占位（构造不发请求）；
工作区用 pytest tmp_path；LLM 用占位密钥（构造不发请求）。
"""

import asyncio
from uuid import uuid4

import pytest

from app.crew_mcps import MCPTool
from app.crew_permissions import PermissionMode
from app.crew_tools import build_builtin_tools
from app.schemas import ChatRequest


@pytest.fixture(autouse=True)
def _dummy_llm_key(monkeypatch):
    # DeepSeekCompletion 构造要求非空 api_key（测试全程不发起任何模型请求）
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://example.invalid")


def _mcp() -> MCPTool:
    # 指向不可达端口的 MCP 工具占位：只验证挂载关系，不真正调用
    return MCPTool(
        name="mcp__common_tools__medical_rag_search",
        description="医学文献检索",
        spec_name="medical_rag_search",
        server_url="http://127.0.0.1:59999/mcp",
        auth_header="",
    )


def _builtin(tmp_path):
    return build_builtin_tools(tmp_path, PermissionMode.DEFAULT)


def _team(mcps, builtin, agent_tools_prompt=""):
    from app.crew_agents import build_team

    return build_team(
        "deepseek-v4-pro", False, [{"role": "user", "content": "hi"}], "你好",
        agent_tools_prompt=agent_tools_prompt,
        builtin_tools=builtin[0] if builtin else None,
        mcp_tool_list=mcps or None,
    )


class TestBuildTeamMembers:
    """成员裁剪：检索员随 MCP、执行员随 agent_tools，leader 恒在。"""

    def test_full_team(self, tmp_path):
        crew, ids = _team([_mcp()], _builtin(tmp_path), agent_tools_prompt="【Agent 内置工具】x")
        assert [a.role for a in crew.agents] == ["Friday", "检索员", "执行员"]
        assert len(ids) == 3  # 事件路由键 = 全部 agent_id

    def test_researcher_only_without_agent_tools(self, tmp_path):
        crew, _ids = _team([_mcp()], None)
        assert [a.role for a in crew.agents] == ["Friday", "检索员"]

    def test_executor_only_without_mcp(self, tmp_path):
        crew, _ids = _team(None, _builtin(tmp_path))
        assert [a.role for a in crew.agents] == ["Friday", "执行员"]

    def _tool_names(self, agent) -> list[str]:
        return sorted(t.name for t in agent.tools)

    def test_member_tools(self, tmp_path):
        """检索员只挂检索（Skill 元工具 + MCP），执行员挂内置工具，leader 只拿 TeamAssign。"""
        crew, _ids = _team([_mcp()], _builtin(tmp_path))
        researcher_tools = self._tool_names(crew.agents[1])
        assert "mcp__common_tools__medical_rag_search" in researcher_tools
        assert "Skill" in researcher_tools
        executor_tools = self._tool_names(crew.agents[2])
        assert "Bash" in executor_tools and "Write" in executor_tools and "Read" in executor_tools
        assert self._tool_names(crew.agents[0]) == ["TeamAssign"]

    def test_team_assign_unknown_member_errors(self, tmp_path):
        """委派未知成员返回错误文案（模型可换路），不抛异常。"""
        crew, _ids = _team([_mcp()], _builtin(tmp_path))
        assign = crew.agents[0].tools[0]
        assert assign._run(member="不存在", prompt="x").startswith("Error")


class TestBuildTeamPrompts:
    """提示词组装（CrewAI 的 backstory 承载 system 内容）：成员清单进 leader，被裁剪的成员不得出现。"""

    def test_leader_prompt_lists_members(self, tmp_path):
        crew, _ids = _team([_mcp()], _builtin(tmp_path))
        backstory = crew.agents[0].backstory
        assert "多智能体协作" in backstory and "TeamAssign" in backstory
        assert "检索员" in backstory and "执行员" in backstory
        # 成员描述进清单（leader 路由依据）
        assert "医学文献与联网检索" in backstory and "文件与命令执行" in backstory

    def test_cropped_member_absent_from_leader_prompt(self, tmp_path):
        """调度规则不硬编码成员名：裁剪后提示词不引用不存在的成员。"""
        crew, _ids = _team(None, _builtin(tmp_path))
        assert "检索员" not in crew.agents[0].backstory
        crew2, _ids2 = _team([_mcp()], None)
        assert "执行员" not in crew2.agents[0].backstory

    def test_worker_prompts_compose(self, tmp_path):
        crew, _ids = _team([_mcp()], _builtin(tmp_path), agent_tools_prompt="【Agent 内置工具】占位")
        researcher_prompt = crew.agents[1].backstory
        assert "检索规则" in researcher_prompt
        # web_search 关闭时不追加联网搜索指令（追加块的专属标记是模型侧工具名）
        assert "mcp_common_tools_web_search" not in researcher_prompt
        executor_prompt = crew.agents[2].backstory
        assert "执行规则" in executor_prompt
        assert "【Agent 内置工具】占位" in executor_prompt

    def test_web_search_prompt_appended_to_researcher(self):
        from app.crew_agents import build_team

        crew, _ids = build_team(
            "deepseek-v4-pro", False, [], "你好",
            web_search=True, mcp_tool_list=[_mcp()],
        )
        assert "mcp_common_tools_web_search" in crew.agents[1].backstory

    def test_history_and_now_in_task(self, tmp_path):
        """回放窗口拼进 Task description，成员提示词带当前北京时间。"""
        from app.crew_agents import task_description

        desc = task_description(
            [{"role": "user", "content": "上次说到北京"}, {"role": "assistant", "content": "好的"}],
            "继续",
        )
        assert "上次说到北京" in desc and "继续" in desc
        crew, _ids = _team([_mcp()], None)
        assert "当前时间" in crew.agents[1].backstory


class TestEventAttribution:
    """事件归属：全局总线 → SSEEventBridge（按 agent_id 路由）→ StreamHub。"""

    def test_bridge_routes_and_translates(self, tmp_path):
        """成员的 token 增量带成员归属；非本请求 agent 的事件被丢弃。"""
        from crewai.events.event_bus import crewai_event_bus
        from crewai.events.types.llm_events import LLMStreamChunkEvent, LLMThinkingChunkEvent
        from crewai.events.types.tool_usage_events import ToolUsageStartedEvent
        from datetime import datetime

        from app.crew_events import SSEEventBridge, StreamHub

        crew, ids = _team([_mcp()], _builtin(tmp_path))
        researcher_id = str(crew.agents[1].id)
        outsider_id = str(uuid4())

        async def scenario():
            hub = StreamHub()
            hub.bind()
            bridge = SSEEventBridge(hub, ids)
            try:
                crewai_event_bus.emit(None, event=LLMStreamChunkEvent(
                    chunk="你好", call_id="c1", agent_id=researcher_id, agent_role="检索员"))
                crewai_event_bus.emit(None, event=LLMThinkingChunkEvent(
                    chunk="思考中", call_id="c2", agent_id=researcher_id, agent_role="检索员"))
                crewai_event_bus.emit(None, event=ToolUsageStartedEvent(
                    tool_name="mcp__common_tools__medical_rag_search",
                    tool_args={"query": "哮喘 治疗"},
                    agent_id=researcher_id, agent_role="检索员"))
                # 无归属事件（如异步摘要调用）与外部请求事件：丢弃
                crewai_event_bus.emit(None, event=LLMStreamChunkEvent(
                    chunk="外部", call_id="c3", agent_id=outsider_id, agent_role="Friday"))
                crewai_event_bus.emit(None, event=LLMStreamChunkEvent(
                    chunk="无归属", call_id="c4"))
                crewai_event_bus.flush(timeout=5)  # 工具类事件经线程池异步分发
                hub.close()
                return [event async for event in hub.events()]
            finally:
                bridge.close()

        events = asyncio.run(scenario())
        assert events == [
            {"type": "text", "content": "你好", "agent": "检索员"},
            {"type": "thinking", "content": "思考中", "agent": "检索员"},
            {"type": "tool_call", "name": "mcp__common_tools__medical_rag_search", "query": "哮喘 治疗", "agent": "检索员"},
        ]

    def test_tool_call_argument_delta_not_leaked_as_text(self, tmp_path):
        """工具参数增量（tool_call 非空）不进 text 流：chip 由 ToolUsageStarted 统一发。"""
        from crewai.events.event_bus import crewai_event_bus
        from crewai.events.types.llm_events import FunctionCall, LLMStreamChunkEvent, LLMCallType, ToolCall

        from app.crew_events import SSEEventBridge, StreamHub

        crew, ids = _team([_mcp()], None)
        leader_id = str(crew.agents[0].id)

        async def scenario():
            hub = StreamHub()
            hub.bind()
            bridge = SSEEventBridge(hub, ids)
            try:
                crewai_event_bus.emit(None, event=LLMStreamChunkEvent(
                    chunk='{"member": "检索员"',
                    tool_call=ToolCall(index=0, function=FunctionCall(arguments='{"member": "检索员"', name="TeamAssign")),
                    call_type=LLMCallType.TOOL_CALL,
                    call_id="c1", agent_id=leader_id, agent_role="Friday"))
                hub.close()
                return [event async for event in hub.events()]
            finally:
                bridge.close()

        assert asyncio.run(scenario()) == []


class TestSingleAgentPath:
    def test_single_agent_full_tools(self, tmp_path):
        from app.crew_agents import build_single

        crew, ids = build_single(
            "deepseek-v4-pro", False, [], "你好",
            builtin_tools=_builtin(tmp_path)[0], mcp_tool_list=[_mcp()],
        )
        names = [t.name for t in crew.agents[0].tools]
        assert crew.agents[0].role == "Friday" and len(ids) == 1
        assert "Bash" in names and "Skill" in names and "mcp__common_tools__medical_rag_search" in names
        assert "TeamAssign" not in names  # 单 Agent 模式无委派工具


class TestRequestSchema:
    def test_multi_agent_default_on(self):
        assert ChatRequest(content="hi").multi_agent is True

    def test_multi_agent_can_be_disabled(self):
        assert ChatRequest(content="hi", multi_agent=False).multi_agent is False
