"""agent_logging.py 纯逻辑单测：钩子透传与节点日志断言，不触网。

假件风格沿用 test_memory.py：SimpleNamespace 模拟 agent/事件/工具结果，
异步钩子用 asyncio.run 包成同步用例；集成冒烟用假模型走真实 Agent 装配，
验证中间件挂载后钩子确实被触发。
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from agentscope.agent import Agent
from agentscope.event import EventType
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.state import AgentState

from app.agent_logging import AgentLoggingMiddleware


def _fake_agent():
    return SimpleNamespace(state=SimpleNamespace(session_id="sess-1"))


def _tool_call(name="web_search", call_id="call-1", raw_input='{"query": "哮喘"}'):
    return ToolCallBlock(id=call_id, name=name, input=raw_input)


class TestOnActing:
    def _run(self, mw, tool_call, items):
        async def next_handler(**kwargs):
            for item in items:
                yield item

        async def scenario():
            out = []
            async for item in mw.on_acting(_fake_agent(), {"tool_call": tool_call}, next_handler):
                out.append(item)
            return out

        return asyncio.run(scenario())

    def test_success_passthrough_and_logs(self, caplog):
        mw = AgentLoggingMiddleware()
        items = [
            SimpleNamespace(state=ToolResultState.RUNNING),
            SimpleNamespace(state=ToolResultState.SUCCESS, content=[SimpleNamespace(text="done")]),
        ]
        with caplog.at_level(logging.INFO, logger="friday.agent"):
            out = self._run(mw, _tool_call(), items)
        assert [i.state for i in out] == [ToolResultState.RUNNING, ToolResultState.SUCCESS]
        messages = [r.getMessage() for r in caplog.records]
        assert any("节点[工具调用]" in m and "web_search" in m and "哮喘" in m for m in messages)
        assert any("节点[工具结果]" in m and "success" in m and "耗时=" in m for m in messages)

    def test_error_state_logs_warning_with_preview(self, caplog):
        mw = AgentLoggingMiddleware()
        items = [SimpleNamespace(state=ToolResultState.ERROR, content=[SimpleNamespace(text="检索超时")])]
        with caplog.at_level(logging.WARNING, logger="friday.agent"):
            self._run(mw, _tool_call(), items)
        records = [r for r in caplog.records if "节点[工具结果]" in r.getMessage()]
        assert records and records[0].levelno == logging.WARNING
        assert "error" in records[0].getMessage() and "检索超时" in records[0].getMessage()

    def test_exception_logged_and_reraised(self, caplog):
        mw = AgentLoggingMiddleware()

        async def next_handler(**kwargs):
            yield SimpleNamespace(state=ToolResultState.RUNNING)
            raise RuntimeError("工具内部崩溃")

        async def scenario():
            async for _ in mw.on_acting(_fake_agent(), {"tool_call": _tool_call()}, next_handler):
                pass

        with caplog.at_level(logging.INFO, logger="friday.agent"), pytest.raises(RuntimeError):
            asyncio.run(scenario())
        assert any("节点[工具异常]" in r.getMessage() and "web_search" in r.getMessage() for r in caplog.records)

    def test_malformed_tool_call_passthrough_silent(self, caplog):
        # input_kwargs 里没有合法 ToolCallBlock 时纯透传，不记执行段日志（防御分支）
        mw = AgentLoggingMiddleware()
        items = [SimpleNamespace(state=ToolResultState.SUCCESS, content=[])]
        with caplog.at_level(logging.INFO, logger="friday.agent"):
            out = self._run(mw, None, items)
        assert [i.state for i in out] == [ToolResultState.SUCCESS]
        assert not [r for r in caplog.records if "节点[工具" in r.getMessage()]


class TestOnReasoning:
    def _run(self, mw, events):
        async def next_handler(**kwargs):
            for event in events:
                yield event

        async def scenario():
            out = []
            async for event in mw.on_reasoning(_fake_agent(), {}, next_handler):
                out.append(event)
            return out

        return asyncio.run(scenario())

    def test_model_call_logs_with_tokens(self, caplog):
        mw = AgentLoggingMiddleware()
        events = [
            SimpleNamespace(type=EventType.MODEL_CALL_START, model_name="deepseek-chat"),
            SimpleNamespace(
                type=EventType.MODEL_CALL_END,
                input_tokens=100, output_tokens=20,
                cache_input_tokens=60, cache_creation_input_tokens=5,
                finished_reason="completed",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="friday.agent"):
            out = self._run(mw, events)
        assert out == events
        messages = [r.getMessage() for r in caplog.records]
        assert any("节点[模型调用]" in m and "round=1" in m and "deepseek-chat" in m for m in messages)
        assert any(
            "节点[模型返回]" in m and "input_tokens=100" in m and "output_tokens=20" in m
            and "cache_read=60" in m and "cache_write=5" in m
            for m in messages
        )

    def test_round_increments_across_reasoning_rounds(self, caplog):
        mw = AgentLoggingMiddleware()
        events = [SimpleNamespace(type=EventType.MODEL_CALL_START, model_name="deepseek-chat")]
        with caplog.at_level(logging.INFO, logger="friday.agent"):
            self._run(mw, events)
            self._run(mw, events)
        messages = [r.getMessage() for r in caplog.records if "节点[模型调用]" in r.getMessage()]
        assert len(messages) == 2
        assert "round=1" in messages[0] and "round=2" in messages[1]


class TestOnReply:
    def test_end_log_with_duration(self, caplog):
        mw = AgentLoggingMiddleware()

        async def next_handler(**kwargs):
            yield SimpleNamespace(type=EventType.TEXT_BLOCK_DELTA, delta="hi")

        async def scenario():
            async for _ in mw.on_reply(_fake_agent(), {}, next_handler):
                pass

        with caplog.at_level(logging.INFO, logger="friday.agent"):
            asyncio.run(scenario())
        assert any("节点[Agent返回]" in r.getMessage() and "耗时=" in r.getMessage() for r in caplog.records)

    def test_end_log_aggregates_tokens_across_rounds(self, caplog):
        # 两轮 reasoning 各消耗 10/5 与 7/3，Agent返回 应给整轮累计 17/8
        mw = AgentLoggingMiddleware()

        def _round_handler(in_tok, out_tok):
            async def handler(**kwargs):
                yield SimpleNamespace(
                    type=EventType.MODEL_CALL_END,
                    input_tokens=in_tok, output_tokens=out_tok,
                    cache_input_tokens=0, cache_creation_input_tokens=0,
                    finished_reason="completed",
                )
            return handler

        async def reply_handler(**kwargs):
            yield SimpleNamespace(type=EventType.TEXT_BLOCK_DELTA, delta="hi")

        async def scenario():
            async for _ in mw.on_reasoning(_fake_agent(), {}, _round_handler(10, 5)):
                pass
            async for _ in mw.on_reasoning(_fake_agent(), {}, _round_handler(7, 3)):
                pass
            async for _ in mw.on_reply(_fake_agent(), {}, reply_handler):
                pass

        with caplog.at_level(logging.INFO, logger="friday.agent"):
            asyncio.run(scenario())
        final = [r.getMessage() for r in caplog.records if "节点[Agent返回]" in r.getMessage()]
        assert final and "input_tokens=17" in final[0] and "output_tokens=8" in final[0]


class _FakeModel(ChatModelBase):
    """单轮纯文本假模型：不触网，直接回一条已完成的响应。"""

    # Agent 装配时会读 formatter 判断输入媒体类型，借用 OpenAI 兼容实现
    formatter = OpenAIChatFormatter()

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        return ChatResponse(
            content=[TextBlock(type="text", text="ok")],
            is_last=True,
            usage=ChatUsage(input_tokens=10, output_tokens=5, time=0.01),
        )


class TestAgentWiring:
    def test_real_agent_fires_middleware_hooks(self, caplog):
        agent = Agent(
            name="Friday",
            system_prompt="你是测试助手",
            model=_FakeModel(
                credential=SimpleNamespace(),
                model="fake-model",
                parameters=ChatModelBase.Parameters(),
            ),
            state=AgentState(session_id="sess-int"),
            middlewares=[AgentLoggingMiddleware()],
        )

        async def scenario():
            async for _ in agent.reply_stream(UserMsg(name="user", content="hi")):
                pass

        with caplog.at_level(logging.INFO, logger="friday.agent"):
            asyncio.run(scenario())
        messages = [r.getMessage() for r in caplog.records]
        assert any("节点[模型调用]" in m and "fake-model" in m for m in messages)
        assert any("节点[模型返回]" in m and "input_tokens=10" in m for m in messages)
        assert any(
            "节点[Agent返回]" in m and "sess-int" in m
            and "input_tokens=10" in m and "output_tokens=5" in m
            for m in messages
        )
