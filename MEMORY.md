# Friday Agent 记忆系统：会话级与用户级记忆的实现文档

> 本文描述**当前已上线**的记忆系统实现（核对基准：2026-09-23，基于 backend 代码与 AgentScope 2.0.8）。
> 历史方案书已被本文档取代；实现过程中相对原方案的简化决策见 §8。
> 核心原则：**记忆的唯一事实来源是 PostgreSQL 业务库，进程内不持有任何跨请求状态。**

---

## 一、TL;DR

| 层 | 存储 | 写入 | 读取 |
| --- | --- | --- | --- |
| **会话级记忆**（短期） | `messages` 表（全文，原有）+ `conversations.summary` / `summary_upto_message_id`（滚动摘要两列） | 消息落库（原有）+ 溢出段异步压缩成摘要 | 每轮按 token 预算取「摘要 + 最近若干轮」显式回放 |
| **用户级记忆**（长期） | `user_memories` 表，**每用户一行**聚合文本 | 每轮后台 GLM 合并式抽取（旧记忆 + 新消息段 → 重写全文） | 每轮查一行 → 去重 → 拼进 system prompt |

每轮模型上下文的组成：

```
┌──────────────── 每轮模型上下文（预算受控，无状态重建）────────────────┐
│ ① system prompt + 工具定义 + 用户长期记忆块（≤500 tok，单行聚合）    │
│ ② 会话滚动摘要（AgentState.summary，框架自动前置注入，~500 tok）     │
│ ③ 回放窗口：预算内最近若干轮纯文本（observe 回放）                    │
│ ④ 本轮用户消息 + 图片附件（reply_stream 输入，图片只随本轮发）        │
└──────────────────────────────────────────────────────────────────┘
```

原始问题（跨用户串味、落库历史从不回放、重启失忆、无预算无压缩、并发中止连锁、切开关丢上下文）已随无状态化架构全部消除。

---

## 二、会话级记忆实现（会话内上下文管理）

### 2.1 无状态回放（核心）

每轮请求用库中数据重建一次性 Agent，`app/agent.py`：

```python
state = AgentState(session_id=str(conversation_id), summary=带标注头的会话摘要)
agent = Agent(name="Friday", system_prompt=..., model=..., toolkit=..., state=state)
await agent.observe(replay_msgs)          # 回放历史（纯文本 user/assistant）
async for event in agent.reply_stream(本轮消息):  # 本轮输入（含图片）
```

- `AgentService` 不再缓存 Agent 实例（原按"深度思考×联网搜索"组合缓存的单例是串味根因）；
- `DeepSeekCredential` 仍缓存（无状态对象）；
- system prompt 中含【会话上下文是有效事实】指令：会话内用户已确认的信息（城市/天气/偏好等）须直接沿用并注明来源，禁止以"无法获取实时数据"为由拒绝——该指令修复过"模型看到历史却不敢用"的问题。

### 2.2 消息清洗（`app/context.py` 的 `clean_for_replay`）

回放强约束（AgentScope 校验）：只允许 user/assistant 纯文本。清洗规则：

| 数据 | 处理 |
| --- | --- |
| 用户/助手消息正文 | ✅ 回放 |
| 思考过程（`meta.thinking`） | ❌ 仅前端展示 |
| 工具调用记录（`meta.toolCalls`） | ❌ 仅前端渲染检索 chip |
| 图片附件 | ❌ 不重发，内容前置 `[图片]` 占位（附件预算只留本轮） |
| 空内容（中止生成的空回复） | ❌ 整条丢弃 |

### 2.3 token 预算与窗口切分（`build_context_window`）

- token 计数为**字符近似**（`estimate_tokens`：中文 ÷1.6 + 其余 ÷4），不引 tokenizer——预算是软约束，精度够用；
- 切分：早于等于 `summary_upto_message_id`（摘要游标）的消息跳过；其余**从最新向前累加**塞满 `context_token_budget` 即停，单条超剩余预算整条不塞（不切半条）；
- 结果三段：`replay`（回放窗口）+ `overflow`（溢出段，待压缩）+ 游标前（已压缩）。不变量：三段拼回等于全部消息；
- 本轮用户消息不在窗口内（走 `reply_stream` 输入），窗口为空不影响当轮对话。

### 2.4 滚动摘要（`app/summarizer.py`）

| 项 | 实现 |
| --- | --- |
| 生成 | GLM（glm-4-flash）合并「旧摘要 + 溢出段」→ 新摘要，覆盖写回；提示词要求保留用户偏好/已确认事实/决策结论/未决问题，新信息覆盖旧信息 |
| 触发 | chat.py 装配窗口后：溢出 ≥ `summary_min_overflow_tokens` 即 `create_task` 后台执行（不阻塞对话、不依赖回复成败） |
| 幂等 | `summary_upto_message_id` 游标推进到溢出段末条；重算窗口天然幂等 |
| 并发 | 每会话 `asyncio.Lock`（弱引用字典，防泄漏）；压缩中重复触发直接跳过 |
| 失败 | 任何异常只记日志；游标未推进 → 下轮发现溢出仍在自动重试（自愈） |
| 注入 | 构造 `AgentState.summary` 时加标注头，框架自动 prepend 到模型上下文 |
| 降级 | 未配置智谱密钥 → 摘要停用（warning 一次），窗口照常截断（退化为丢弃早期上下文） |

