"""Agent 组装：把凭证、提示词、中间件、模型、工具/技能装配成一次性 Agent。

每轮请求由 agent.py 的 AgentService.stream() 调用，无跨请求共享状态。
"""

from agentscope.agent import Agent, InjectionConfig
from agentscope.credential import DeepSeekCredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.mcp import MCPClient
from agentscope.middleware import TracingMiddleware
from agentscope.model import DeepSeekChatModel
from agentscope.state import AgentState
from agentscope.tool import ToolBase, Toolkit

from .agent_logging import AgentLoggingMiddleware
from .agent_skills import SKILL_LOADER
from .prompts import SYSTEM_PROMPT, WEB_SEARCH_PROMPT


def build_agent(
    credential: DeepSeekCredential,
    model: str,
    state: AgentState,
    thinking: bool = False,
    web_search: bool = False,
    memory_block: str | None = None,
    builtin_tools: list[ToolBase] | None = None,
    agent_tools_prompt: str = "",
    mcps: list[MCPClient] | None = None,
) -> Agent:
    # 每个 Agent 用独立 Toolkit：共享实例可能被 Agent 内部改写。
    # 医学检索（medical_rag_search）与联网搜索（web_search）都经 MCP 注册，
    # 服务端 readOnlyHint 声明只读自动放行；内置工具（Bash/文件/任务）在此
    # 混排注册，权限由 AgentState 的 DONT_ASK 上下文裁决（工作区内写入放行、
    # 敏感路径 deny、其余拒绝）
    tools: list[ToolBase] = []
    if builtin_tools:
        tools.extend(builtin_tools)
    return Agent(
        name="Friday",
        # 长期记忆块（用户画像/事实条目，自带标注头）插在人设指令之后；
        # 其标注已声明"与最近对话冲突时以最近对话为准"，与会话上下文指令衔接
        system_prompt=SYSTEM_PROMPT
        + (f"\n\n{memory_block}" if memory_block else "")
        + (f"\n\n{WEB_SEARCH_PROMPT}" if web_search else "")
        + (f"\n\n{agent_tools_prompt}" if agent_tools_prompt else ""),
        # Tracing 未配置时自动短路；日志中间件产出执行段节点日志（模型/工具/耗时/token）
        middlewares=[TracingMiddleware(), AgentLoggingMiddleware()],
        # 框架默认以 UTC 注入 <current-time>，模型会先答 UTC 再自行换算；
        # 改成东八区后直接报告北京时间
        injection_config=InjectionConfig(timezone="Asia/Shanghai"),
        # 每轮一次性状态：session_id 绑定会话，summary 承载滚动摘要
        state=state,
        model=DeepSeekChatModel(
            credential=credential,
            model=model,
            parameters=DeepSeekChatModel.Parameters(thinking_enable=True) if thinking else None,
            stream=True,
            # DeepSeekChatFormatter 会跳过 DataBlock（图片），换成 OpenAI 兼容 formatter；
            # DeepSeek 接口本身兼容，纯文本/工具调用路径实测同样正常。
            formatter=OpenAIChatFormatter(),
        ),
        toolkit=Toolkit(tools=tools, mcps=mcps, skills_or_loaders=[SKILL_LOADER]),
    )
