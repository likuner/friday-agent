"""RAG 检索评测脚本。

对 med-pgvector 医学文献库执行一组 golden 查询，计算 Hit@K / MRR / 平均分，
并输出检索过程明细。评测裁判（LLM 相关性打分）需要配置 EVAL_JUDGE_API_KEY，
未配置时自动跳过该环节（逻辑跑通，key 暂时留空）。

运行：.venv/bin/python scripts/eval_rag.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag import rag_search  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("friday.eval_rag")

# golden 评测集：query + 期望命中的文献标题关键词（不区分大小写）
CASES = [
    {"query": "type 2 diabetes mellitus management", "expect": "type 2 diabetes"},
    {"query": "二型糖尿病的流行病学", "expect": "diabetes"},
    {"query": "帕金森病的主要症状有哪些", "expect": "parkinson"},
    {"query": "hepatitis c treatment", "expect": "hepatitis"},
    {"query": "阿片类药物过量如何急救", "expect": "opioid"},
    {"query": "膝关节半月板损伤", "expect": "meniscus"},
    {"query": "唐氏综合征是什么病", "expect": "down"},
    {"query": "胃癌的早期表现", "expect": "stomach"},
]

TOP_K = 4


async def judge_relevance(query: str, chunk: str) -> float | None:
    """LLM 相关性裁判。key 未配置时返回 None（跳过）。"""
    api_key = os.getenv("EVAL_JUDGE_API_KEY", "")
    if not api_key:
        return None
    import httpx

    prompt = (
        "判断以下文献摘录与用户问题的相关性，只回复 0-2 的整数"
        "（2=直接相关，1=部分相关，0=不相关）。\n"
        f"问题：{query}\n文献摘录：{chunk[:600]}"
    )
    resp = await httpx.AsyncClient(timeout=30).post(
        f"{os.getenv('EVAL_JUDGE_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')}/chat/completions",
        json={"model": os.getenv("EVAL_JUDGE_MODEL", "glm-4-flash"), "messages": [{"role": "user", "content": prompt}]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    text = resp.json()["choices"][0]["message"]["content"]
    for value in (2, 1, 0):
        if str(value) in text:
            return float(value)
    return None


async def main() -> None:
    setup_logging()
    started = time.monotonic()
    logger.info("评测[RAG开始] cases=%s top_k=%s", len(CASES), TOP_K)
    print(f"\n=== RAG 检索评测（{len(CASES)} 条用例，top_k={TOP_K}）===\n")

    results = []
    for case in CASES:
        hits = await rag_search(case["query"], TOP_K)
        titles = [hit["title"].lower() for hit in hits]
        expect = case["expect"].lower()
        rank = next((i + 1 for i, t in enumerate(titles) if expect in t), None)
        hit_at_k = rank is not None
        mrr = 1.0 / rank if rank else 0.0
        top_score = hits[0]["score"] if hits else 0.0
        judge_scores = []
        for hit in hits:
            score = await judge_relevance(case["query"], hit["chunk_text"])
            if score is not None:
                judge_scores.append(score)
        entry = {
            "query": case["query"],
            "expect_keyword": expect,
            "hit": hit_at_k,
            "rank": rank,
            "mrr": round(mrr, 3),
            "top_score": round(top_score, 4),
            "titles": [hit["title"][:50] for hit in hits],
        }
        if judge_scores:
            entry["judge_avg"] = round(sum(judge_scores) / len(judge_scores), 2)
        results.append(entry)
        status = "PASS" if hit_at_k else "MISS"
        logger.info(
            "评测[RAG用例] %s query=%r rank=%s top_score=%.4f titles=%s",
            status, case["query"], rank, top_score, entry["titles"],
        )
        print(f"[{status}] {case['query']}")
        for i, hit in enumerate(hits, 1):
            marker = " <- 命中" if expect in hit["title"].lower() else ""
            print(f"    {i}. ({hit['score']:.4f}) {hit['title'][:60]}{marker}")

    hit_rate = sum(1 for r in results if r["hit"]) / len(results)
    mrr = sum(r["mrr"] for r in results) / len(results)
    avg_score = sum(r["top_score"] for r in results) / len(results)
    summary = {
        "cases": len(results),
        "hit_rate": round(hit_rate, 3),
        "mrr": round(mrr, 3),
        "avg_top_score": round(avg_score, 4),
        "judge_enabled": any("judge_avg" in r for r in results),
        "duration_s": round(time.monotonic() - started, 2),
        "results": results,
    }
    print("\n=== 汇总 ===")
    print(f"Hit@{TOP_K}: {summary['hit_rate']}  MRR: {summary['mrr']}  平均Top分: {summary['avg_top_score']}")
    if not summary["judge_enabled"]:
        print("（LLM 裁判未启用：未配置 EVAL_JUDGE_API_KEY，已跳过相关性评分）")
    out = Path(__file__).resolve().parent.parent / "logs" / "eval_rag.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("评测[RAG完成] hit_rate=%s mrr=%s duration=%.1fs 报告=%s", hit_rate, mrr, summary["duration_s"], out)


if __name__ == "__main__":
    asyncio.run(main())
