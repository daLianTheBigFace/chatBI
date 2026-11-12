# mmrag 文件结构

多模态 RAG 子系统。本机跑接口 / 索引 / 问答，GPU 实例只跑 MinerU 解析。
设计文档见 [multimodal-rag-design.md](multimodal-rag-design.md)，架构图见
[images/mmrag-architecture-v2.png](images/mmrag-architecture-v2.png)。

## 目录

```
src/chatbi/mmrag/
├── __init__.py
├── config.py              配置：路径 / Kafka / Redis / MinerU / Embed / LLM，全部来自 .env
├── models.py              领域模型：DocStatus、Chunk(kind=text|table|image)、ChatMessage(带 blocks)、answer blocks
├── contracts.py           Kafka 三 topic 的消息体 + SSE StatusEvent
├── db.py                  rag.db（SQLite）：documents / chunks / messages 的 DDL 与读写
├── storage.py             磁盘布局、上传落盘、静态 URL、删文档
│
├── api/                   FastAPI 接入层（本机）
│   ├── app.py             应用工厂，挂路由 + /static/docs
│   ├── deps.py            每请求一份 settings + sqlite 连接
│   ├── docs.py            upload / list / status(批量) / detail / reparse / events(SSE)
│   ├── chat.py            图文问答 SSE
│   ├── sessions.py        会话历史（assistant 消息带 blocks，刷新不丢图）
│   ├── sse.py             状态广播（Redis pub/sub）
│   ├── publisher.py       投递 ParseRequest（kafka-python 是同步库，挪到线程池）
│   └── web.py             GET /mmrag —— 直接把 web/index.html 发出去
│
├── web/
│   └── index.html         单文件前端：上传 / 状态徽标 / 图文问答，无需构建
│
├── worker/                解析 worker（部署在 GPU 实例）
│   ├── __main__.py        python -m chatbi.mmrag.worker
│   ├── consumer.py        消费 doc.parse.request，手动 commit
│   ├── mineru.py          调本地 mineru CLI（subprocess）
│   ├── reporter.py        回写 result / DLQ
│   └── pipeline.py        一条任务：幂等 → 解析 → 组装 result
│
├── bridge/                本机侧接货
│   ├── transfer.py        scp 上传原文件 / 拉回产物
│   ├── result_consumer.py 消费 doc.parse.result → 拉产物 → 切块索引 → 改状态
│   └── watchdog.py        定时扫 parsing 超时的文档 → failed（worker 死了没人报）
│
├── ingest/                产物入库（本机）
│   ├── chunker.py         content_list.json → chunks（切块规则在这里）
│   ├── embedder.py        bge-m3 批量向量化
│   └── indexer.py         写 chunks + 落 vectors.npy
│
└── rag/                   问答（本机）
    ├── retriever.py       Top-K 文本块 + 同页图片块
    ├── prompt.py          system / context / user 组装
    ├── llm.py             流式调用（是否支持视觉看配置）
    └── answer.py          模型输出 → answer_blocks

tests/test_mmrag_units.py     切块 / 库表 / 看门狗 / 路径拼装的单元测试
tests/test_mmrag_api.py       8 条路由 + 页面 + SSE 帧格式（假 Kafka）
scripts/mmrag_live_e2e.py     服务端真链路（投 request → 等 done）
scripts/mmrag_http_e2e.py     HTTP 真链路（上传 → 状态 → 问答流）
```

## 数据流

```
浏览器 ──POST upload──▶ api/docs ──save_upload──▶ data/mmrag/docs/{id}/raw.pdf
                            │
                            ├──bridge.transfer.push_pdf──▶ 实例 in/{id}.pdf
                            └──Kafka doc.parse.request (key=doc_id)
                                        │
                              实例 worker: mineru parse --tier standard --pages all
                                        │
                              Kafka doc.parse.result ──▶ 本机 bridge.result_consumer
                                        │
                        scp 拉回 out/{id}/ ──▶ data/mmrag/docs/{id}/parsed/
                                        │
                        ingest.chunker → chunks → vectors.npy + rag.db
                                        │
                              状态 done ──SSE──▶ 前端徽标变绿
```

## 为什么这样分

- **一份文档两种块**：text/table 进向量，image 不进向量、靠同页文本召回。
  分成两份文档会让 `doc_id` 对不上、页码断链、重解析删两份。
- **产物拉回本机**：实例的 `/workspace` 是容器 overlay，实例一停就没了。
  只用本机出站 ssh，不需要实例反连本机。
- **Kafka 不搬文件**：单条消息默认上限 1MB，result 里只放路径、页数、耗时。
- **用户提问的图不入库**：它没有页码、没有版本，只属于那一次会话。

## 现在的状态

整条链路已经跑通并实测过（本机 → 实例 → 本机 → 浏览器）：

```
上传 → scp 推实例 → Kafka request → 实例 worker 消费 → mineru parse
     → Kafka result → 本机 bridge 拉产物 → 切块 → bge-m3 向量 → rag.db
     → SSE 推 done → 前端徽标变绿 → 提问 → 检索 → 模型流式回答（图文混排）
```

已验证的细节：

- `scripts/mmrag_live_e2e.py`：只测服务端链路，`块数=8`、`向量已写入=8`。
- `scripts/mmrag_http_e2e.py`：走 HTTP，上传 → SSE 状态（pending/parsing/done）→ 问答流（`start`/`meta`/`delta`/`done`）→ 回答带 `[来源 n · 第 m 页]` 与表格内容。
- `tests/`：`291 passed`（含切块、库表、看门狗、API 路由、页面）。

## 还没做的

1. **向量库还是 numpy 全量矩阵**（`vectors.npy`）。文档量到几千块以后每次检索都要扫全表，得换 FAISS / hnswlib。
2. **图片不做向量**。现在靠「命中同页文本 → 带出该页图」，图多、文少的 PPT 类文档会退化。
3. **单个 SQLite 连接**。并发上传时写锁会排队，量大了换 Postgres。
4. **没有分页**，文档列表硬上限 200 条。
5. **重排（rerank）没做**。现在的顺序是纯向量分 + 同页表格补位，问题复杂时召回精度不够。

