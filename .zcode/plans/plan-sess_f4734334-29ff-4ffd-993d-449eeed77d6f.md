# RAG 改造：pgvector → 本地 Elasticsearch 9.5.4 + 混合检索（稠密 + BM25 倒排 + RRF 融合 + Rerank）

## 检索链路（目标）

```
query → 智谱 embedding-3 向量化(1024维,不变)
      → Elasticsearch 混合召回: knn稠密 + BM25(IK中文分词)稀疏, RRF融合取 top10
      → 智谱 rerank-2 重排 → 取 top3
      → Agent 工具 medical_rag_search 返回(接口不变, agent.py 零改动)
```

## 现有环境（已确认，不重复部署）

- **med-es 容器已在运行**：`docker.elastic.co/elasticsearch/elasticsearch:9.5.4`，单节点、1G 堆、端口 9200，挂 `med-es-data`/`med-es-config` 命名卷；med-kibana 同版本在跑。**不改 docker-compose.yml，不建 ES Dockerfile**
- **ES basic auth**：`elastic` / 用户提供（已给），写入 `backend/.env` 的 `ES_PASSWORD`；`.env.example` 只留空占位符；curl 验证与 Python 客户端均带认证
- 迁移数据源 med-pgvector（5433）在运行，业务库 postgres（5432）不动
- rerank-2 复用现有 `zhipu_api_key` + `embedding_base_url`（`POST /api/paas/v4/rerank`，body `{model, query, documents, top_n}`，返回 `results[].{index, relevance_score}`），无需新增密钥

## 改动清单

### 0. 实施首步：环境验证
- 带认证 `curl http://localhost:9200/_cat/plugins` 确认 IK 插件；若缺失：`docker exec med-es bin/elasticsearch-plugin install https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-9.5.4.zip` 后重启容器（命令写入 README 便于容器重建后复现；plugins 目录不在卷上，重建会丢）

### 1. backend/app/config.py + .env.example + .env
- 新增：`es_url=http://localhost:9200`、`es_username=elastic`、`es_password`（.env 写入实际密码）、`es_index=medrag`、`rag_candidate_k=10`（RRF 融合候选数）、`rerank_model=rerank-2`
- 语义调整：`rag_top_k` 默认改 3（rerank 后最终条数）；`rag_min_score` 改为作用于 rerank 分数
- `medrag_db_url` 保留，仅供迁移脚本读旧库

### 2. backend/app/rag.py 重写（核心）
- `_get_es_client()`：`AsyncElasticsearch(basic_auth=(es_username, es_password))` 懒加载单例（沿用现有双检锁模式）
- `embed_query()`：不变（智谱 embedding-3）
- `_hybrid_recall()`：ES retriever API——`rrf` retriever 组合 `knn`（field=embedding, k=10, num_candidates=100）与 `standard`（match chunk_text），`rank_window_size=10`，取 10 条候选
- `_rerank()`：POST `{embedding_base_url}/rerank`（rerank-2），按 `relevance_score` 排序取 top_n；调用失败时降级用 RRF 序并打告警日志
- `rag_search(query, top_k=None)`：组合上述，默认 top_k=3；保留现有结构化日志风格（补充 RRF 名次、rerank 分数）
- `medical_rag_search(query, top_k=3)`：默认值 4→3，docstring 微调；工具签名不变（agent.py 零改动）
- requirements.txt：新增 `elasticsearch[async]`（9.x 客户端）；asyncpg 保留（业务库仍用）

### 3. 语料迁移脚本 backend/scripts/migrate_pg_to_es.py
- 自动建 ES 索引：`embedding` 为 dense_vector(dims=1024, cosine, HNSW)；`chunk_text`/`title` 用 ik_max_word 索引/ik_smart 检索；`source_url` 为 keyword
- 从 med-pgvector 全量读 `chunks JOIN documents`（`embedding::text` 解析回浮点数组），`async_bulk` 灌入 ES——**复用已存向量，零 embedding 成本**
- 带与 rag.py 相同的 ES basic auth；结束校验两侧 count 一致并打印报告

### 4. 评测与文档
- `scripts/eval_rag.py` 无需改（复用 `rag_search`，TOP_K=4 即 rerank 后取 4 条）
- README.md：RAG 部署节改为 ES 说明（外部容器 + basic auth + IK 安装命令 + 迁移命令）、env 表更新
- ARCHITECTURE.md §5：重写为混合检索描述，顺带修正过期的 512 维文档

## 验证步骤
1. 带认证的 IK 分词验证（`_analyze` API 测中文切词）
2. 跑迁移脚本灌语料，校验 ES 与 pgvector 两侧 count 一致
3. 跑 `eval_rag.py` 8 条 golden 用例，输出 Hit@4/MRR/平均分，对比旧 pgvector 基线
4. 手动调 `rag_search` 验证 top10 → rerank → top3 全链路与结构化日志

## 不改的部分
- docker-compose.yml（ES 为外部已起容器）、agent.py 工具注册、prompts.py 引用规范、websearch.py、长记忆与工作区功能
- med-pgvector（5433）保留原样作为迁移源，不再被运行时使用