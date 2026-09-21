"""Agent 评测脚本。

构造医学/非医学用例，验证 Friday Agent 的工具决策与回答质量：
- 医学问题：模型应自主调用 medical_rag_search 工具，回答非空且含相关关键词；
- 非医学问题：模型不应调用工具。

运行轨迹用 agentscope 内置的 ConsoleRenderer 做终端可视化（逐事件渲染
文本增量、思考、工具调用与工具结果）；LLM 裁判需要配置 EVAL_JUDGE_API_KEY，
未配置时自动跳过（key 暂时留空，先跑通逻辑）。

运行：.venv/bin/python scripts/eval_agent.py [--verbose]
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentscope.console import ConsoleRenderer  # noqa: E402
from agentscope.event import EventType  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402

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
    """跑单条用例：消费 agent 原生事件流，ConsoleRenderer 实时可视化。"""
    service = AgentService()
    agent = service._agent
    if not agent:
        raise RuntimeError("Agent 未初始化：请配置 MODEL_PROVIDER=deepseek 与 OPENAI_API_KEY")

    renderer = ConsoleRenderer(verbosity="default" if verbose else "quiet")
    answer_parts: list[str] = []
    tool_calls: list[str] = []

    async for event in agent.reply_stream(UserMsg(name="user", content=case["query"])):
        renderer.render(event)
        if event.type == EventType.TEXT_BLOCK_DELTA:
            answer_parts.append(event.delta)
        elif event.type == EventType.TOOL_CALL_START:
            tool_calls.append(event.tool_call_name)

    return {"tool_calls": tool_calls, "answer": "".join(answer_parts)}


async def main() -> None:
    setup_logging()
    verbose = "--verbose" in sys.argv
    if not (AgentService()._agent):
        print("Agent 未初始化：请配置 MODEL_PROVIDER=deepseek 与 OPENAI_API_KEY")
        return

    print(f"\n=== Agent 评测（{len(CASES)} 条用例，agentscope ConsoleRenderer 可视化）===")
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