压缩节奏为锯齿形：压缩一次清零溢出 → 随对话重新累积 → 攒够阈值再压。每次都是增量合并，摘要体积稳定在 `summary_max_tokens` 内。

---

## 三、用户级记忆实现（跨会话，单行聚合版）

### 3.1 存储

```sql
user_memories: id UUID PK | user_id UNIQUE FK(users.id, CASCADE) | content Text | updated_at
```

**每用户只有一行**，`content` 是 `- ` 分行的条目清单（如 `- 城市：杭州\n- 偏好：紫色`）。
抽取触发进度记录在 `conversations.memory_extracted_upto`（游标列，同摘要模式）。

### 3.2 读取注入（每轮，`load_memory_block`）

一次单行查询（唯一索引，亚毫秒，复用请求 session）→ `_dedupe_lines` 行级去重 → 套标注头拼进 system prompt：

```
【用户长期记忆（来自历史对话，可能过时；与最近对话冲突时以最近对话为准）】
- 城市：杭州
- 偏好：紫色
```

读取失败降级为 None（本轮不注入，不阻塞对话）。不缓存到进程内存——与"库是唯一事实来源"原则一致，成本对比模型调用可忽略。

### 3.3 抽取合并管线（`extract_user_memory`，后台异步）

与滚动摘要完全同构：独立 SessionLocal + 每会话锁 + 游标幂等 + 失败下轮自愈。

- **触发**：chat.py 每轮检查 `messages_since_extract >= memory_extract_min_messages`（当前配置为 1，即每轮判断一次）；
- **输入**：游标之后的新消息（上限 30 条）+ 旧记忆全文；
- **GLM 合并提示词**：保留仍成立的旧条目、并入新信息（身份/城市/职业/长期偏好/明确说"记住"的必收）；不收录临时上下文、寒暄、健康等敏感信息；冲突以新为准删除旧条目；语义重复只留一条；总长 ≤ `memory_max_tokens`；
- **输出纯文本**（非 JSON），写回前 `_dedupe_lines` 行级去重兜底（拦截模型复读同一行）；
- 空回视为模型失误：不动库、不推游标，下轮重试同一段。

### 3.4 大小与增长控制

| 闸口 | 机制 |
| --- | --- |
| 记忆体积 | 合并提示词限定 ≤500 tok（软限）+ GLM `max_tokens=1000`（硬顶）；数据库 Text 列不构成约束 |
| 抽取节奏 | `memory_extract_min_messages`（当前 =1 每轮） |
| 重复 | 提示词"语义重复只留一条" + 写回/读取双重行级精确去重 |

---

## 四、一轮请求的完整时序

```
用户发送消息 POST /conversations/{id}/messages
 ├─ 鉴权 → 用户消息落库 → 标题改写（原有逻辑）
 ├─ 查用户记忆行 → memory_block（长期记忆注入块）
 ├─ build_context_window → replay / overflow / 摘要游标
 ├─ 后台触发（互不阻塞）：溢出 ≥ 阈值 → 滚动摘要压缩
 │                        游标后新消息 ≥ 阈值 → 用户记忆抽取合并
 ├─ SSE 流：observe(replay) → reply_stream(本轮消息) → 事件转发（原有逻辑）
 └─ assistant 回复落库（含 meta.thinking/toolCalls，原有逻辑）
```

两个后台任务均为 fire-and-forget、独立 session、失败自愈，不影响回复流。

---

## 五、配置参数（`app/config.py`）

```python
# 会话级记忆
context_token_budget: int = 1000        # 回放窗口预算（开发调试值；设计默认 16000）
summary_max_tokens: int = 500           # 摘要目标长度
summary_min_overflow_tokens: int = 200  # 溢出触发压缩阈值（开发调试值；设计默认 2000）

# 用户级记忆
memory_enabled: bool = True             # 总开关；无智谱密钥时抽取自动停用，读取不受影响
memory_extract_min_messages: int = 1    # 新增消息数触发抽取（1 = 每轮判断）
memory_max_tokens: int = 500            # 记忆合并输出上限
```

摘要与抽取模型均复用 `glm_model`（glm-4-flash，免费档）与 `zhipu_api_key`。

---

## 六、代码地图

