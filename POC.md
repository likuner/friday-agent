# Friday Agent · POC 技术文档

> 一个验证「生产级 AI Agent 全链路」的 POC：**Next.js 16 前端 + FastAPI/AgentScope 后端 +
> 工具调用式 RAG + 全节点日志 + 自动化评测**。
> 本文档面向技术评审/面试场景：讲清楚系统做了什么、关键技术决策是怎么做的、
> 踩过哪些坑、下一步往哪走。
>
> 配套文档：[ISSUES.md](./ISSUES.md)（重点问题清单与四条演进路线的深度分析）

---

## 目录

- [一、项目概述](#一项目概述)
- [二、系统架构与技术栈](#二系统架构与技术栈)
- [三、已实现的核心功能](#三已实现的核心功能)
- [四、关键技术问题与解决过程](#四关键技术问题与解决过程)
- [五、评测体系与实测数据](#五评测体系与实测数据)
- [六、可观测性：全节点日志](#六可观测性全节点日志)
- [七、当前主要问题与挑战](#七当前主要问题与挑战)
- [八、未来迭代计划](#八未来迭代计划)
- [九、如何运行与演示](#九如何运行与演示)

---

## 一、项目概述

**Friday Agent** 是一个可以直接用的 AI Agent 对话产品（注册登录 → 多会话管理 → 流式对话 →
医学文献 RAG 检索 → 引用溯源），POC 阶段重点验证四件事：

| # | POC 验证目标 | 结论 |
| --- | --- | --- |
| 1 | 基于 AgentScope 的 Agent 能否承载真实产品交互（SSE 流式、工具调用、多会话） | ✅ 全链路可用 |
| 2 | LLM 自主决策的 RAG 工具调用（而不是关键词硬编码触发）是否可行 | ✅ 可行，且模型会自主将中文问题改写为英文检索词提升召回 |
| 3 | 前端如何把「流式 + 工具调用 + Markdown 富排版」做成原生产品体验 | ✅ 打字机缓冲、检索来源标签、引用块均已落地 |
| 4 | 全链路可观测（每一步决策留痕）+ 可评测（自动化指标）能否低成本低接入 | ✅ 节点日志 + OTel Tracing + 双评测脚本 |

**刻意保留的诚实结论**：当前 RAG 检索存在「查询向量模型与建库向量模型不匹配」的已知问题
（详见 §4.5），POC 阶段按计划不阻塞主链路，由评测体系如实暴露——这也是把评测做在早期的价值。

---

## 二、系统架构与技术栈

```
      ┌──────────────────────────────────────────────┐
      │            前端 Next.js 16 (App Router)       │
      │  React 19 · antd 5 · Tailwind · zustand       │
      │  SSE 流式渲染 · 打字机缓冲 · 响应式/移动端       │
      └───────────────┬──────────────────────────────┘
                      │ REST + SSE (fetch ReadableStream)
      ┌───────────────▼──────────────────────────────┐
      │         后端 FastAPI (app/main.py)            │
      │  ┌─────────────┐  ┌────────────────────────┐ │
      │  │ auth 路由    │  │ conversations/chat 路由 │ │
      │  │ JWT+验证码   │  │ SSE 生成器 + 节点日志    │ │
      │  └─────────────┘  └───────────┬────────────┘ │
      │  ┌────────────▼─────────────┐ │              │
      │  │ AgentScope Agent (ReAct) │ │              │
      │  │ DeepSeek + Toolkit       │ │              │
      │  │  └─ medical_rag_search 工具│ │              │
      │  └────────────┬─────────────┘ │              │
      └───────────────┼───────────────┘
                      │
     ┌────────────────┼──────────────────────────┐
     ▼                ▼                          ▼
┌──────────┐   ┌─────────────┐        ┌──────────────────┐
│ 业务库    │   │ med-pgvector │        │ 智谱 GLM API      │
│ Postgres │   │ pgvector     │        │ chat: DeepSeek    │
│ 用户/会话 │   │ 58 篇 PubMed │        │ embedding-3@512   │
│ /消息     │   │ 1024 chunks  │        └──────────────────┘
└──────────┘   │ HNSW 余弦索引 │
               └─────────────┘
```

| 层 | 技术 | 说明 |
| --- | --- | --- |
| 前端 | Next.js 16 (App Router) / React 19 / antd 5 / Tailwind / zustand | RSC + 客户端组件混合；响应式布局（桌面侧边栏可收起、移动端抽屉） |
| 后端 | FastAPI 0.115 / SQLAlchemy(async) / asyncpg / PyJWT + argon2 | 分层路由：auth / conversations / chat；SSE `StreamingResponse` |
| Agent 框架 | AgentScope 2.0.8 | `Agent`（ReAct 推理-行动循环）+ `Toolkit`/`FunctionTool` + 事件流 + 权限引擎 |
| 模型 | DeepSeek（对话）/ 智谱 GLM embedding-3（检索向量，512 维） | 均为 OpenAI 兼容协议 |
| RAG 存储 | pgvector (PG16) + HNSW 余弦索引 | 独立 Docker 容器，58 篇 PubMed 文献 / 1024 chunks |
| 可观测 | 结构化节点日志（控制台 INFO + 滚动文件 DEBUG）+ OpenTelemetry → AgentScope Studio | 全节点留痕 + trace 树 / token 用量可视化 |
| 评测 | 自研双评测脚本 + agentscope `ConsoleRenderer` 可视化 | RAG 检索指标 + Agent 行为断言，LLM 裁判位预留 |

---

## 三、已实现的核心功能

| 模块 | 功能 | 备注 |
| --- | --- | --- |
| 用户体系 | 注册 / 登录 / 图形验证码（Pillow 动态生成）/ JWT | 验证码抗干扰线、居中大字号 |
| 会话管理 | 创建 / 重命名 / 删除 / 历史记录搜索 | 侧边栏最近对话、移动端抽屉 |
| 流式对话 | SSE 逐 token 推送 + 前端打字机缓冲渲染 | 前后端解耦：后端控节奏，前端控流畅 |
| **RAG 检索** | `medical_rag_search` 工具，**由模型自主决策是否调用** | 医学问题触发；工具调用过程前端可见（检索标签） |
| 多模态 | 图片上传（≤9 张、内容嗅探校验）、base64 送模型、预览与失效降级 | 见 ISSUES.md 附录 A |
| 深度思考 / 联网搜索 | 思考过程流式展示；联网检索为占位开关 | 占位项已列入迭代计划 |
| 可观测 | 会话全流程节点日志；OTel Tracing → AgentScope Studio | 见 §6 |
| 评测 | RAG 检索评测 + Agent 行为评测，报告落 `logs/eval_*.json` | 见 §5 |
| 体验细节 | 深色主题（服务端渲染无闪烁）、中止生成、移动端响应式 | — |

---

## 四、关键技术问题与解决过程

> POC 真正的价值在「问题—定位—修复—验证」的完整闭环。以下六个问题均带实测复现与修复后验证，
> 是面试中可以展开讲的代表性案例。

### 4.1 React 19 兼容补丁在 App Router 下不生效

- **现象**：控制台报 `Warning: [antd: compatible] antd v5 support React is 16 ~ 18`。
- **定位**：补丁包 `@ant-design/v5-patch-for-react-19` 已安装且已在 `layout.tsx` 导入，但 antd v5 的
  静态方法（`message` / `Modal.confirm`）内部渲染仍走旧路径——因为 **`layout.tsx` 是 Server
  Component（需要导出 metadata，不能标 `'use client'`），从 RSC 对一个带 `'use client'`
  标记的包做副作用导入，浏览器端不会执行其副作用**。
- **修复**：新建 `providers.tsx`（`'use client'`）承载补丁导入，layout 用它包裹 children。
- **验证**：浏览器内挂钩 `console.error`，真实触发 `message.error`，捕获数 0。

### 4.2 首屏样式闪现（FOUC）

- **现象**：刷新页面时样式闪现错乱。
- **定位**：antd v5 是 CSS-in-JS，样式在客户端水合后才注入；实测 SSR HTML 中 antd 样式数为 0。
- **修复**：接入 `@ant-design/nextjs-registry`，服务端渲染时把样式内联进首屏 HTML
  （`<style id="antd-cssinjs">`）。
- **验证**：`curl` 抓取 SSR HTML 确认样式随 HTML 一起到达；首帧即完整样式。

### 4.3 流式输出卡在「正在思考…」

- **现象**：后端 SSE 正常推送（EventStream 里 token 不断到达），前端长时间停留在「正在思考…」。
- **定位**：前端的打字机 flush 定时器放在 `await streamMessage()` **之后**（`finally` 里）才启动
  ——流式期间内容只堆在缓冲区，整条流结束才一次性上屏。短回复测试掩盖了这个时序 bug，
  后端加节流后长回答（数百 token、数十秒）才暴露。
- **修复**：flusher 随发送**立即启动**；流结束后只等缓冲区放完再解除 loading。
- **验证**：长回答实测「流式进行中」内容已上屏 231 字，4 秒后增长到 274 字，完成后完整无截断。

### 4.4 工具调用挂起：AgentScope 权限引擎默认拦截

- **现象**：模型决策调用了 `medical_rag_search`，但事件流终止在 `REQUIRE_USER_CONFIRM`，
  工具从未执行、回答为空。
- **定位**：AgentScope 的权限引擎对工具默认要求人工确认；无头 SSE 场景没有人能点「允许」。
- **修复**：给 `FunctionTool` 显式配置 `PermissionDecision(behavior=ALLOW)`（只读检索，安全可放行）。
- **验证**：模型连续两次自主调用工具完成检索与回答；Agent 评测从 2/3 → 3/3。

### 4.5 RAG 检索分数异常低：用「自检索校验」二分定位

- **现象**：中文医学查询的向量检索 Top 分只有 ~0.08，命中文档完全不相关。
- **定位思路**（值得展开讲的方法论）：
  1. **自检索校验**：用语料自身的向量做查询——自身命中 1.0、同文档相邻块 0.84，
     证明存储/HNSW/度量方式完全正常；
  2. 于是问题收敛到**查询向量与建库向量不同源**：逐一排除假设——
     GLM embedding-3@512 直查（不符）、embedding-3 全 2048 维截断（不符）；
  3. 结论：语料入库时使用了另一个 512 维嵌入模型，跨模型查询必然失配。
- **当前处置**：主链路保留（评测如实暴露指标）；同时发现一个正面结论——**模型自主决策时会把
  中文问题改写成英文检索词**（如 `"genetics of Parkinson's disease inherited genetic risk
  factors LRRK2 GBA SNCA"`），此时 Top 分回升到 **0.63~0.65**，跨语言场景下检索质量大幅改善。
- **后续**：统一嵌入模型（用 GLM embedding-3@512 重建语料，或找回原建库模型），指标即恢复。

### 4.6 Markdown 渲染换行间隙

- **现象**：回答里段落之间出现巨大的空白间隙。
- **定位**：`.md` 容器误用 `white-space: pre-wrap`——把内容里每个换行符渲染成真实空行；
  实测段落间隔 39px = 12px margin + 27px 幽灵空行。
- **修复**：移除 `pre-wrap`，改用 `remark-breaks` 让单个换行转 `<br>`（诗歌仍正常分行），
  并补全被 Tailwind reset 吞掉的列表/标题/代码块排版。
- **验证**：段落间隔 39px → 12px；诗歌分行保留；无控制台报错。

---

## 五、评测体系与实测数据

评测脚本位于 `backend/scripts/`，报告落盘 `backend/logs/eval_*.json`。
设计原则：**规则断言先跑通逻辑，LLM 裁判位预留但 key 留空可跳过**（`EVAL_JUDGE_API_KEY`）。

### 5.1 Agent 评测（`eval_agent.py`）

- 用例：医学问题应触发工具（2 条，含跨语言）、非医学问题不应触发工具（1 条）。
- 断言：工具决策正确 + 回答包含关键词；LLM 裁判（1-5 分）预留。
- 可视化：复用 agentscope 内置 `ConsoleRenderer` 逐事件渲染运行轨迹（文本流、工具调用、工具结果）。
- **实测：3/3 通过**（16.5s）。

### 5.2 RAG 检索评测（`eval_rag.py`）

- 8 条 golden 查询（中英混合）+ 期望命中的文献关键词；指标：Hit@K、MRR、平均 Top 分。
- **当前实测：Hit@4 = 0，平均 Top 分 0.089** —— 即 §4.5 的嵌入模型失配问题，评测如实暴露。
  正面数据：LLM 自主改写的英文检索词 Top 分 0.63~0.65（真实产品链路的实际质量）。
- 改进路径明确：统一嵌入模型后重跑即可，评测框架无需改动。

### 5.3 评测的价值

- 在「接入更多功能」之前先建立质量基线；任何重构（如 §7 的记忆无状态化）都可以靠重跑评测兜底。
- 评测暴露问题的方式是**诚实的数字**，而不是「看起来能用」。

---

## 六、可观测性：全节点日志

会话链路的每个决策点都有结构化日志（控制台 INFO + `logs/backend.log` DEBUG 滚动文件）。
一次真实请求的完整轨迹：

```
节点[接收消息]  user=streamtest1 conversation=5918… content='请只回复：日志测试完成'
节点[流式开始]  conversation=5918…
节点[Agent调用] model=deepseek-chat prompt='请只回复：日志测试完成'
节点[工具调用]  model 自主决策调用工具 name=medical_rag_search
节点[RAG检索开始] query="genetics of Parkinson's disease inherited…" top_k=5
节点[RAG向量化] model=embedding-3 dims=512 latency=0.28s
节点[RAG检索完成] hits=5 kept=5 top_score=0.6518 latency=0.28s
节点[工具参数/工具结果]（DEBUG 级明细）
节点[模型事件]  seq=1..N type=text delta=…（DEBUG，逐 token）
节点[保存回复]  message_id=41226d27… chars=6
节点[流式完成]  events=3 chars=6 duration=0.79s
```

另有 OpenTelemetry Tracing 接入 AgentScope Studio（trace 树、token 用量、耗时，
见 ISSUES.md §6.3/附录 B）。日志让「模型为什么这么答」可以被回放——4.4 的权限拦截问题
就是靠事件流日志定位的。

---

## 七、当前主要问题与挑战

> 完整清单与修复方案见 [ISSUES.md](./ISSUES.md)，此处是面试视角的提炼。

| 级别 | 问题 | 为什么重要 |
| --- | --- | --- |
| **P0** | Agent 是进程级单例，对话状态跨会话/跨用户共享 | 已构造复现 + 真实发生「记忆串味」（隐私泄漏 + 答非所问）；同时**卡死多副本水平扩容**。修复方向明确：每轮从库里显式回放该会话历史（无状态化） |
| **P0** | 传入的 `history` 是死参数 | 上下文实际来自单例内部状态，重启即失忆 |
| **P0** | 无测试 / 无 CI / 无 Dockerfile / 无迁移 | 无法安全重构 P0 问题——它是其余一切的前置 |
| **P1** | 安全项：JWT 默认密钥、登录无限流、`/files` 静态目录无鉴权、提示词注入无防护 | 上线前必须逐项关闭 |
| **P1** | 有状态组件阻碍多副本：验证码存进程内存、附件存本地磁盘 | 迁 Redis / 对象存储 |
| **P1** | RAG 检索质量依赖嵌入模型匹配 | 建库模型与查询模型不一致时指标坍塌（§4.5），需要语料重建流程与模型版本管理 |
| **P2** | 性能体验：长会话无虚拟滚动、会话列表无分页、后端人为流式限速应移除 | 规模化前的体验债 |
| **P2** | 产品：联网搜索为占位实现、默认模型标记 sunset、医学场景缺固定免责声明 | 功能完整性 |

**面试视角的两点思考**：

1. **POC 与生产的距离主要在「状态管理」**——把 Agent 从有状态单例改成无状态服务，
   正确性、隐私、扩容三个问题同时解决；这也是 LLM 应用与传统 CRUD 服务最大的架构差异。
2. **评测必须先于重构**——没有 Agent/RAG 评测基线，任何核心链路改动都无法证明「没有变坏」。

---

## 八、未来迭代计划

按 ISSUES.md 的优先级路线图，分四个批次推进：

| 批次 | 目标 | 关键项 |
| --- | --- | --- |
| **第一批：正确性与安全**（立刻） | 让系统「可信」 | ① Agent 无状态化（显式回放 + token 预算，同时修死参数）② JWT 启动校验 + 登录限流 ③ `/files` 鉴权 ④ 补鉴权/会话/SSE 三条主链路测试 |
| **第二批：可迭代可运维**（1~2 迭代） | 让系统「可演进」 | ① CI 门禁（lint + 类型 + 测试 + 构建）② Dockerfile + Alembic 迁移 ③ 验证码迁 Redis、附件迁对象存储 ④ 聚合 metrics + 告警 |
| **第三批：能力扩展**（按价值） | 让产品「更强」 | ① PDF 文本抽取接入 RAG（复用现有链路）② 短期记忆滚动摘要 + 结构化用户画像 ③ 跨会话全局搜索 ④ 音频转写 |
| **第四批：规模化** | 让系统「可扩」 | ① 无状态多副本 + 自动扩缩 ② 长任务独立 worker/队列 ③ 模型网关（限流/重试/多供应商/计费）④ 长期记忆向量化事实条目（防记忆污染） |

**三条长期演进线**（详见 ISSUES.md §3/§4/§5）：Agent 记忆（短期回放 → 滚动摘要 → 长期画像与
事实条目）、多模态（图片 ✅ → PDF 文本 → 音频 → 视频）、云上线（容器化 → 托管化 → 多副本 → 生产加固）。

---

## 九、如何运行与演示

```bash
# 0. 依赖环境：Node 20+ / Python 3.12 / Docker（pgvector 容器 med-pgvector:5433）

# 1. 后端（端口 8000；.env 配置 DeepSeek/智谱 key、数据库连接）
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000

# 2. 前端（端口 3000）
cd frontend && npm install && npm run dev

# 3. 评测（无 LLM 裁判 key 也可运行，裁判位自动跳过）
cd backend && .venv/bin/python scripts/eval_rag.py
.venv/bin/python scripts/eval_agent.py          # 加 --verbose 看逐事件轨迹

# 4. 观察全节点日志
tail -f backend/logs/backend.log                # INFO 起控制台，DEBUG 起文件
```

**演示脚本建议**：注册登录 → 提问「帕金森病的主要症状」→ 指出界面上的
「已检索医学文献库」标签 → 翻看 `backend.log` 的节点轨迹 → 跑一遍两个评测脚本 →
打开 ISSUES.md 讲迭代计划。

---

*文档基于代码实测与 ISSUES.md（`997e922`）整理；所有指标均为真实运行数据。*
