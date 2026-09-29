# Friday Agent RAG 检索：Elasticsearch 混合检索实现文档

> 本文描述**当前已上线**的医学文献检索实现（核对基准：2026-09-29，基于 backend 代码、
> Elasticsearch 9.5.4 与智谱开放平台接口实测）。实现集中于 `backend/app/rag.py`，
> 架构定位与全链路日志在 `backend/ARCHITECTURE.md` §5 有摘要版，本文是完整展开。
>
> 核心链路：**查询向量化 → 稠密 knn + BM25(IK) 双路召回 → RRF 融合取 top10 → rerank 精排取 top3**。

---

## 一、TL;DR

```
用户问题（经模型改写为中文检索词）
   │
   ▼
① embed_query()        智谱 embedding-3 → 1024 维查询向量
   │
   ▼
② _hybrid_recall()     Elasticsearch 并行两路召回（各取 top20 = RAG_RECALL_K）：
   │                      稠密：knn 查询 embedding 字段（HNSW，cosine）
   │                      稀疏：match 查询 chunk_text（BM25，IK 中文分词）
   │
   ▼
③ RRF 融合（应用层）    score = Σ 1/(60 + rank)，两路秩融合取前 10 条候选
   │
   ▼
④ _rerank()            智谱 rerank 模型交叉编码精排 10 条 → 取前 3
   │                    （失败自动降级为 RRF 序，检索不中断）
   ▼
⑤ 过滤返回             rag_min_score 阈值过滤 → 3 条文献给模型
```

| 环节 | 实现 | 参数 | 耗时实测 |
| --- | --- | --- | --- |
| 向量化 | httpx POST 智谱 `/embeddings` | embedding-3，dims=1024 | ~0.3s |
| 双路召回 | `AsyncElasticsearch.search` × 2（`asyncio.gather` 并行） | knn k=20 num_candidates=100；BM25 size=20 | ~0.02s |
| RRF 融合 | 纯 Python，秩常数 K=60 | 候选池 `RAG_CANDIDATE_K=10` | <1ms |
| rerank 精排 | httpx POST 智谱 `/rerank` | 模型 `rerank`，top_n=`RAG_TOP_K=3` | ~0.3s |

单次检索全程 0.6~0.8 秒。

---

## 二、检索流程分步详解

### ① 查询向量化（`embed_query`）

- POST `{EMBEDDING_BASE_URL}/embeddings`，body `{"model": "embedding-3", "input": [text], "dimensions": 1024}`，鉴权 `ZHIPU_API_KEY`；
- **1024 维必须与 ES 索引 `embedding` 字段维度一致**，否则 knn 查询直接报错；
- embedding 与 rerank、联网搜索共用同一个智谱密钥。

### ② 双路召回（`_hybrid_recall`）

两路查询用 `asyncio.gather` 并行发出，各自取前 `RAG_RECALL_K=20` 条（**单路深度 = 融合窗口 × 2**：
若单路也只取 10，任何一路第 11 名之后的文档就永远进不了候选池；单路深于融合输出是 ES 官方
RRF 示例的惯例做法。召回加深只影响 ES 查询——毫秒级且免费，不增加 rerank 费用）：

| 路 | 查询 | 命中的索引结构 | 擅长 |
| --- | --- | --- | --- |
| 稠密 | `knn: {field: embedding, k: 10, num_candidates: 100}` | dense_vector(1024, cosine, bbq_hnsw) | 语义相似（换说法也能召回） |
| 稀疏 | `query: {match: {chunk_text: <query>}}` | text，ik_max_word 索引 / ik_smart 检索 | 关键词精确匹配（术语、药名、编号） |

两路互补的典型例子：模型把用户口语改写成关键词后，BM25 对"抗蛇毒血清"这类术语的精确命中
比向量路更稳；而"呼吸困难怎么救"这类表述变化则靠向量路兜住。

### ③ RRF 融合（应用层，非 ES 原生）

对两路返回的文档按出现名次累加倒数分数：

```
score(doc) = Σ_legs  1 / (K + rank_leg(doc))     # K = 60，ES 默认值
```

- 同时被两路召回的文档分数叠加，天然获得提升；
- 融合后取前 `RAG_CANDIDATE_K=10` 条进入精排。

**为什么在应用层做**：ES 9.x 的 RRF retriever 是**付费许可功能**，Basic license 实测返回
`403 'current license is non-compliant for [RRF]'`。客户端按同一公式融合，数学上与服务端等价，
且不引入许可依赖。若未来集群升级了许可，可切回服务端 `rrf` retriever（一行查询体的改动）。

### ④ rerank 精排（`_rerank`）

