from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://friday:friday@localhost:5432/friday_agent"
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 10080
    cors_origins: str = "http://localhost:3000"
    model_provider: str = "demo"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.deepseek.com"
    openai_model: str = "deepseek-chat"
    # 深度思考模式使用的模型（需支持 reasoning，如 deepseek-v4-flash）；留空则退化为提示词引导
    openai_thinking_model: str = "deepseek-v4-flash"
    # RAG（医学文献向量检索）
    zhipu_api_key: str = ""
    embedding_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_model: str = "embedding-3"
    embedding_dimensions: int = 512
    # 联网搜索：主模型 tool_call 触发 web_search 工具，转交 GLM 内置联网检索执行（复用 zhipu_api_key）
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4-flash"
    glm_search_engine: str = "search_std"
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
    medrag_db_url: str = "postgresql://meduser:medpass@localhost:5433/medrag"
    rag_top_k: int = 4
    rag_min_score: float = 0.0
    # 可观测：OpenTelemetry 追踪（可接入 AgentScope Studio / Jaeger / Langfuse 等 OTLP 后端）
    tracing_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    # grpc 对应 Studio 的 OTEL_GRPC_PORT(4317)；http 对应其 Web 端口(默认 3000)
    otel_exporter_otlp_protocol: str = "grpc"
    otel_service_name: str = "friday-agent"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
