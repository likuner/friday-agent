"""LangGraph 版 Agent 执行图：agent → 权限门 → tools 的 ReAct 循环。

替代原 AgentScope ``Agent.reply_stream`` 的编排职责：
- ``agent`` 节点：模型流式生成，text/thinking 增量与 tool_call chip 经
  ``get_stream_writer`` 写成自定义事件，由 agent.py 消费翻译成 SSE；
- ``gate`` 节点：对模型产出的每个工具调用做权限裁决（permissions.decide），
  有 ASK 时 ``interrupt()`` 挂起，等确认接口应答后 ``Command(resume=...)``
  从检查点续跑（对应原 park-and-resume 模型）；
- ``tools`` 节点：放行的调用并行执行，被拒调用合成拒绝 ToolMessage 回填
  （模型收到拒绝结果继续生成，与原 ConfirmResult(confirmed=False) 语义一致）。

执行段 节点[...] 日志与原 AgentLoggingMiddleware 输出同格式（模型调用/token/
耗时、工具调用/参数/结果）；回复级汇总（节点[Agent返回]）由 agent.py 根据
usage 自定义事件在流结束时统一记录。每请求编译一张新图 + MemorySaver 检查点
（仅服务本轮 interrupt 续跑，不跨轮持久——记忆唯一事实来源仍是数据库）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .permissions import Behavior, PermissionContext, decide

logger = logging.getLogger("friday.agent")

# chip 摘要的参数键优先级：检索词 → 命令 → 文件路径 → 搜索模式 → 目录 → 任务标题
_TOOL_QUERY_KEYS = ("query", "command", "file_path", "pattern", "path", "subject")


def _tool_query(args: dict | str) -> str:
    """从工具调用参数里取出展示摘要（检索词/命令/文件路径等），供前端 chip。"""
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except ValueError:
            return ""
    if not isinstance(args, dict):
        return ""
    for key in _TOOL_QUERY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return ""


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # 被拒（规则 DENY 或用户拒绝）的工具调用：[{id, message}]，tools 节点合成拒绝回填
    denied_calls: list[dict[str, str]]


def _reasoning_delta(chunk: AIMessage) -> str:
    """DeepSeek 思考流的增量：reasoning_content 在 additional_kwargs 里透传。"""
    value = chunk.additional_kwargs.get("reasoning_content") or chunk.additional_kwargs.get("reasoning")
    return value if isinstance(value, str) else ""


def _text_delta(chunk: AIMessage) -> str:
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _usage(ai_message: AIMessage) -> dict[str, Any]:
    """提取 token 用量（cache 字段按 DeepSeek 的 prompt_cache_* 字段兜底）。"""
    metadata = ai_message.usage_metadata or {}
    token_usage = ai_message.response_metadata.get("token_usage") or {}
    details = metadata.get("input_token_details") or {}
    return {
        "input_tokens": metadata.get("input_tokens") or token_usage.get("prompt_tokens") or 0,
        "output_tokens": metadata.get("output_tokens") or token_usage.get("completion_tokens") or 0,
        "cache_read": token_usage.get("prompt_cache_hit_tokens") or details.get("cache_read") or 0,
        "cache_write": token_usage.get("prompt_cache_miss_tokens") or details.get("cache_write") or 0,
        "finished_reason": ai_message.response_metadata.get("finish_reason") or "",
    }


def _agent_node(llm: BaseChatModel, round_counter: dict[str, int]):
    async def agent_node(state: AgentState) -> dict:
        writer = get_stream_writer()
        round_counter["n"] += 1
        model_name = getattr(llm, "model_name", None) or getattr(llm, "model", "")
        logger.info("节点[模型调用] round=%s model=%s", round_counter["n"], model_name)
        final: AIMessage | None = None
        async for chunk in llm.astream(state["messages"]):
            final = chunk if final is None else final + chunk
            if not final:
                continue
            thinking = _reasoning_delta(chunk)
            if thinking:
                writer({"type": "thinking", "content": thinking})
            text = _text_delta(chunk)
            if text:
                writer({"type": "text", "content": text})
        if final is None:
            raise RuntimeError("模型未返回任何内容")
        usage = _usage(final)
        logger.info(
            "节点[模型返回] round=%s input_tokens=%s output_tokens=%s cache_read=%s cache_write=%s finished=%s",
            round_counter["n"], usage["input_tokens"], usage["output_tokens"],
            usage["cache_read"], usage["cache_write"], usage["finished_reason"],
        )
        # usage 给 agent.py 做回复级累计（SSE 侧不透传该类型）
        writer({"type": "usage", **usage})
        # 参数此刻才收全：按调用顺序下发 chip，多次并行检索就不会像同一个 chip
        for call in final.tool_calls or []:
            writer({"type": "tool_call", "name": call.get("name", ""), "query": _tool_query(call.get("args", {}))})
        return {"messages": [final]}

    return agent_node


def _gate_node(context: PermissionContext):
    async def gate_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            return {"denied_calls": []}
        decisions: list[tuple[dict, Any]] = [
            (call, decide(call.get("name", ""), call.get("args") or {}, context)) for call in last.tool_calls or []
        ]
        denied: list[dict[str, str]] = [
            {"id": call["id"], "message": decision.message}
            for call, decision in decisions
            if decision.behavior is Behavior.DENY
        ]
        asks = [(call, decision) for call, decision in decisions if decision.behavior is Behavior.ASK]
        if asks:
            reply_id = uuid.uuid4().hex
            payload = {
                "reply_id": reply_id,
                "calls": [{"id": call["id"], "name": call.get("name", ""), "query": _tool_query(call.get("args", {}))} for call, _ in asks],
                # 「总是允许」建议规则：应答 always=true 时由 agent.py 落进 confirm_hub 跨轮重放
                "suggestions": [
                    {"tool_name": rule.tool_name, "rule_content": rule.rule_content}
                    for _, decision in asks
                    for rule in decision.suggested_rules
                ],
            }
            answer = interrupt(payload)
            approved = bool(answer.get("approved"))
            for call, decision in asks:
                if not approved:
                    denied.append({"id": call["id"], "message": "用户拒绝执行该操作"})
            logger.info(
                "节点[权限确认] calls=%s approved=%s always=%s",
                [c.get("name") for c, _ in asks], approved, bool(answer.get("always")),
            )
        return {"denied_calls": denied}

    return gate_node


async def _run_tool(tool: BaseTool, call: dict) -> tuple[str, str, str]:
    """执行单个工具调用，返回 (tool_call_id, name, 结果文本)；异常转错误串不外抛。"""
    name = call.get("name", "")
    raw_args = json.dumps(call.get("args", {}), ensure_ascii=False)
    logger.info("节点[工具调用] model 自主决策调用工具 name=%s call_id=%s args=%s", name, call.get("id"), raw_args[:200])
    start = time.perf_counter()
    try:
        result = await tool.ainvoke(call.get("args") or {})
    except Exception as exc:  # 工具内部异常转错误结果，模型据此自纠
        logger.exception("节点[工具异常] name=%s call_id=%s 耗时=%.2fs", name, call.get("id"), time.perf_counter() - start)
        return call.get("id", ""), name, f"Error: 工具执行失败：{exc}"
    status = "error" if str(result).startswith("Error") else "success"
    log = logger.warning if status == "error" else logger.info
    log(
        "节点[工具结果] name=%s call_id=%s state=%s 耗时=%.2fs preview=%r",
        name, call.get("id"), status, time.perf_counter() - start, str(result)[:120],
    )
    return call.get("id", ""), name, str(result)


def _tools_node(tools: list[BaseTool]):
    registry = {tool.name: tool for tool in tools}

    async def tools_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            return {"messages": []}
        denied = {item["id"]: item["message"] for item in state.get("denied_calls", [])}
        messages: list[ToolMessage] = []
        allowed: list[tuple[BaseTool | None, dict]] = []
        for call in last.tool_calls or []:
            reason = denied.get(call.get("id", ""))
            if reason is not None:
                messages.append(
                    ToolMessage(
                        content=f"权限请求被拒绝：{reason}。请换一种安全的方式完成任务，不要原样重试。",
                        tool_call_id=call.get("id", ""),
                    )
                )
            else:
                allowed.append((registry.get(call.get("name", "")), call))
        results = await asyncio.gather(
            *(_run_tool(tool, call) if tool else _missing_tool(call) for tool, call in allowed)
        )
        for (_tool, call), (_call_id, name, result) in zip(allowed, results):
            messages.append(ToolMessage(content=result, tool_call_id=call.get("id", ""), name=name))
        return {"messages": messages}

    return tools_node


async def _missing_tool(call: dict) -> tuple[str, str, str]:
    """模型调用了未注册的工具（schema 漂移）：回填错误而不是中断整轮。"""
    name = call.get("name", "")
    logger.warning("节点[工具缺失] name=%s call_id=%s", name, call.get("id"))
    return call.get("id", ""), name, f"Error: 工具 {name} 不存在"


def build_graph(llm: BaseChatModel, tools: list[BaseTool], permission_context: PermissionContext | None):
    """编译每请求一次的执行图（MemorySaver 只服务本轮 interrupt 续跑）。

    tools 非空时绑定到模型（bind_tools）；无工具的会话模型直答，权限门直通。
    """
    round_counter = {"n": 0}
    bound = llm.bind_tools(tools) if tools else llm
    builder = StateGraph(AgentState)
    builder.add_node("agent", _agent_node(bound, round_counter))
    builder.add_node("gate", _gate_node(permission_context) if permission_context else _allow_all_gate())
    builder.add_node("tools", _tools_node(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", _route_after_agent)
    builder.add_edge("gate", "tools")
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=MemorySaver())


def _allow_all_gate():
    """无内置工具时（纯 RAG/搜索会话）权限门直通：全部放行。"""

    async def gate_node(state: AgentState) -> dict:
        return {"denied_calls": []}

    return gate_node


def _route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "gate"
    return END
