"""crew_logging.py 执行段日志单测：真实事件总线发射 → 日志断言，不触网。"""

from datetime import datetime, timezone

from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.llm_events import LLMCallCompletedEvent, LLMCallType, LLMCallFailedEvent
from crewai.events.types.tool_usage_events import ToolUsageFinishedEvent, ToolUsageStartedEvent

from app.crew_logging import install_logging_listener


def _emit(event) -> None:
    crewai_event_bus.emit(None, event=event)
    # 非流块事件经总线线程池异步分发，断言前先等 handler 执行完
    crewai_event_bus.flush(timeout=5)


class TestLoggingListener:
    def test_install_is_idempotent(self):
        install_logging_listener()
        install_logging_listener()  # 重复安装不重复挂载、不抛异常

    def test_llm_completed_logs_tokens(self, caplog):
        install_logging_listener()
        with caplog.at_level("INFO", logger="friday.agent"):
            _emit(LLMCallCompletedEvent(
                response="ok", call_type=LLMCallType.LLM_CALL, call_id="c1",
                agent_role="检索员", model="deepseek-v4-pro",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3},
                finish_reason="stop",
            ))
        assert any("节点[模型返回]" in r.message and "input_tokens=10" in r.message for r in caplog.records)

    def test_tool_finished_logs_duration(self, caplog):
        install_logging_listener()
        start = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        with caplog.at_level("INFO", logger="friday.agent"):
            _emit(ToolUsageStartedEvent(
                tool_name="Bash", tool_args={"command": "ls"}, agent_role="执行员"))
            _emit(ToolUsageFinishedEvent(
                tool_name="Bash", tool_args={"command": "ls"}, agent_role="执行员",
                started_at=start, finished_at=start, output="ok"))
        assert any("节点[工具调用]" in r.message for r in caplog.records)
        assert any("节点[工具结果]" in r.message and "state=ok" in r.message for r in caplog.records)

    def test_llm_failed_logs_warning(self, caplog):
        install_logging_listener()
        with caplog.at_level("WARNING", logger="friday.agent"):
            _emit(LLMCallFailedEvent(error="boom", call_id="c2", agent_role="Friday"))
        assert any("节点[模型异常]" in r.message and "boom" in r.message for r in caplog.records)
