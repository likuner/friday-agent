"""Agent 评测脚本（CrewAI 运行时）。

构造医学/非医学用例，验证 Friday Agent 的工具决策与回答质量：
- 医学问题：模型应自主调用 medical_rag_search 工具，回答非空且含相关关键词；
- 非医学问题：模型不应调用工具。

每条用例按 AgentService.stream() 的无状态路径现场组装一次性单 Agent
（build_single + MCP 白名单，不带历史回放/内置工具/权限链路），事件经
SSEEventBridge 收集（text 增量与工具 chip），--verbose 时把回答全文与
工具调用打到终端（替代原 agentscope ConsoleRenderer）；LLM 裁判需要配置
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crewai.events.event_bus import crewai_event_bus  # noqa: E402

from app.config import settings  # noqa: E402
from app.crew_agents import build_single  # noqa: E402
from app.crew_events import SSEEventBridge, StreamHub  # noqa: E402
from app.crew_mcps import list_mcp_tools, mcp_tools, probe_mcp_tools  # noqa: E402
from app.crew_skills import probe_skills  # noqa: E402

logger = logging.getLogger("friday.eval_agent")


def setup_logging() -> None:
    # 与 mcp-server/scripts/eval_rag.py 同款：评测脚本以终端输出为主，
    # 不接入 friday_shared 的服务日志（避免评测运行混入 backend.log）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

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


async def run_case(case: dict, index: int, verbose: bool) -> dict:
    """跑单条用例：每条现场组装一次性单 Agent，事件桥收集回答与工具调用。"""
    # 组装对齐 AgentService.stream()：MCP 白名单只放 medical_rag_search
    # （评测对象），web_search/内置工具/历史回放不参与
    mcp_tool_list = []
    if settings.mcp_server_url:
        specs = await list_mcp_tools(["medical_rag_search"])
        probe_mcp_tools(specs)
        mcp_tool_list = mcp_tools(specs)
    probe_skills()

    crew, agent_ids = build_single(
        settings.llm_model, False, [], case["query"],
        mcp_tool_list=mcp_tool_list or None,
    )
    hub = StreamHub()
    hub.bind()
    bridge = SSEEventBridge(hub, agent_ids)

    async def _run() -> None:
        try:
            await asyncio.to_thread(crew.kickoff)
        finally:
            crewai_event_bus.flush(timeout=10.0)
            hub.close()

    task = asyncio.create_task(_run())
    answer_parts: list[str] = []
    tool_calls: list[str] = []
    try:
        async for event in hub.events():
            if event["type"] == "text":
                answer_parts.append(event["content"])
                if verbose:
                    print(event["content"], end="", flush=True)
            elif event["type"] == "tool_call":
                tool_calls.append(event["name"])
                if verbose:
                    print(f"\n[工具] {event['name']} {event.get('query', '')}", flush=True)
    finally:
        hub.stop.set()
        bridge.close()
        if not task.done():
            task.cancel()
    if verbose:
        print()
    return {"tool_calls": tool_calls, "answer": "".join(answer_parts)}


async def judge_answer(query: str, answer: str) -> float | None:
    """LLM 裁判打分（1-5）。key 未配置时返回 None（跳过）。"""
    api_key = os.getenv("EVAL_JUDGE_API_KEY", "")
    if not api_key:
        return None
    import httpx

    resp = await httpx.AsyncClient(timeout=30).post(
        f"{os.getenv('EVAL_JUDGE_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')}/chat/completions",
        json={
            "model": os.getenv("EVAL_JUDGE_MODEL", "glm-5.3-flash"),
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


async def main() -> None:
    setup_logging()
    verbose = "--verbose" in sys.argv
    if not (settings.model_provider == "deepseek" and settings.llm_api_key):
        print("Agent 未初始化：请配置 MODEL_PROVIDER=deepseek 与 LLM_API_KEY")
        return

    print(f"\n=== Agent 评测（{len(CASES)} 条用例，CrewAI 事件桥可视化）===")
    started = time.monotonic()
    results = []
    for index, case in enumerate(CASES, 1):
        logger.info("评测[Agent用例开始] %s query=%r", case["name"], case["query"])
        print(f"\n--- 用例：{case['name']}  问题：{case['query']} ---")

        result = await run_case(case, index, verbose)
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
    # 报告与全部运行日志统一放仓库根 logs/（scripts → backend → 仓库根）
    out = Path(__file__).resolve().parents[2] / "logs" / "eval_agent.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("评测[Agent完成] passed=%s/%s duration=%.1fs 报告=%s", passed_count, len(results), summary["duration_s"], out)


if __name__ == "__main__":
    asyncio.run(main())
