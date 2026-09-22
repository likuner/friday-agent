# Friday Agent 记忆方案:短期记忆与长期记忆

> 记忆专题的完整方案书:现状盘点、目标架构、存储设计、读写时机、治理配套、选型决策与落地顺序。
> 问题的实证与全项目优先级论证见 [ISSUES.md](ISSUES.md) §一(P0)与 §三(记忆演进);本文档在其基础上补齐存储细节与工程衔接点。
> 核对基准:2026-09-22,基于 backend 当前代码与 AgentScope 2.0.8。

---

## 一、TL;DR

**现状:短期记忆靠进程内存,有六大问题;长期记忆完全没有。**

| 层 | 存哪 | 何时写 | 何时读 |
| --- | --- | --- | --- |
| 短期记忆(方案) | `messages` 表(全文,已有)+ `conversations.summary`(滚动摘要,新增列) | 现有落库逻辑不变;摘要由便宜模型异步压缩 | 每轮按 token 预算取「摘要 + 最近 K 轮」显式回放给模型 |
| 长期记忆(方案) | `user_profiles`(画像)+ `user_memories`(事实条目,含向量)+ `messages` 向量化(历史检索) | 对话结束异步抽取,低置信度先落候选 | 画像每轮注入 system;事实与历史按当前问题向量 Top-K 召回 |

一句话:**记忆的唯一事实来源是 PostgreSQL 业务库,进程内不再持有任何跨请求状态。**

---

## 二、现状与问题

### 2.1 短期记忆:六个问题

现状:唯一的"记忆"是 `AgentService` 单例里至多 4 个(按深度思考×联网搜索组合懒加载)AgentScope Agent 实例各自的 `state.context`(进程内存)。

| # | 问题 | 根因 |
| --- | --- | --- |
| 1 | **跨用户/跨会话串味**(隐私泄漏) | 进程级单例 + Agent 自带状态,全用户共享一份记忆;已两次实证(ISSUES.md §1.1:"紫色河马"复现、杭州污染高血压会话) |
| 2 | **落库历史从不回放** | `chat.py` 拼好的 `history` 传入后被丢弃,`agent.py stream()` 只取 `messages[-1]`;`history` 是死参数 |
| 3 | **重启失忆** | 记忆在内存,进程重启即失;因问题 2,库里的记录也不会被回放 |
| 4 | **无 token 预算、无压缩** | 上下文只增不减(含工具调用与检索结果全文),长会话成本线性涨直至溢出;AgentScope `context_config` 压缩未启用 |
| 5 | **并发中止连锁** | 一个会话点「停止生成」会中断另一个正在生成的会话(共享 Agent 实例) |
| 6 | **开关切换丢上下文** | Agent 按(深度思考, 联网搜索)组合缓存,同一会话中途切开关会跳到没有该会话历史的另一个 Agent |

### 2.2 长期记忆:缺失

无用户画像、无跨会话事实条目、无历史向量检索、无「我的记忆」管理入口。

### 2.3 当前存储位置

| 数据 | 位置 | 用途 |
| --- | --- | --- |
| 对话消息全文 | `messages` 表(PostgreSQL,Text + JSONB meta) | **仅展示用**,从不回放给模型 |
| Agent 对话状态 | 后端进程内存(`state.context`) | 事实上的"短期记忆",重启即失、全用户共享 |
| 长期记忆 | —— | 不存在 |
| AgentScope 自带设施 | `AgentState` 持久化(SQLAlchemy/Redis)、Mem0/AgenticMemory/ReMe 长记忆中间件 | **均未使用**(中间件只挂了 TracingMiddleware) |

---

## 三、总体架构

每轮模型上下文的组成(短期与长期如何汇合):

```
                        ┌────────────────────────────────┐
                        │        PostgreSQL 业务库        │
                        │  messages │ conversations │    │
                        │  user_profiles │ user_memories │
                        └───────┬───────────────┬────────┘
                                │ 每轮读取       │ 异步写入
                                │ (按预算截断)   │ (对话结束后抽取)
                                ▼               │
┌───────────────────── 每轮模型上下文(预算受控)─────────────────────┐
│ ① system prompt + 工具定义                    (固定)             │
│ ② 长期记忆注入:用户画像(全量) + 事实/历史召回(Top-K)  (上限 ~800 tok) │
│ ③ 会话滚动摘要                                (~500 tok)        │
│ ④ 最近 K 轮完整消息(短期记忆主体)              (弹性,撑满剩余预算) │
│ ⑤ 本轮用户消息 + 图片附件                      (固定)             │
└───────────────────────────── ▲ ────────────────────────────────┘
                                │ 无状态:每轮重建,用完即弃
                          Agent(AgentState 按会话构造)
```

要点:**Agent 不再是长寿命单例**。每轮请求用库中数据构造一次模型上下文,进程内零跨请求状态——问题 2.1 的 1/2/3/5/6 随之全部消失,多副本部署同步解锁。

---

## 四、短期记忆方案(会话内)

