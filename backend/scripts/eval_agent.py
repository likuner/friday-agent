"""Agent 评测脚本。

构造医学/非医学用例，验证 Friday Agent 的工具决策与回答质量：
- 医学问题：模型应自主调用 medical_rag_search 工具，回答非空且含相关关键词；
- 非医学问题：模型不应调用工具。

直接驱动 AgentService.stream() 的原生事件流（dict 事件，与 SSE 协议同构），
--verbose 时在终端实时渲染思考/文本增量/工具调用；LLM 裁判需要配置
EVAL_JUDGE_API_KEY，未配置时自动跳过。

运行：.venv/bin/python scripts/eval_agent.py [--verbose]
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import AgentService  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("friday.eval_agent")

CASES = [
    {
        "name": "医学问题应触发 RAG 工具",
        "query": "帕金森病的主要症状是什么？",
        "expect_tool": True,
        "answer_keywords": ["帕金森", "parkinson"],
    },
    {
        "name": "医学问题应触发 RAG 工具（跨语言）",
        "query": "丙型肝炎有哪些症状和传播途径？",
        "expect_tool": True,
        "answer_keywords": ["肝", "hepatitis"],
    },
    {
        "name": "非医学问题不应触发工具",
        "query": "用一句话介绍你自己",
        "expect_tool": False,
        "answer_keywords": ["friday", "助手", "智能"],
    },
]


async def judge_answer(query: str, answer: str) -> float | None:
    """LLM 裁判打分（1-5）。key 未配置时返回 None（跳过）。"""
    api_key = os.getenv("EVAL_JUDGE_API_KEY", "")
    if not api_key:
        return None
    import httpx

    resp = await httpx.AsyncClient(timeout=30).post(
        f"{os.getenv('EVAL_JUDGE_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')}/chat/completions",
        json={
            "model": os.getenv("EVAL_JUDGE_MODEL", "glm-4-flash"),
            "messages": [{
                "role": "user",
                "content": f"给以下 AI 回答的质量打 1-5 分（相关性、完整性），只回复数字。\n问题：{query}\n回答：{answer[:800]}",
            }],
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    text = resp.json()["choices"][0]["message"]["content"]
    for value in (5, 4, 3, 2, 1):
        if str(value) in text:
            return float(value)
    return None


async def run_case(case: dict, verbose: bool) -> dict:
    """跑单条用例：消费 AgentService 原生事件流（与 SSE 协议同构的 dict 事件）。"""
    service = AgentService()
    answer_parts: list[str] = []
    tool_calls: list[str] = []

    async for event in service.stream(
        conversation_id=uuid4(),  # 评测不落库：随机会话号即可（无内置工具时不触碰工作区）
        replay=[],
        summary=None,
        content=case["query"],
    ):
        kind = event.get("type")
        if kind == "text":
            answer_parts.append(event.get("content", ""))
            if verbose:
                print(event.get("content", ""), end="", flush=True)
        elif kind == "thinking":
            if verbose:
                print(f"\033[2m{event.get('content', '')}\033[0m", end="", flush=True)
        elif kind == "tool_call":
            tool_calls.append(event.get("name", ""))
            if verbose:
                print(f"\n\033[36m[工具调用] {event.get('name', '')} {event.get('query', '')}\033[0m", end="", flush=True)
    if verbose:
        print()
    return {"tool_calls": tool_calls, "answer": "".join(answer_parts)}


async def main() -> None:
    setup_logging()
    verbose = "--verbose" in sys.argv
    if not AgentService()._model_ready:
        print("Agent 未初始化：请配置 MODEL_PROVIDER=deepseek 与 OPENAI_API_KEY")
        return

    print(f"\n=== Agent 评测（{len(CASES)} 条用例，LangGraph 事件流{'，实时渲染' if verbose else ''}）===")
    started = time.monotonic()
    results = []
    for case in CASES:
        logger.info("评测[Agent用例开始] %s query=%r", case["name"], case["query"])
        print(f"\n--- 用例：{case['name']}  问题：{case['query']} ---")

        result = await run_case(case, verbose)
        answer, tool_calls = result["answer"], result["tool_calls"]

        tool_ok = (len(tool_calls) > 0) == case["expect_tool"]
        kw_ok = any(kw.lower() in answer.lower() for kw in case["answer_keywords"])
        judge = await judge_answer(case["query"], answer)
        passed = tool_ok and kw_ok
        entry = {
            "name": case["name"],
            "query": case["query"],
            "expect_tool": case["expect_tool"],
            "tool_called": bool(tool_calls),
            "tool_ok": tool_ok,
            "keyword_ok": kw_ok,
            "answer_chars": len(answer),
            "judge": judge,
            "passed": passed,
        }
        results.append(entry)
        logger.info(
            "评测[Agent用例] %s passed=%s tool_called=%s kw_ok=%s judge=%s chars=%s",
            "PASS" if passed else "FAIL", passed, bool(tool_calls), kw_ok, judge, len(answer),
        )
        print(f"  => {'PASS' if passed else 'FAIL'}（工具决策{'✓' if tool_ok else '✗'} 关键词{'✓' if kw_ok else '✗'}"
              f"{' 裁判分=' + str(judge) if judge is not None else ''}）")

    passed_count = sum(1 for r in results if r["passed"])
    summary = {
        "cases": len(results),
        "passed": passed_count,
        "judge_enabled": any(r["judge"] is not None for r in results),
        "duration_s": round(time.monotonic() - started, 2),
        "results": results,
    }
    print("\n=== 汇总 ===")
    print(f"通过 {passed_count}/{len(results)}")
    if not summary["judge_enabled"]:
        print("（LLM 裁判未启用：未配置 EVAL_JUDGE_API_KEY，已跳过质量打分）")
    out = Path(__file__).resolve().parent.parent / "logs" / "eval_agent.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("评测[Agent完成] passed=%s/%s duration=%.1fs 报告=%s", passed_count, len(results), summary["duration_s"], out)


if __name__ == "__main__":
    asyncio.run(main())
