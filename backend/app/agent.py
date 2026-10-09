from collections.abc import AsyncIterator
import asyncio
import base64
import logging
import time
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langchain_deepseek import ChatDeepSeek
from langgraph.types import Command
from pydantic import BaseModel, Field

from .agent_tools import apply_session_rules, build_builtin_tools, resolve_workspace
from .config import settings
from .confirm import AskAnswer, PendingAsk, confirm_hub
from .files import resolve_stored_image
from .graph import build_graph
from .permissions import PermissionMode, SessionRule
from .prompts import AGENT_TOOLS_PROMPT, AGENT_TOOLS_PROMPT_HOST, SYSTEM_PROMPT, WEB_SEARCH_PROMPT
from .rag import medical_rag_search
from .tracing_callback import make_tracing_callbacks
# 别名避免与 _build/stream 的 web_search 布尔参数互相遮蔽
from .websearch import web_search as _web_search_tool
from .websearch import web_search_available

logger = logging.getLogger("friday.agent")

# 前端可切换的权限模式（ChatRequest.permission_mode 的取值域）。
# BYPASS（跳过全部安全检查）永不开放。
_PERMISSION_MODES: dict[str, PermissionMode] = {
    "default": PermissionMode.DEFAULT,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "explore": PermissionMode.EXPLORE,
    "dont_ask": PermissionMode.DONT_ASK,
}


class _MedicalRagArgs(BaseModel):
    query: str
    top_k: int = 3


class _WebSearchArgs(BaseModel):
    query: str
    count: int = 5


def _user_message(content: str, attachments: list[str] | None) -> HumanMessage:
    """有图片附件时构造多模态消息：图片读成 base64 data URI 直接传给模型。

    DeepSeek 的 OpenAI 兼容接口接受 image_url 形式的 data URI，
    LangChain 侧由 content blocks 原生承载该格式。
    """
    blocks: list[dict] = []
    for name in attachments or []:
        resolved = resolve_stored_image(name)
        if not resolved:
            logger.warning("节点[附件跳过] 附件不存在或名称非法 name=%r", name)
            continue
        path, media_type = resolved
        data = base64.b64encode(path.read_bytes()).decode()
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})
    if not blocks:
        return HumanMessage(content=content)
    # 只发图不打字时给模型一个默认指令
    blocks.insert(0, {"type": "text", "text": content or "请描述这张图片。"})
    logger.info("节点[多模态消息] images=%s text=%r", len(blocks) - 1, (content or "")[:60])
    return HumanMessage(content=blocks)


def _replay_msgs(replay: list[dict[str, str]]) -> list[BaseMessage]:
    """把库中的回放窗口转成 LangChain 消息：仅 user/assistant 纯文本（已由 context.py 清洗）。"""
    return [
        HumanMessage(content=item["content"]) if item["role"] == "user" else AIMessage(content=item["content"])
        for item in replay
    ]