目标:同一会话连贯、跨会话隔离、不超预算、重启不失忆。

### 4.1 显式回放(核心,ISSUES.md §1.1 方案 A)

每轮从库里取该会话最近消息,显式构造模型上下文;`stream()` 不再依赖任何跨请求状态。

AgentScope 2.0.8 的正确姿势(包内已核实):

```python
# 每轮:按会话重建无状态上下文,替代单例 Agent 的内存累积
state = AgentState(session_id=str(conversation.id))
agent = Agent(name="Friday", system_prompt=..., model=..., toolkit=..., state=state)

await agent.observe(replay_msgs)      # 回放历史(纯文本 user/assistant 消息)
async for event in agent.reply_stream(None):   # None = 从当前 state 继续
    ...
```

约束(框架强校验,回放前必须清洗):

- 回放消息只允许 `user` / `assistant` 角色、**纯文本**;
- **不得包含 tool_call / tool_result / thinking 块**(会抛 `ValueError`)——历史里的 `meta.toolCalls`、`meta.thinking` 只用于前端展示,不回放;
- 历史图片附件**不重复送模型**(沿用现有设计):预算中附件额度只留给本轮,历史轮次回放为「[图片]」占位文本。

### 4.2 Token 预算

以 token 数为准(不是条数)。初版不引入 tokenizer 依赖,按字符近似估算(中文 ≈ 1.6 字符/token,英文 ≈ 4 字符/token),超限再换 tiktoken。

```
┌───────────── 模型上下文预算 ─────────────┐
│ system prompt + 工具定义       (固定)    │
│ 长期记忆注入(画像+召回)        (≤800 tok)│
│ 会话摘要(滚动压缩)            (~500 tok)│
│ 最近 K 轮完整消息              (弹性)    │
│ 本轮用户消息 + 附件            (固定)    │
└─────────────────────────────────────────┘
```

取数策略:从最新消息向前逐条累加,塞满「弹性区」即停;更早的部分进入摘要覆盖范围。

### 4.3 滚动摘要

| 项 | 设计 |
| --- | --- |
| 存放 | `conversations` 表新增两列:`summary Text` + `summary_upto_message_id UUID`(可空) |
| 生成 | 被预算截断的早期历史,用便宜模型(如 `glm-4-flash`,已有密钥)压缩;输入 = 旧摘要 + 新被截断段,输出覆盖写回 |
| 触发 | 对话结束(或下一轮开始前)异步执行,`summary_upto_message_id` 记录摘要覆盖进度,避免每轮重算 |
| 读取 | 每轮上下文 = `summary` + 从 `summary_upto_message_id` 之后的全文消息 |

### 4.4 存储一览(短期不新增任何组件)

| 数据 | 存储 | 变更 |
| --- | --- | --- |
| 会话消息全文 | `messages` 表 | 不变(已落库) |
| 滚动摘要 | `conversations.summary` | 新增两列 |
| 回放上下文 | 进程内存(每轮临时构造) | 不落盘、用完即弃 |

---

## 五、长期记忆方案(跨会话,三层)

### 5.1 三层结构

| 层 | 存储 | 写入时机 | 读取方式 |
| --- | --- | --- | --- |
| **L1 结构化画像** | `user_profiles`(`user_id` PK, `profile JSONB`, `updated_at`) | 用户显式告知("记住我喜欢…");或定期从对话抽取 | 每轮全量注入 system prompt(条数/体积上限) |
| **L2 事实条目** | `user_memories`(`id`, `user_id`, `content`, `embedding vector`, `confidence`, `source_message_id`, `created_at`, `expires_at`, `superseded_by`) | 对话结束异步抽取(便宜模型),低置信度先落候选状态 | 当前问题 embedding → 余弦 Top-K 召回,注入上下文 |
| **L3 历史检索** | `messages` 向量化(旁表 `message_embeddings`) | 消息入库后异步 | 「上次那个方案」式按相似度召回历史片段 |

向量统一复用现有链路:`zhipu embedding-3` @ 512 维(`rag.py embed_query` 已具备)。记忆是新写新查、同模型同维度,不存在 [backend/ARCHITECTURE.md](backend/ARCHITECTURE.md) §5.4 的嵌入模型失配问题。

### 5.2 治理配套(长期记忆最容易出事的地方)

| 治理项 | 要求 |
| --- | --- |
| **写入把关** | 只存稳定事实与偏好;低置信度落候选,确认后才生效;抽取 prompt 明确排除敏感信息(健康数据需单独授权) |
| **时效与冲突** | 条目带 `created_at` / `expires_at` / `superseded_by`;新事实**覆盖**旧事实而非并存(如"我换工作了") |
| **遗忘权(合规)** | 前端提供「我的记忆」页面:查看 / 单条删除 / 全清;删除即物理删除 |
| **注入标注** | 注入时标明「以下是历史记忆,可能过时」,避免模型当作当前事实陈述 |
| **成本控制** | 召回注入设条数与 token 上限;画像与召回结果按用户短缓存 |

### 5.3 存储一览(长期)

