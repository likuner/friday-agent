# 将 medical_rag_search 抽取到 mcp-server

## 目标
医学文献混合检索（ES knn+BM25/RRF/rerank）从 backend 进程内 FunctionTool 迁移为 mcp-server 公共 MCP 工具；ES 依赖与凭据收敛到 mcp-server 一处，其他 agent 项目经 stdio / http+Bearer 直接复用。

## mcp-server 侧

1. **`app/config.py`**：新增 `es_url / es_username / es_password / es_verify_certs / es_index`、`embedding_base_url / embedding_model / embedding_dimensions`、`rerank_model`、`rag_recall_k=20 / rag_candidate_k=10 / rag_top_k=3 / rag_min_score=0.0`（默认值照抄 backend）。
2. **新建 `app/tools/rag_search.py`**：整体移植 backend/app/rag.py（`embed_query / _hybrid_recall / _rerank / rag_search`，含 ES 客户端单例+锁、rerank 失败降级 RRF），注册：
   ```python
   @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
   async def medical_rag_search(query: str, top_k: int = 3) -> str
   ```
   docstring 原样保留（模型判断何时调用的依据）；logger 改 `mcp-server.rag`，节点日志格式不变。
3. **`requirements.txt`** 加 `elasticsearch`（版本对齐 backend）；`.env.example` 与 `.env` 加入 ES/RAG 配置（凭据从 backend/.env 抄入）。
4. **`README.md`**：目录结构与工具清单补 `rag_search.py` / `medical_rag_search`，配置项表加 ES/RAG 各行。

## backend 侧

5. **`app/agent.py`**：删 `FunctionTool(medical_rag_search)` 与 `from .rag import`；MCP 客户端挂载条件改为**只要 `mcp_server_url` 配置就挂载**，`enable_tools` 动态拼装 `["medical_rag_search"] + (["web_search"] if web_search else [])`，保持联网开关语义；更新超时注释（60s 需覆盖 embedding 30s + rerank 30s 的 RAG 链路）。
6. **删除 `app/rag.py`**；**`app/config.py` 清理**：删 ES/RAG 共 13 项及零引用的遗留死配置 `medrag_db_url`，**保留** `zhipu_api_key / glm_base_url / glm_model`（memory.py、summarizer.py 在用）；`requirements.txt` 移除 `elasticsearch`。
7. **提示词不改正文**：prompts.py 与 SKILL.md 中的 `medical_rag_search` 短名引用保持不变（MCP 注册后模型看到 `mcp__common_tools__` 前缀全名，语义匹配不受影响），仅更新注释。

## 验证

8. 重启 mcp-server，`scripts/smoke_test.py` 验证 `medical_rag_search` 真实检索可用；backend 冒烟：医学提问走 MCP 链路、非医学不触发、联网开关关时模型看不到 web_search；跑 backend pytest（test_skills.py 断言不受影响）；确认其他项目可用 README 的 stdio/http 方式 `tools/list` 到新工具。

## 已知权衡（接受才做）

- **可用性耦合**：mcp-server 不可达时医学检索静默消失，医学问答退化为无文献依据（现状是进程内永不消失）；单机 start.sh 托管 + 端口检查，风险可接受。
- **多一跳 localhost 网络**，相对秒级检索耗时可忽略；ES 凭据从 backend/.env 移到 mcp-server/.env（zhipu key 本就双份同值，不新增负担）。