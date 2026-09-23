"""医学文献向量检索（med-pgvector 容器：pgvector + 智谱 embedding-3）。"""

import asyncio
import logging
import time

import asyncpg
import httpx

from .config import settings

logger = logging.getLogger("friday.rag")

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(settings.medrag_db_url, min_size=1, max_size=4)
                logger.info("节点[RAG连接] 已连接向量数据库 dims=%s", settings.embedding_dimensions)
    return _pool


async def embed_query(text: str) -> list[float]:
    """调用智谱 embedding 接口生成查询向量。"""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.embedding_base_url}/embeddings",
            json={
                "model": settings.embedding_model,
                "input": [text],
                "dimensions": settings.embedding_dimensions,
            },
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
        )
        resp.raise_for_status()
    vector = resp.json()["data"][0]["embedding"]
    logger.info(
        "节点[RAG向量化] model=%s dims=%s latency=%.2fs text=%r",
        settings.embedding_model, len(vector), time.monotonic() - started, text[:80],
    )
    return vector


async def rag_search(query: str, top_k: int | None = None) -> list[dict]:
    """在 med-pgvector 中做余弦相似度检索，返回最相关的文献块。"""
    top_k = top_k or settings.rag_top_k
    started = time.monotonic()
    logger.info("节点[RAG检索开始] query=%r top_k=%s", query[:120], top_k)
    try:
        vector = await embed_query(query)
        literal = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.chunk_text, d.title, d.source_name, d.source_url,
                       1 - (c.embedding <=> $1::vector) AS score
                FROM chunks c JOIN documents d ON d.id = c.document_id
                ORDER BY c.embedding <=> $1::vector
                LIMIT $2
                """,
                literal,
                top_k,
            )
        hits = [
            {
                "title": row["title"],
                "source": row["source_name"],
                "url": row["source_url"],
                "chunk_text": row["chunk_text"],
                "score": float(row["score"]),
            }
            for row in rows
        ]
        kept = [hit for hit in hits if hit["score"] >= settings.rag_min_score]
        logger.info(
            "节点[RAG检索完成] query=%r hits=%s kept=%s top_score=%.4f latency=%.2fs",
            query[:80], len(hits), len(kept), hits[0]["score"] if hits else 0.0, time.monotonic() - started,
        )
        for index, hit in enumerate(kept, 1):
            logger.info(
                "节点[RAG命中] #%s score=%.4f title=%r source=%r snippet=%r",
                index, hit["score"], hit["title"], hit["source"], hit["chunk_text"][:120],
            )
        return kept
    except Exception:
        logger.exception("节点[RAG检索异常] query=%r", query[:120])
        raise


async def medical_rag_search(query: str, top_k: int = 4) -> str:
    """检索本地医学文献向量库（语料为中文医学文献，检索词请使用中文，通用英文缩写可保留）。当用户询问医学或健康相关问题（疾病、症状、诊断、治疗、药物、临床研究等）时调用本工具，获取文献依据后再回答。非医学问题不要调用。"""
    try:
        hits = await rag_search(query, top_k)
    except Exception as exc:
        return f"医学文献检索失败：{exc}"
    if not hits:
        return "未在医学文献库中找到相关文献。请基于你自己的知识回答，并说明未找到文献支持。"
    lines = ["以下是与问题最相关的医学文献摘录（含相似度分数与来源）："]
    for index, hit in enumerate(hits, 1):
        lines.append(
            f"[{index}] ({hit['source']}, score={hit['score']:.4f}) {hit['title']}\n"
            f"URL: {hit['url']}\n{hit['chunk_text']}"
        )
    return "\n\n".join(lines)