- POST `{EMBEDDING_BASE_URL}/rerank`，body `{"model": "rerank", "query": …, "documents": [10 条候选的 chunk_text], "top_n": 3}`；
- 响应 `results[].{index, relevance_score}`，按分数降序取前 3；
- **注意模型名是裸的 `rerank`**（文档常见的 `rerank-2` 在该密钥下返回 1211 模型不存在）；
- rerank 是交叉编码（query 与文档联合建模），能识别召回阶段"字面不像但语义相关"的文档——
  实测能把 RRF 第 5~10 名的正确文档精准提进前 3；
- **降级策略**：rerank 调用失败（网络/限流/欠费）时打告警日志 `节点[RAG重排异常]`，
  自动改用 RRF 序取前 3，检索链路不中断。

### ⑤ 过滤与返回

- `rag_min_score` 作用于 **rerank 分数**（0~1），默认 0.0 全保留；
- 每条结果含 `title / source / url / chunk_text / score / rrf_rank`，由工具层
  `medical_rag_search` 格式化为带编号的文本返回给模型（格式含来源与 URL，配合系统提示词要求引用）。

---

## 三、Agentic 检索行为：为什么一次提问会有多条 query

这套 RAG 是 **Agent 自主调用**（agentic），不是"每问固定检索一次"的流水线：

1. **检索词由模型改写**：日志里的 query 不是用户原话，而是模型提炼的中文关键词
   （如用户问"我爸被蛇咬了现在呼吸困难怎么办" → 检索"蛇咬中毒 症状 呼吸困难"）。
   关键词化提高两路召回命中（向量对措辞敏感、BM25 依赖分词对齐）；
2. **多角度多次调用**：系统提示词（`prompts.py`）明确允许"必要时从多个角度多次调用
   （如病名+症状、病名+治疗）"，ReAct 循环内模型看到首次结果后可决定再查；
3. 每条 query 都是一次完整的 向量化→融合→精排 链路，因此日志中一次用户提问常出现
   多组 `节点[RAG检索开始]…节点[RAG检索完成]`。

代价是延迟与调用费随检索次数线性增长；如需收敛，改 `prompts.py` 中该句措辞即可（纯提示词调整）。

---

## 四、索引与数据

### 索引契约（mapping 存档：`backend/scripts/es/medical_chunks_v2.mapping.json`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `embedding` | dense_vector(1024, cosine, bbq_hnsw) | 稠密路用，dims 须与 embedding-3 一致 |
| `chunk_text` | text（ik_max_word 索引 / ik_smart 检索） | 稀疏路用 + 返回给模型的正文 |
| `title` | text（同上分析器） | 文档标题 |
| `source` / `source_url` | keyword | 来源标识（who_zh/jk39/a_hospital/xywy 等）与 URL |
| `document_id` / `chunk_index` / `lang` / `char_length` / `token_count` / `created_at` | — | 溯源与统计字段 |

### 别名切换的由来

- 语料由**外部项目**导入（664 chunks / 74 documents，中文医学文献：WHO 实况报道、
  医院百科等），初版 `chunk_text` 用默认 standard 分析器——中文只能单字切分，BM25 质量差；
- 2026-09-29 在 ES 内做了 `_reindex`：装好 IK 插件后重建为 `medical_chunks_v2`（IK 分析器，
  664 条零失败，**向量与 `_id` 原样保留**），删除原索引并把 `medical_chunks` 切为指向 v2 的别名；
- 应用只认别名 `medical_chunks`，后续再换分析器/重建索引时应用侧零改动；
- 旧 pgvector 库（med-pgvector 容器，5433）仅作原始语料留存，运行时不再访问。

---

## 五、配置项（backend/.env）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ES_URL` | `https://localhost:9200` | med-es 容器地址（TLS） |
| `ES_USERNAME` / `ES_PASSWORD` | `elastic` / 空 | basic auth，密码必填 |
| `ES_VERIFY_CERTS` | `false` | 自签证书默认不校验 |
| `ES_INDEX` | `medical_chunks` | 索引别名（→ medical_chunks_v2） |
| `RAG_RECALL_K` | `20` | 单路召回深度（knn k 与 BM25 size），应大于融合窗口 |
| `RAG_CANDIDATE_K` | `10` | RRF 融合候选池（rerank 前） |
| `RAG_TOP_K` | `3` | rerank 后最终返回条数 |
| `RAG_MIN_SCORE` | `0.0` | rerank 分数阈值（见 §6 分数饱和说明，实际别开） |
| `RERANK_MODEL` | `rerank` | 智谱精排模型（复用 ZHIPU_API_KEY / EMBEDDING_BASE_URL） |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | `embedding-3` / `1024` | 查询向量化（须与入库向量同源同维） |

---

## 六、日志与可观测

logger 为 `friday.rag`，输出两路：控制台 INFO + `backend/logs/backend.log`（DEBUG，5MB×3 滚动）。
一次完整检索的节点序列：

