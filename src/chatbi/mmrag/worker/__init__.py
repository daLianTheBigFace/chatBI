"""远端实例上的解析 worker：消费 request → 调 MinerU → 回写 result。

部署在 GPU 实例（/workspace/software/mmrag-worker），本机只负责投递与接货。
"""
