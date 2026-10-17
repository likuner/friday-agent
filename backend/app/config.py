from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 已知弱密钥：命中即拒绝启动，防止漏配后静默用可预测值签发 token
_WEAK_JWT_SECRETS = {"", "change-me", "replace-with-a-long-random-secret"}


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://friday:friday@localhost:5432/friday_agent"
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 10080
    cors_origins: str = "http://localhost:3000"
    model_provider: str = "demo"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    # V4 系列：deepseek-chat/deepseek-reasoner 已于 2026-07-24 退役
    llm_model: str = "deepseek-v4-pro"
    # 深度思考模式使用的模型（需支持 reasoning，V4 系列均原生支持）；留空则退化为提示词引导
    llm_thinking_model: str = "deepseek-v4-pro"
    # 带图请求的多模态档（V4.1 Flash，API 别名 deepseek-flash）：v4-pro 无视觉，
    # API 不报错而是把图片静默替换成 [Unsupported Image] 占位文本
    llm_vision_model: str = "deepseek-flash"
    # GLM 直连（长期记忆抽取 / 滚动摘要用）
    zhipu_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-5.3-flash"
    # 医学文献检索（medical_rag_search）与联网搜索均转交 mcp-server 执行，
    # ES/embedding/rerank 配置与检索逻辑都在 mcp-server 侧（见 mcp-server/.env）；
    # mcp_auth_token 与 mcp-server/.env 的 MCP_AUTH_TOKEN 保持一致，服务端未配鉴权则留空
    mcp_server_url: str = "http://127.0.0.1:8806/mcp"
    mcp_auth_token: str = ""
    # 会话级记忆（上下文管理）：回放窗口 token 预算与滚动摘要参数。
    # 溢出段（早于窗口、晚于摘要游标）攒够 summary_min_overflow_tokens 才触发后台压缩，
    # 避免每轮都做小规模摘要；摘要模型复用 glm_model。
    context_token_budget: int = 1000
    summary_max_tokens: int = 500
    summary_min_overflow_tokens: int = 200
    # 用户级长期记忆：每用户一行聚合文本，GLM 合并式抽取（与滚动摘要同构），
    # 每轮全量注入 system prompt。长度由合并提示词限定，写回前行级去重兜底。
    # 未配置智谱密钥时抽取自动停用，读取注入不受影响。
    memory_enabled: bool = True
    memory_extract_min_messages: int = 1
    memory_max_tokens: int = 500
    # Agent 内置工具（Bash/文件读写/任务四件套）：服务端总开关（默认开）。
    # 请求侧还需 ChatRequest.agent_tools 同时为真才注册（前端仅工作区会话开启）。
    # DONT_ASK 权限模式：写入仅限每会话工作区目录，敏感路径由 deny 规则封禁，
    # Bash 危险命令自动拒绝；非硬隔离（详见 ARCHITECTURE.md 权限模型一节）。
    agent_tools_enabled: bool = True
    workspaces_dir: str = "workspaces"
    # 多智能体协作（CrewAI：leader 规划派活 + 检索员/执行员执行）：
    # 服务端总开关，紧急情况一键回退单 Agent。请求侧还需 ChatRequest.multi_agent
    # 为真且非带图请求才组建团队；成员按开关裁剪（检索员随 MCP 可用性、
    # 执行员随 agent_tools），见 crew_agents.build_team。
    multi_agent_enabled: bool = True
    # Bash 命令放行前缀（逗号分隔，子串匹配，如 "open -a,osascript"）：
    # DONT_ASK 模式下非白名单命令一律拒绝；想让 Friday 打开本机应用等场景可显式放行。
    # 仅建议本地开发使用——该放行对所有登录用户生效，且命令在后端所在机器上执行。
    agent_tools_bash_allow_prefixes: str = ""
    # 权限确认（ASK）超时秒数：DEFAULT/ACCEPT_EDITS 模式下前端确认卡片无人应答，
    # 超时后按「拒绝」续跑（模型收到 denied 结果继续生成），不会悬挂请求。
    permission_confirm_timeout_seconds: int = 120
    # 可观测：OpenTelemetry 追踪（可接入 AgentScope Studio / Jaeger / Langfuse 等 OTLP 后端）
    tracing_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    # grpc 对应 Studio 的 OTEL_GRPC_PORT(4317)；http 对应其 Web 端口(默认 3000)
    otel_exporter_otlp_protocol: str = "grpc"
    otel_service_name: str = "friday-agent"
    # 锚定到 backend/.env 的绝对路径：否则从仓库根启动时 .env 按 CWD 查找会静默不加载，
    # 全部配置落到代码默认值（含弱 jwt_secret）且无任何提示
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"), extra="ignore"
    )

    @model_validator(mode="after")
    def _reject_weak_jwt_secret(self) -> "Settings":
        if self.jwt_secret.strip().lower() in _WEAK_JWT_SECRETS:
            raise ValueError(
                "JWT_SECRET 未配置或为弱默认值：请在 backend/.env 中设置随机长密钥"
                "（可用 `openssl rand -hex 32` 生成）后重启"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
