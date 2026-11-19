"""情感识别 → 回应生成，两段流水线。

- `contracts`  两边共用的模型（谁也别各写一套）
- `prompts`    三套提示词（判定 / 鼓励 / 排解）
- `planning`   判定 → plan 的纯函数规则
- `history`    SQLite 多轮记忆
- `runtimes`   OpenAI Agents SDK 版与 LangGraph 版，同一组 Protocol
"""

from chatbi.sense.config import SenseSettings, load_settings

__all__ = ["SenseSettings", "load_settings"]