| 文件 | 职责 |
| --- | --- |
| `app/context.py` | token 近似估算、消息清洗、预算窗口切分（纯函数，`tests/test_context.py` 覆盖） |
| `app/summarizer.py` | 会话滚动摘要：GLM 合并压缩、锁、游标、自愈 |
| `app/memory.py` | 用户记忆：`load_memory_block` 读取注入、`extract_user_memory` 合并抽取、`_dedupe_lines` 去重（`tests/test_memory.py` 覆盖） |
| `app/agent.py` | 无状态 Agent 构造：`AgentState(session_id, summary)` + `observe` 回放 + system prompt（人设 + 长期记忆块 + 联网搜索段） |
| `app/chat.py` | 接线：读取记忆块 → 装配窗口 → 触发两个后台任务 → 调 stream |
| `app/models.py` | `conversations` 加 summary/summary_upto_message_id/memory_extracted_upto 三列；`user_memories` 表 |
| `app/db.py` | 启动 `create_all` + 幂等 `ADD COLUMN IF NOT EXISTS`（无 Alembic） |
| `app/config.py` | 全部预算/阈值参数 |

迁移方式：启动时幂等 DDL，`user_memories` 为单行聚合结构（由多行条目结构一次性迁移而来，存量数据已聚合回填）。

---

## 七、边界与失败策略

| 场景 | 行为 |
| --- | --- |
| 压缩/抽取任务失败、进程重启 | 只记日志；游标未推进 → 下轮自动重试（自愈） |
| 未配置智谱密钥 | 摘要与抽取停用（各 warning 一次）；回放窗口照常截断，长期记忆读取为空 |
| 同会话并发请求 | 摘要/抽取各有每会话锁，重复触发跳过 |
| 用户中止生成 | 部分回复已落库照常参与下轮回放；后台任务不依赖回复完成 |
| 新会话 / 预算内短会话 | 无摘要无溢出，全量回放，零额外开销 |
| 单条消息超回放预算 | 整条不塞半条，归入溢出段 |
| 「编辑重发」截断消息 | 游标消息被删时：摘要游标失配 → 退化为全量回放；抽取游标失配 → 计 0，等新消息自然触发 |
| 合并模型复读/漂移 | 行级精确去重兜底；携带旧全文合并降低丢条目概率 |
| 语义近似重复（"喜欢游泳"vs"游泳"） | 不做代码去重（误伤风险），交给合并提示词与将来的向量召回 |

---

## 八、设计决策与取舍（相对原方案书的变化）

1. **不用 AgentScope 内置 `context_config` 压缩**：它作用于进程内长寿命 Agent 的累积 context，与无状态回放架构冲突；且用主模型（DeepSeek）同步压缩，贵且在关键路径上。自研方案用免费 glm-4-flash 后台异步。读侧仍用框架原生能力（`AgentState.summary` 自动前置注入、`observe` 回放）。
2. **用户记忆从三层（画像表/事实条目表/向量旁表）简化为单行聚合**：在"无向量召回、无管理页面、全量注入"的选型下，多行结构的按条管理/召回能力用不上；单行 + GLM 合并重写与滚动摘要同构，代码量减半。代价：合并模型偶发漂移、将来按条管理需先拆回条目。
3. **token 字符近似**：预算是软约束；tiktoken 对 DeepSeek 分词同样不精确，不值得引依赖。
4. **摘要触发在流式开始前而非回复后**：单一触发点、不依赖回复成败/中止路径，正确性由游标幂等兜底。
5. **迁移用启动幂等 ALTER 而非 Alembic**：改动仅为加列，与现有 create_all 模式一致；将来建新表多了再上 Alembic。

---

## 九、已验证的验收场景（真实模型 + 真实库）

| 场景 | 结果 |
| --- | --- |
| 历史回放 | 回放"我叫小明喜欢紫色"后问颜色 → 答对（信息只在回放里） |
| 摘要注入 | 零回放、仅 AgentState.summary → 答对只存在于摘要里的信息 |
| 摘要压缩 | 旧摘要"杭州" + 新片段"换到上海" → 正确合并覆盖 |
| 跨用户隔离 | 无状态架构消除共享实例（原"紫色河马"串味根因） |
| 重启不失忆 | 上下文每轮从库重建 |
| 用户记忆跨会话 | 会话 A"记住我在杭州工作、喜欢紫色" → 新会话 B 答"杭州" |
| 记忆冲突覆盖 | "换到上海工作了" → 记忆中城市更新为上海、旧条目消失 → 新会话答"上海" |
| 单测 | `tests/test_context.py` + `tests/test_memory.py` 全绿（窗口切分不变量、去重、触发计数） |

---

## 十、未实现 / 演进方向

- **向量召回**（原方案 L2/L3）：给 `user_memories` 加 embedding 列、按当前问题 Top-K 召回，替换全量注入；届时注入管线不变，仅换选取方式。触发时机：条目多到 500 tok 装不下、或需要"上次那个方案"式历史检索。
- **「我的记忆」管理页面**：查看/编辑/删除记忆（遗忘权合规）。
- **Alembic 迁移**：表结构变更频繁时引入。
- **记忆时效**（`expires_at`）：当前靠"冲突覆盖"处理信息变更，无自动过期。

---

*配套阅读：[ISSUES.md](ISSUES.md)（原始问题实证）、[backend/ARCHITECTURE.md](backend/ARCHITECTURE.md)（后端文件级架构）。*
