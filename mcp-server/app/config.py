"""mcp-server：通用公共工具 MCP 服务（无状态）。

所有公共工具都在 app/tools/ 下按模块实现，由 tools 包自动注册；
本模块只做全局配置，.env 锚定到 mcp-server/ 目录，避免按 CWD 查找导致静默落空。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 智谱开放平台密钥：web_search / rag_search 等工具依赖；未配置时相应工具返回可读提示
    zhipu_api_key: str = ""
    # GLM 联网检索参数（与 backend 联网搜索同源约定）
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-5.3-flash"
    glm_search_engine: str = "search_std"
    # 医学文献混合检索（med-es 容器：ES 稠密 knn + BM25/IK 稀疏 → RRF 融合 → rerank 精排）
    embedding_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_model: str = "embedding-3"
    embedding_dimensions: int = 1024
    rerank_model: str = "rerank"
    # 本地 Elasticsearch（med-es 容器，已开 TLS + basic auth；自签证书默认不校验）
    es_url: str = "https://localhost:9200"
    es_username: str = "elastic"
    es_password: str = ""
    es_verify_certs: bool = False
    es_index: str = "medical_chunks"
    # 单路召回深度（knn k 与 BM25 size）：应大于融合窗口，避免单路第 N+1 名之后的好文档出局
    rag_recall_k: int = 20
    # RRF 融合候选数（rerank 前的候选池大小）与 rerank 后最终保留条数
    rag_candidate_k: int = 10
    rag_top_k: int = 3
    # rerank 相关性分数阈值（rerank-2 的 relevance_score，0~1）
    rag_min_score: float = 0.0
    # 传输方式：stdio（本地客户端默认）/ http（streamable-http，供远程消费方）
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8806
    # http 模式 Bearer 鉴权 token，逗号分隔可配多个（轮换时新旧并存）；留空不鉴权
    mcp_auth_token: str = ""

    # 锚定到 mcp-server/.env 的绝对路径：否则按 CWD 查找会静默不加载（backend 同款教训）
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"), extra="ignore"
    )

    @property
    def auth_token_list(self) -> list[str]:
        """解析逗号分隔的 token 列表，去除空白项。"""
        return [item.strip() for item in self.mcp_auth_token.split(",") if item.strip()]


settings = Settings()
