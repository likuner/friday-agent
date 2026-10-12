# 抽取「医学文献检索」流程为 AgentScope Skill

## 调研结论（为什么选它）

后端各流程评估：滚动摘要/长期记忆/上下文组装是模型从不调用的后台流程，SSE/权限确认是基础设施，均不适合 skill 形状；**医学文献检索**（`prompts.py:6-19` 系统提示词中最重的 ~14 行硬规则 + `rag.py:188` 工具）是面向 agent 的程序性知识，正是 SKILL.md 的设计目标。

AgentScope 2.0.8 原生 skill 机制（项目尚未使用）：
- `LocalSkillLoader(directory, scan_subdir)` 扫描含 frontmatter 的 SKILL.md（须有 name/description）
- `Toolkit(skills_or_loaders=[...])` 注册进 basic 组（不涉及 ToolGroup，符合既有决策）
- `Agent._get_system_prompt()` 自动追加 `<agent-skills>` 清单（仅 name/description/dir）
- 模型经只读元工具 `Skill`（自动 ALLOW，免权限确认）按需读取 SKILL.md 全文 → 渐进式披露

用户已选定**纯 skill 路由**：SYSTEM_PROMPT 完全移除医学块，路由全靠框架注入的清单 + frontmatter description。

## 实施步骤

### 1. 新建 `backend/skills/medical-literature-search/SKILL.md`
- frontmatter：`name: medical-literature-search`；`description` 为**触发条件全集**（疾病与病因/症状与体征/诊断与检查/治疗与用药/手术与预后/流行病学与预防/营养与运动处方/心理健康/医学名词解释/身体状况描述与就医咨询）——纯路由下这是唯一触发器，须全面
- 正文迁移 `prompts.py:11-19` 的完整流程：先检索后回答；中文检索词（英文缩写 AFP/TACE 可保留）+ 多角度多次调用示例；引用注明文献标题与 URL；未命中/不相关的降级话术（"本地文献库未找到相关依据，以下基于通用医学知识"）；急症/用药剂量/诊疗决策的就医提醒；非医学问题不调用
- 注意：`SkillViewer` 只返回 SKILL.md 单文件内容，不列目录 → 单文件承载全部内容，不建 references/

### 2. `backend/app/agent.py`
- 模块级 `_SKILL_LOADER = LocalSkillLoader(directory=str(Path(__file__).resolve().parents[1] / "skills"), scan_subdir=True)`（mtime 缓存跨请求共享；目录缺失仅告警降级，不影响对话）
- `_build()` 的 `Toolkit(...)` 加 `skills_or_loaders=[_SKILL_LOADER]`（始终注册；未来加技能=丢目录，零代码改动）
- `_TOOL_QUERY_KEYS` 增加 `"skill"`，前端 chip 显示技能名
- `stream()` 仿 MCP 探测模式加 `节点[Skill注册] skills=[...]` 日志

### 3. `backend/app/prompts.py`
- SYSTEM_PROMPT 删除整个【医学问题必须先检索知识库】块，保留人设、【会话上下文是有效事实】、中文规则；更新模块注释
- WEB_SEARCH_PROMPT / AGENT_TOOLS_PROMPT 中"医学优先 medical_rag_search"的一行交叉引用保留（开关开启时是额外触发信号）

### 4. 新增 `backend/tests/test_skills.py`（无网络无 DB 风格）
- loader 能发现技能，frontmatter name/description/markdown 完整
- `Toolkit(skills_or_loaders=[...])` 后 `get_tool_schemas()` 含 `Skill` 元工具；`get_skill_instructions()` 含技能名
- description 覆盖关键触发词（疾病/症状/治疗/用药）——守护纯路由质量
- markdown 提及 medical_rag_search

### 5. 验证
- `cd backend && .venv/bin/python -m pytest tests/ -q` 全部通过
- 可选端到端冒烟：医学问题 → 日志出现 Skill 调用 + medical_rag_search；非医学问题 → 不读技能不检索

## 不改动
rag.py、mcp-server、start.sh（skills 随仓库分发，路径相对 app 包解析，uvicorn --reload 自动生效）、前端、config.py。

## 已知取舍（纯路由）
模型可能偶发跳过读 skill 直接凭记忆回答（尤其简短健康闲聊），description 写全是主要缓解手段；`<agent-skills>` 注入块为框架英文模板、描述为中文，混排不影响路由。