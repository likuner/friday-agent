"""graph.py 执行图单测：流式事件翻译、工具执行回填、interrupt/resume 与节点日志。

替代原 test_agent_logging.py（AgentScope 中间件钩子测试）：假件为脚本化
ChatModel（按脚本顺序吐 AIMessage，支持 bind_tools / astream 分片输出），
不触网；权限裁决走真实 permissions 引擎，工具走真实实现（tmp_path 工作区）。
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from app.agent_tools import build_builtin_tools
from app.graph import build_graph
from app.permissions import PermissionMode


class ScriptedChatModel(BaseChatModel):
    """按脚本顺序吐消息的假模型：文本分两片流式，工具调用走 tool_call_chunks。"""

    script: list
    step: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self.script[min(self.step, len(self.script) - 1)]
        self.step += 1
        return ChatResult(generations=[{"message": message}])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        message = self.script[min(self.step, len(self.script) - 1)]
        self.step += 1
        reasoning = message.additional_kwargs.get("reasoning_content", "")
        if reasoning:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": reasoning}))
        if isinstance(message.content, str) and message.content:
            half = len(message.content) // 2
            for part in (message.content[:half], message.content[half:]):
                yield ChatGenerationChunk(message=AIMessageChunk(content=part))
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": call["name"], "args": json.dumps(call["args"]), "id": call["id"], "index": idx}
                        for idx, call in enumerate(message.tool_calls)
                    ],
                )
            )

    @property
    def _llm_type(self) -> str:
        return "scripted"


async def _echo(query: str) -> str:
    return f"检索结果：{query}"


def _make_tools(tmp_path):
    tools, context = build_builtin_tools(tmp_path, PermissionMode.DEFAULT)
    return [StructuredTool.from_function(coroutine=_echo, name="medical_rag_search", description="test echo")] + tools, context


async def _collect(graph, inputs, config):
    """跑一遍 astream，返回 (custom 事件列表, interrupt payload 或 None)。"""
    events, interrupt_payload = [], None
    async for mode, chunk in graph.astream(inputs, config, stream_mode=["custom", "updates"]):
        if mode == "custom":
            events.append(chunk)
        elif isinstance(chunk, dict) and "__interrupt__" in chunk:
            interrupt_payload = chunk["__interrupt__"][0].value
    return events, interrupt_payload


def _run(coro):
    return asyncio.run(coro)


def _sse(events):
    """过滤出会进 SSE 的事件（usage 是内部累计事件）。"""
    return [e for e in events if e.get("type") != "usage"]


class TestStreamingTranslation:
    def test_text_and_thinking_deltas(self, tmp_path):
        tools, _context = _make_tools(tmp_path)
        script = [AIMessage(content="最终回答", additional_kwargs={"reasoning_content": "先想想"})]
        graph = build_graph(ScriptedChatModel(script=script), tools, None)
        events, interrupt = _run(
            _collect(graph, {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}})
        )
        assert interrupt is None
        thinking = "".join(e["content"] for e in events if e["type"] == "thinking")
        text = "".join(e["content"] for e in events if e["type"] == "text")
        assert thinking == "先想想"
        assert text == "最终回答"

    def test_tool_call_chip_after_args_complete(self, tmp_path):
        tools, _context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "medical_rag_search", "args": {"query": "哮喘 治疗"}, "id": "c1"}]),
            AIMessage(content="done"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, None)
        events, _interrupt = _run(
            _collect(graph, {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}})
        )
        chips = [e for e in _sse(events) if e["type"] == "tool_call"]
        assert chips == [{"type": "tool_call", "name": "medical_rag_search", "query": "哮喘 治疗"}]
        # 工具结果已回填，模型第二轮给最终回答
        assert "".join(e["content"] for e in events if e["type"] == "text") == "done"

    def test_usage_events_accumulate_rounds(self, tmp_path):
        tools, _context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "medical_rag_search", "args": {"query": "x"}, "id": "c1"}]),
            AIMessage(content="ok"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, None)
        events, _interrupt = _run(
            _collect(graph, {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}})
        )
        usage = [e for e in events if e["type"] == "usage"]
        assert len(usage) == 2  # 每轮模型调用一条


class TestPermissionInterrupt:
    def test_ask_interrupts_and_resume_approved_executes(self, tmp_path):
        tools, context = _make_tools(tmp_path)
        (tmp_path / "gen.py").write_text("print('report')\n")
        script = [
            AIMessage(content="", tool_calls=[{"name": "Bash", "args": {"command": "python3 gen.py", "description": "跑脚本"}, "id": "c2"}]),
            AIMessage(content="已执行完毕"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, context)
        config = {"configurable": {"thread_id": "t"}}
        events, payload = _run(_collect(graph, {"messages": [HumanMessage(content="hi")]}, config))
        assert isinstance(payload, dict)
        assert payload["calls"] == [{"id": "c2", "name": "Bash", "query": "python3 gen.py"}]
        assert any(s["tool_name"] == "Bash" and s["rule_content"] == "python3 gen.py:*" for s in payload["suggestions"])
        # 批准续跑：命令真实执行（python3 输出 report）
        events2, _interrupt2 = _run(_collect(graph, Command(resume={"approved": True, "always": False}), config))
        state = _run(graph.aget_state(config))
        tool_messages = [m for m in state.values["messages"] if m.type == "tool"]
        assert len(tool_messages) == 1
        assert "report" in tool_messages[0].content
        assert "".join(e["content"] for e in events2 if e["type"] == "text") == "已执行完毕"

    def test_resume_denied_synthesizes_rejection(self, tmp_path):
        tools, context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "Bash", "args": {"command": "python3 gen.py", "description": ""}, "id": "c2"}]),
            AIMessage(content="那我换个方式"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, context)
        config = {"configurable": {"thread_id": "t"}}
        _events, payload = _run(_collect(graph, {"messages": [HumanMessage(content="hi")]}, config))
        assert isinstance(payload, dict)
        events2, _interrupt2 = _run(_collect(graph, Command(resume={"approved": False, "always": False}), config))
        state = _run(graph.aget_state(config))
        tool_messages = [m for m in state.values["messages"] if m.type == "tool"]
        assert "权限请求被拒绝" in tool_messages[0].content
        assert "".join(e["content"] for e in events2 if e["type"] == "text") == "那我换个方式"

    def test_rule_denied_call_skips_interrupt(self, tmp_path):
        # deny 规则直接拒绝：不 interrupt，模型立即收到拒绝回填
        tools, context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "Read", "args": {"file_path": str(tmp_path / ".env")}, "id": "c1"}]),
            AIMessage(content="读不了，换一个"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, context)
        config = {"configurable": {"thread_id": "t"}}
        events, payload = _run(_collect(graph, {"messages": [HumanMessage(content="hi")]}, config))
        assert payload is None  # deny 不走人工确认
        state = _run(graph.aget_state(config))
        tool_messages = [m for m in state.values["messages"] if m.type == "tool"]
        assert "权限请求被拒绝" in tool_messages[0].content
        assert "".join(e["content"] for e in events if e["type"] == "text") == "读不了，换一个"

    def test_mixed_calls_single_interrupt_covers_asks(self, tmp_path):
        # 一个自动放行 + 一个 ASK：interrupt 只包含 ASK 的调用
        tools, context = _make_tools(tmp_path)
        script = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "medical_rag_search", "args": {"query": "哮喘"}, "id": "c1"},
                    {"name": "Bash", "args": {"command": "python3 gen.py", "description": ""}, "id": "c2"},
                ],
            ),
            AIMessage(content="都处理完了"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, context)
        config = {"configurable": {"thread_id": "t"}}
        events, payload = _run(_collect(graph, {"messages": [HumanMessage(content="hi")]}, config))
        assert isinstance(payload, dict)
        assert [c["id"] for c in payload["calls"]] == ["c2"]
        # 首轮 chips 两个都下发（模型已决定调用），检索已执行
        chips = [e for e in _sse(events) if e["type"] == "tool_call"]
        assert [c["name"] for c in chips] == ["medical_rag_search", "Bash"]
        _events2, _interrupt2 = _run(_collect(graph, Command(resume={"approved": True, "always": False}), config))
        state = _run(graph.aget_state(config))
        tool_messages = [m for m in state.values["messages"] if m.type == "tool"]
        assert len(tool_messages) == 2  # 检索结果 + 批准后的命令结果


class TestGraphLogs:
    def test_node_logs_emitted(self, tmp_path, caplog):
        tools, _context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "medical_rag_search", "args": {"query": "哮喘"}, "id": "c1"}]),
            AIMessage(content="ok"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, None)
        with caplog.at_level(logging.INFO, logger="friday.agent"):
            _run(_collect(graph, {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}}))
        messages = [r.getMessage() for r in caplog.records]
        assert any("节点[模型调用]" in m and "round=1" in m for m in messages)
        assert any("节点[模型返回]" in m and "input_tokens=" in m for m in messages)
        assert any("节点[工具调用]" in m and "medical_rag_search" in m and "哮喘" in m for m in messages)
        assert any("节点[工具结果]" in m and "state=success" in m and "耗时=" in m for m in messages)

    def test_tool_error_logs_warning(self, tmp_path, caplog):
        tools, _context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "Read", "args": {"file_path": str(tmp_path / "missing.txt")}, "id": "c1"}]),
            AIMessage(content="ok"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, None)
        with caplog.at_level(logging.WARNING, logger="friday.agent"):
            _run(_collect(graph, {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}}))
        records = [r for r in caplog.records if "节点[工具结果]" in r.getMessage()]
        assert records and records[0].levelno == logging.WARNING
        assert "state=error" in records[0].getMessage()


class TestMultiRound:
    def test_three_round_react_loop(self, tmp_path):
        tools, _context = _make_tools(tmp_path)
        script = [
            AIMessage(content="", tool_calls=[{"name": "medical_rag_search", "args": {"query": "哮喘 症状"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "medical_rag_search", "args": {"query": "哮喘 治疗"}, "id": "c2"}]),
            AIMessage(content="综合两次检索：哮喘可控。"),
        ]
        graph = build_graph(ScriptedChatModel(script=script), tools, None)
        events, interrupt = _run(
            _collect(graph, {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}})
        )
        assert interrupt is None
        chips = [e for e in _sse(events) if e["type"] == "tool_call"]
        assert [c["query"] for c in chips] == ["哮喘 症状", "哮喘 治疗"]
        assert len([e for e in events if e["type"] == "usage"]) == 3
        assert "".join(e["content"] for e in events if e["type"] == "text") == "综合两次检索：哮喘可控。"
