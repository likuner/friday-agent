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
    medrag_db_url: str = "postgresql://meduser:medpass@localhost:5433/medrag"
    rag_top_k: int = 4
    rag_min_score: float = 0.0
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