```
节点[RAG检索开始] query='蛇咬中毒如何急救' recall=20 candidates=10 top_k=3
节点[RAG向量化] model=embedding-3 dims=1024 latency=0.25s text='蛇咬中毒如何急救'
节点[RAG融合完成] hits=10 rrf_top='蛇咬中毒'
节点[RAG重排] model=rerank docs=10 top_n=3 latency=0.30s best=1.000000
节点[RAG检索完成] rerank=3 kept=3 top_score=1.000000 latency=0.61s
节点[RAG命中] #1 rerank=1.000000 rrf_rank=8 title='蛇咬中毒' source='who_zh' snippet='…'
```

`节点[RAG命中]` 字段：`#N` 是 rerank 后的最终名次；`rrf_rank` 是精排前在候选池的名次，
两者对比可见 rerank 的提权效果（如 `#1 … rrf_rank=8` 即从第 8 提到第 1）。

**分数饱和说明**：智谱 rerank 输出的是 sigmoid 饱和分数，基线极高——无关文档也有 0.96+，
医学对医学普遍 0.99+。**排序仍正确**（无关/相关区分在千分位），但绝对分数无区分度：
`RAG_MIN_SCORE` 阈值实际不适用（保持 0），日志/评测里的 top_score 只在 6 位小数下才有信息量
（因此代码用 `%.6f`）。若需要真正可阈值化的分数，需换打分更分散的 rerank 模型
（如 SiliconFlow 的 BAAI/bge-reranker-v2-m3）。

---

## 七、评测

`backend/scripts/eval_rag.py`：golden 查询 → `rag_search` → 按标题关键词判 Hit@K / MRR。

- **金标设计原则**：expect 关键词必须与语料实际标题（中文）匹配——旧英文关键词集
  （"opioid"/"parkinson"）对中文标题语料永远 MISS，属假阴性；且语料中本无糖尿病、
  帕金森等主题文档，选例需先确认语料覆盖；
- 当前用例 9 条（8 中文 + 1 英文跨语言探测 "measles vaccination"）；
- 当前结果：**Hit@4 = 1.0，MRR = 0.944**；
- 可选 LLM 裁判：配置 `EVAL_JUDGE_API_KEY` 后对每条命中做 0-2 相关性打分；
- 报告落盘 `backend/logs/eval_rag.json`。

---

## 八、部署与运维

### ES 容器（med-es，独立于 docker-compose 单独启动）

```bash
docker run -d --name med-es -p 9200:9200 \
  -e discovery.type=single-node -e ES_JAVA_OPTS=-Xms1g -Xmx1g \
  -v med-es-data:/usr/share/elasticsearch/data \
  -v med-es-config:/usr/share/elasticsearch/config \
  docker.elastic.co/elasticsearch/elasticsearch:9.5.4
```

默认 TLS + basic auth（用户 `elastic`，密码见启动日志），健康检查：

```bash
curl -sk -u elastic:$ES_PASSWORD https://localhost:9200/_cluster/health?pretty
```

### IK 插件（稀疏路依赖；容器重建后需重装——plugins 目录不在卷上）

```bash
docker exec med-es bin/elasticsearch-plugin install --batch \
  https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-9.5.4.zip
docker restart med-es
# 验证中文分词
curl -sk -u elastic:$ES_PASSWORD 'https://localhost:9200/_analyze' \
  -H 'Content-Type: application/json' -d '{"analyzer":"ik_smart","text":"二型糖尿病的流行病学"}'
```

IK 版本必须与 ES 版本**严格一致**；可用版本见 release.infinilabs.com/analysis-ik/stable/。

### 常见问题

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| ES 请求 401/`_security` 报错 | 密码未配置/已改 | 核对 `.env` 的 `ES_PASSWORD` |
| ES 连接报证书错误 | 自签 TLS | 确认 `ES_VERIFY_CERTS=false` |
| knn 查询维度报错 | `EMBEDDING_DIMENSIONS` 与索引不符 | 改回 1024 或重建索引 |
| BM25 中文命中差/`_analyze` 报 unknown analyzer | IK 未装（容器被重建） | 按 §8 重装 IK 并重启 |
| rerank 日志"模型不存在" | 模型名错误 | 必须是裸名 `rerank` |
| `节点[RAG重排异常]` 后结果变差 | rerank 降级为 RRF 序 | 查智谱密钥/额度；降级是预期兜底 |

---

## 九、已知问题与演进方向

1. **语料向量与查询模型是否同源存疑**（历史失配，稠密路区分度受损）——当前由 BM25 路 +
   rerank 兜底补足（评测全命中）；根治需用 embedding-3 重建语料向量；
2. rerank 分数饱和，无法做绝对阈值过滤（见 §6）；
3. 入库（chunking / 增量导入）由外部项目负责，本仓库只做查询侧；语料更新后直接写入
   `medical_chunks_v2`（别名 `medical_chunks` 可写，单索引别名对写入透明）；
4. 可选演进：查询改写（query expansion）、语义缓存、多路召回加权（当前 RRF 无权重）、
   ES 服务端 RRF（需付费许可）。
