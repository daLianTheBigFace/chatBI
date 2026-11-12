"""多模态 RAG 子系统。

链路：网页上传 PDF → Kafka → 远端实例 MinerU 解析 → 产物回传本机 → 切块入索引
→ 图文问答（检索文本块 + 同页图片块 → LLM/VLM → 图文混排 answer_blocks）。

分层与文件职责见 ``docs/mmrag-structure.md``。
"""