class AgentService:
    """无状态的对话服务：跨请求不持有任何 Agent 上下文（记忆的唯一事实来源是数据库）。

    每轮请求用「滚动摘要 + 回放窗口」重建一次性 LangGraph 执行图：
    摘要并入 system prompt，历史作为消息列表直接传入，本轮消息（含图片附件）
    作为最后一条 HumanMessage。图内的 MemorySaver 检查点只服务本轮权限确认的
    interrupt 续跑，不跨轮持久。进程级共享实例带来的跨会话串味、重启失忆、
    并发中止连锁、开关切换丢上下文随之消失。
    """

    def __init__(self) -> None:
        self._model_ready = settings.model_provider == "deepseek" and bool(settings.openai_api_key)

    @staticmethod
    def _build_tools(web_search: bool) -> list[StructuredTool]:
        # 只读检索工具恒放行（permissions.AUTO_ALLOW_TOOLS），避免权限门挂起等待人工确认
        tools = [
            StructuredTool.from_function(
                coroutine=medical_rag_search,
                name="medical_rag_search",
                description=medical_rag_search.__doc__ or "medical_rag_search",
                args_schema=_MedicalRagArgs,
            ),
        ]
        if web_search:
            tools.append(
                StructuredTool.from_function(
                    coroutine=_web_search_tool,
                    name="web_search",
                    description=_web_search_tool.__doc__ or "web_search",
                    args_schema=_WebSearchArgs,
                )
            )
        return tools

    async def stream(
        self,
        conversation_id: UUID,
        replay: list[dict[str, str]],
        summary: str | None,
        content: str,
        deep_thinking: bool = False,
        web_search: bool = False,
        attachments: list[str] | None = None,
        memory_block: str | None = None,
        agent_tools: bool = False,
        permission_mode: str = "default",
        workspace_root: str | None = None,
        user_id: UUID | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        use_thinking = deep_thinking and bool(settings.openai_thinking_model)
        # 联网搜索需要智谱密钥（GLM 内置 web_search 执行）；未配置时降级为提示词引导
        use_search = web_search and web_search_available()
        # 内置工具受服务端总开关约束（安全边界见 agent_tools.py 模块说明）
        use_agent_tools = agent_tools and settings.agent_tools_enabled
        if self._model_ready:
            model = settings.openai_thinking_model if use_thinking else settings.openai_model
            tools = self._build_tools(use_search)
            permission_context = None
            agent_tools_prompt = ""
            if use_agent_tools:
                # 会话自选目录优先（用户本机目录，落库时已校验存在）；默认会话隔离目录需创建
                workspace = resolve_workspace(conversation_id, workspace_root)
                if not workspace_root:
                    workspace.mkdir(parents=True, exist_ok=True)
                mode = _PERMISSION_MODES.get(permission_mode, PermissionMode.DEFAULT)
                builtin_tools, permission_context = build_builtin_tools(workspace, mode)
                # 会话级「总是允许」规则重放（前端确认时勾选 always 落进来的）
                apply_session_rules(permission_context, confirm_hub.session_rules(conversation_id))
                tools.extend(builtin_tools)
                # 自选目录没有工作区下载链路，用 HOST 版提示词（告知本地路径）
                template = AGENT_TOOLS_PROMPT_HOST if workspace_root else AGENT_TOOLS_PROMPT
                agent_tools_prompt = template.format(
                    workspace=workspace,
                    # 产出文件下载走鉴权路由（前端会拦截该前缀链接做带 token 下载）
                    public_prefix=f"/api/workspaces/{conversation_id}",
                )
            # 滚动摘要并入 system prompt（原 AgentState.summary 的注入位，头文本保持一致）
            system_prompt = SYSTEM_PROMPT
            if summary:
                system_prompt += f"\n\n【本会话此前对话的滚动摘要（要点记录，细节以最近消息为准）】\n{summary}"
            if memory_block:
                system_prompt += f"\n\n{memory_block}"
            if use_search:
                system_prompt += f"\n\n{WEB_SEARCH_PROMPT}"
            if agent_tools_prompt:
                system_prompt += f"\n\n{agent_tools_prompt}"
            llm = ChatDeepSeek(
                model=model,
                api_key=settings.openai_api_key,
                api_base=settings.openai_base_url,
                stream_usage=True,
                # thinking 开关映射 DeepSeek 的 thinking.type（enabled/disabled）
                extra_body={"thinking": {"type": "enabled" if use_thinking else "disabled"}},
                callbacks=make_tracing_callbacks(),
            )
            graph = build_graph(llm, tools, permission_context)
            user_content = content
            # 有 reasoning 模型时由模型真正产出思考过程；没有才退化成提示词引导
            if deep_thinking and not use_thinking:
                user_content = f"请深度思考后回答。\n\n{user_content}"
            if web_search and not use_search:
                user_content = f"请结合联网检索能力回答；如果无法访问网络，请明确说明。\n\n{user_content}"
            logger.info(
                "节点[Agent调用] conversation=%s replay=%s条(摘要=%s) model=%s thinking=%s search=%s agent_tools=%s perm=%s prompt=%r",
                conversation_id, len(replay), bool(summary), model, deep_thinking, web_search, use_agent_tools,
                permission_mode if use_agent_tools else "-", user_content[:100],
            )
            config = {
                "configurable": {"thread_id": str(conversation_id)},
                "recursion_limit": 80,
            }
            messages = [SystemMessage(content=system_prompt), *_replay_msgs(replay), _user_message(user_content, attachments)]
            inputs: object = {"messages": messages}
            totals = {"input_tokens": 0, "output_tokens": 0, "rounds": 0}
            started = time.perf_counter()
            while True:
                pending_payload: dict | None = None
                async for mode_key, chunk in graph.astream(inputs, config, stream_mode=["custom", "updates"]):
                    if mode_key == "custom":
                        event = chunk
                        if not isinstance(event, dict):
                            continue
                        # usage 事件只做回复级累计，不进 SSE
                        if event.get("type") == "usage":
                            totals["input_tokens"] += int(event.get("input_tokens") or 0)
                            totals["output_tokens"] += int(event.get("output_tokens") or 0)
                            totals["rounds"] += 1
                            continue
                        yield event
                    elif mode_key == "updates":
                        interrupted = chunk.get("__interrupt__") if isinstance(chunk, dict) else None
                        if interrupted:
                            pending_payload = interrupted[0].value
                if not isinstance(pending_payload, dict):
                    break
                # ---- 权限确认：推给前端 → 等确认接口应答（超时自动拒绝）→ 续跑 ----
                pending = PendingAsk(
                    conversation_id=conversation_id,
                    user_id=user_id or UUID(int=0),
                    reply_id=pending_payload["reply_id"],
                    calls=pending_payload.get("calls", []),
                )
                confirm_hub.register(pending)
                logger.info(
                    "节点[权限确认] conversation=%s reply=%s calls=%s",
                    conversation_id, pending_payload["reply_id"], [c.get("name") for c in pending.calls],
                )
                yield {"type": "permission_ask", "reply_id": pending_payload["reply_id"], "calls": pending.calls}
                timed_out = False
                try:
                    await asyncio.wait_for(
                        pending.event.wait(), timeout=settings.permission_confirm_timeout_seconds,
                    )
                except TimeoutError:
                    timed_out = True  # 无人应答：超时按拒绝续跑，流不会悬挂
                finally:
                    confirm_hub.cancel(conversation_id)
                answer = pending.answer or AskAnswer(approved=False)
                yield {"type": "permission_resolved", "approved": answer.approved, "timed_out": timed_out}
                # 「总是允许」：把建议规则存为会话级规则（后续轮次自动重放），
                # 并随 resume 答复喂回 gate（本回复内立即生效由 approve 达成）
                if answer.approved and answer.always:
                    suggested = [
                        SessionRule(item["tool_name"], item["rule_content"])
                        for item in pending_payload.get("suggestions", [])
                    ]
                    if suggested:
                        confirm_hub.add_session_rules(conversation_id, suggested)
                inputs = Command(resume={"approved": answer.approved, "always": answer.always})
            logger.info(
                "节点[Agent返回] 模型流结束 rounds=%s input_tokens=%s output_tokens=%s 耗时=%.2fs session=%s",
                totals["rounds"], totals["input_tokens"], totals["output_tokens"],
                time.perf_counter() - started, conversation_id,
            )
            return

        logger.warning("节点[演示模式] 未配置模型密钥，返回本地演示流式回复 prompt=%r", content[:100])

        if deep_thinking:
            yield {"type": "thinking", "content": "正在梳理问题的上下文、约束条件和可执行步骤。"}
        if web_search:
            yield {"type": "search", "content": "已准备好联网搜索结果摘要。"}
        answer = self._demo_answer(content)
        for index in range(0, len(answer), 4):
            await asyncio.sleep(0.025)
            yield {"type": "text", "content": answer[index : index + 4]}

    @staticmethod
    def _demo_answer(prompt: str) -> str:
        return (
            "收到，我先把这个问题拆成可执行的步骤。\n\n"
            f"你当前的问题是：{prompt}\n\n"
            "建议先明确目标、输入和验收标准，再按最小闭环实现。"
            "如果这是工程问题，可以从数据结构、错误处理、性能边界和测试用例四个方面逐项确认。\n\n"
            "当前服务未配置外部模型密钥，因此这里返回的是本地演示流式回复。"
            "配置 OPENAI_API_KEY 并将 MODEL_PROVIDER 设置为 deepseek 后，会通过 LangGraph 调用 DeepSeek。"
        )


agent_service = AgentService()