| 数据 | 存储 | 变更 |
| --- | --- | --- |
| 用户画像 | 新表 `user_profiles` | 新建 |
| 事实条目 + 向量 | 新表 `user_memories` | 新建(依赖 pgvector,见 §六) |
| 历史消息向量 | 旁表 `message_embeddings` | 新建(消息正文仍在 `messages`,向量只存增量) |

---

## 六、向量存储选型

业务库当前镜像是 `postgres:16-alpine`(**不带 pgvector**),而 `user_memories` / `message_embeddings` 需要向量列,必须先拍板:

| 方案 | 做法 | 优点 | 代价 |
| --- | --- | --- | --- |
| **A. 业务库启用 pgvector(推荐)** | `docker-compose.yml` 镜像换 `pgvector/pgvector:pg16`,业务库执行 `CREATE EXTENSION vector` | 记忆与业务数据**同库同事务**(消息抽取→写记忆天然一致);一套备份/迁移/副本 | 换镜像需重建容器(数据卷保留);开发库需一次性初始化 |
| B. 复用 med-pgvector(5433) | 在 medrag 库加记忆表 | 零基础设施改动 | 语义混杂(医学文献库);跨库无法 join;生命周期耦合 |
| C. 独立记忆库 | 再起一个 pgvector 实例 | 隔离干净 | 多一个组件,与"减少组件"的目标相悖 |

**结论:选 A。** 记忆写入与消息落库的一致性是最重要的理由;B 仅适合快速试验。

---

## 七、实现路线对比:自研 vs AgentScope 自带中间件

AgentScope 2.0.8 自带 `Mem0Middleware`(外部 mem0 服务)、`AgenticMemoryMiddleware`(Markdown 文件)、`ReMeMiddleware`,以及 `agentscope.app.storage` 会话持久化(SQLAlchemy/Redis)。

| | 自研三层(本文方案,推荐) | 挂 AgentScope 长记忆中间件 |
| --- | --- | --- |
| 依赖 | 零新增(复用业务库 pgvector + embedding 链路) | 引入 mem0 外部服务或接受文件存储 |
| 治理 | 写入把关 / 遗忘权页面 / 时效冲突完全可控,数据都在库里 | 中间件之上仍需自建治理层 |
| 短期记忆 | 必须自研(§4.1,中间件不解决串味/回放) | 不覆盖 |
| 结论 | 与现有架构(业务库 + 自有 RAG 链路)同构,体量小、可控 | 可作效果对照试验,不建议直接上生产 |

---

## 八、落地顺序

| 步骤 | 内容 | 验证方式 |
| --- | --- | --- |
| **1. 短期·显式回放 + token 预算(P0)** | 无状态化:`AgentState` 按会话每轮构造,`history` 变唯一事实来源;随 ISSUES.md §1.1 串味修复一起做 | ①"紫色河马"复现场景不再串味;②重启后继续会话上下文连贯;③两会话并发互不干扰、中止不再连锁;④中途切开关不丢上下文 |
| **2. 短期·滚动摘要** | `conversations` 加列 + 异步压缩 | 超长会话(50+ 轮)成本可控,早期信息仍可答 |
| **3. 长期·L1 用户画像** | `user_profiles` + 显式告知写入 + 注入 + 「我的记忆」页 | 跨会话记住偏好;页面可查看/删除 |
| **4. 长期·L2 事实条目 + 召回** | `user_memories` + 异步抽取 + Top-K 召回(需 §六选型落地) | 「上次那个方案」能召回;注入不超预算;错误记忆可删 |

> 每步独立可上线;步骤 1 同时是「多副本部署」(ISSUES.md §5)的前置条件。

---

## 九、代码衔接点

| 位置 | 改动 |
| --- | --- |
| `backend/app/agent.py` | `AgentService` 去单例化;`stream()` 改为接收完整回放消息列表,每轮构造 `AgentState`;组合缓存(深度思考×联网搜索)改为按轮装配参数而非按状态缓存实例 |
| `backend/app/chat.py` | `history` 从死参数变为唯一事实来源:预算取数 + 摘要拼接 + 长期记忆召回注入 |
| `backend/app/models.py` | `conversations` 加 `summary` / `summary_upto_message_id`;新表 `user_profiles` / `user_memories` / `message_embeddings`(引入 Alembic 迁移,见 ISSUES.md §6.2) |
| `backend/app/memory.py`(新建) | 记忆抽取 / 召回 / 治理服务(画像读写、条目抽取、向量召回、过期与覆盖) |
| `backend/app/config.py` | 预算与召回参数:上下文预算上限、摘要模型、召回 Top-K、注入上限 |
| `docker-compose.yml` | postgres 镜像换 `pgvector/pgvector:pg16`(§六方案 A) |
| `frontend` | 「我的记忆」页面(查看/删除,遗忘权);消息发送链路无感知 |

---

*本文档为记忆专题方案书,与 [ISSUES.md](ISSUES.md)(问题盘点与优先级)、[backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)(实现细节)配套阅读。*
