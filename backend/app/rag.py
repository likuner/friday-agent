"""医学文献混合检索（med-es 容器：Elasticsearch 稠密 knn + BM25/IK 稀疏，RRF 融合后经智谱 rerank-2 精排）。"""

import asyncio
import logging
import time

import httpx
from elasticsearch import AsyncElasticsearch

from .config import settings

logger = logging.getLogger("friday.rag")

_es_client: AsyncElasticsearch | None = None
_es_client_lock = asyncio.Lock()


async def _get_es_client() -> AsyncElasticsearch:
    global _es_client
    if _es_client is None:
        async with _es_client_lock:
            if _es_client is None:
                _es_client = AsyncElasticsearch(
                    hosts=[settings.es_url],
                    basic_auth=(settings.es_username, settings.es_password),
                    verify_certs=settings.es_verify_certs,
                    ssl_show_warn=False,
                )
                logger.info(
                    "节点[RAG连接] 已连接 Elasticsearch url=%s index=%s dims=%s",
                    settings.es_url, settings.es_index, settings.embedding_dimensions,
                )
    return _es_client


async def close_es_client() -> None:
    """进程退出前关闭 ES 连接（脚本场景避免 Unclosed session 告警）。"""
    global _es_client
    if _es_client is not None:
        await _es_client.close()
        _es_client = None


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


RRF_RANK_CONSTANT = 60  # ES RRF 的默认秩常数


async def _hybrid_recall(query: str, vector: list[float], recall_k: int, candidate_k: int) -> list[dict]:
    """稠密 knn 与 BM25 倒排（IK 分词）两路并行召回（各取前 recall_k 条），
    应用层 RRF 融合取前 candidate_k 条候选。

    单路深度大于融合窗口（20→10），避免单路第 11 名之后的好文档永远出局；
    RRF retriever 是 ES 付费许可功能（Basic license 返回 403），故在客户端按
    score = Σ 1/(K + rank) 融合，数学上与服务端 RRF 等价。
    """
    es = await _get_es_client()
    source = ["chunk_text", "title", "source", "source_url"]
    knn_resp, bm25_resp = await asyncio.gather(
        es.search(
            index=settings.es_index,
            knn={
                "field": "embedding",
                "query_vector": vector,
                "k": recall_k,
                "num_candidates": 100,
            },
            size=recall_k,
            source=source,
        ),
        es.search(
            index=settings.es_index,
            query={"match": {"chunk_text": query}},
            size=recall_k,
            source=source,
        ),
    )
    fused: dict[str, dict] = {}
    for leg, resp in (("dense", knn_resp), ("sparse", bm25_resp)):
        for rank, hit in enumerate(resp["hits"]["hits"], 1):
            doc = hit["_source"]
            entry = fused.setdefault(
                hit["_id"],
                {
                    "title": doc.get("title") or "",
                    "source": doc.get("source") or "",
                    "url": doc.get("source_url") or "",
                    "chunk_text": doc["chunk_text"],
                    "rrf_score": 0.0,
                    "legs": [],
                },
            )
            entry["rrf_score"] += 1.0 / (RRF_RANK_CONSTANT + rank)
            entry["legs"].append(leg)
    candidates = sorted(fused.values(), key=lambda item: item["rrf_score"], reverse=True)[:candidate_k]
    for rank, candidate in enumerate(candidates, 1):
        candidate["rrf_rank"] = rank
    return candidates


async def _rerank(query: str, candidates: list[dict], top_n: int) -> list[tuple[int, float]]:
    """智谱 rerank-2 交叉编码精排，返回 [(候选下标, 相关性分数)]，按分数降序。"""
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.embedding_base_url}/rerank",
            json={
                "model": settings.rerank_model,
                "query": query,
                "documents": [candidate["chunk_text"] for candidate in candidates],
                "top_n": top_n,
            },
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
        )
        resp.raise_for_status()
    results = resp.json()["results"]
    ranking = [(item["index"], float(item["relevance_score"])) for item in results]
    ranking.sort(key=lambda item: item[1], reverse=True)
    logger.info(
        "节点[RAG重排] model=%s docs=%s top_n=%s latency=%.2fs best=%.6f",
        settings.rerank_model, len(candidates), top_n, time.monotonic() - started,
        ranking[0][1] if ranking else 0.0,
    )
    return ranking


async def rag_search(query: str, top_k: int | None = None) -> list[dict]:
    """混合检索：RRF 融合召回 top rag_candidate_k 条候选，rerank 精排后返回最终 top_k 条。"""
    top_k = top_k or settings.rag_top_k
    started = time.monotonic()
    logger.info(
        "节点[RAG检索开始] query=%r recall=%s candidates=%s top_k=%s",
        query[:120], settings.rag_recall_k, settings.rag_candidate_k, top_k,
    )
    try:
        vector = await embed_query(query)
        candidates = await _hybrid_recall(query, vector, settings.rag_recall_k, settings.rag_candidate_k)
        logger.info(
            "节点[RAG融合完成] query=%r hits=%s rrf_top=%r",
            query[:80], len(candidates), candidates[0]["title"] if candidates else "",
        )
        try:
            ranking = await _rerank(query, candidates, top_k)
        except Exception:
            logger.exception("节点[RAG重排异常] query=%r 降级为 RRF 排序", query[:80])
            ranking = [(index, 0.0) for index in range(min(top_k, len(candidates)))]
        hits = []
        for index, score in ranking:
            candidate = candidates[index]
            candidate["score"] = score
            hits.append(candidate)
        kept = [hit for hit in hits if hit["score"] >= settings.rag_min_score]
        logger.info(
            "节点[RAG检索完成] query=%r rerank=%s kept=%s top_score=%.6f latency=%.2fs",
            query[:80], len(hits), len(kept), hits[0]["score"] if hits else 0.0, time.monotonic() - started,
        )
        for index, hit in enumerate(kept, 1):
            logger.info(
                "节点[RAG命中] #%s rerank=%.6f rrf_rank=%s title=%r source=%r snippet=%r",
                index, hit["score"], hit["rrf_rank"], hit["title"], hit["source"], hit["chunk_text"][:120],
            )
        return kept
    except Exception:
        logger.exception("节点[RAG检索异常] query=%r", query[:120])
        raise


async def medical_rag_search(query: str, top_k: int = 3) -> str:
    """检索本地医学文献库（稠密+BM25 混合召回并经重排精选；语料以中文医学文献为主，检索词请使用中文，通用英文缩写可保留）。当用户询问医学或健康相关问题（疾病、症状、诊断、治疗、药物、临床研究等）时调用本工具，获取文献依据后再回答。非医学问题不要调用。"""
    try:
        hits = await rag_search(query, top_k)
    except Exception as exc:
        return f"医学文献检索失败：{exc}"
    if not hits:
        return "未在医学文献库中找到相关文献。请基于你自己的知识回答，并说明未找到文献支持。"
    lines = ["以下是与问题最相关的医学文献摘录（含相关性分数与来源）："]
    for index, hit in enumerate(hits, 1):
        lines.append(
            f"[{index}] ({hit['source']}, score={hit['score']:.4f}) {hit['title']}\n"
            f"URL: {hit['url']}\n{hit['chunk_text']}"
        )
    return "\n\n".join(lines)
