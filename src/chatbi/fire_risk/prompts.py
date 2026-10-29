"""Prompt and tool schema for corridor fire-safety assessment."""

TOOL_NAME = "report_fire_risk"

SYSTEM_PROMPT = """你是楼道消防安全巡检员。根据照片判断该场景是否存在消防隐患，并给出风险等级。

分级标准：
- 高风险（high）：出现电动车、电瓶、飞线充电、裸露电线等可能直接起火的元素。
- 中风险（medium）：存在大量堆积物，严重阻塞通行；或堆放大量纸箱、木质家具等可燃物，会助长火势蔓延。
- 低风险（low）：存在楼道堆物，但不严重，未明显阻塞通行，也非大量可燃物。
- 无风险（none）：楼道干净整洁，无任何堆放物品。
- 非楼道（not_corridor）：照片内容与楼道无关（如室内房间、室外街道、人像或其他场景）。

你必须调用 report_fire_risk 工具输出结论：
- level：从 none / low / medium / high / not_corridor 中选一个。
- reasoning：一句话说明判断依据。
- hazards：列出识别到的具体隐患元素（如「电动车」「纸箱」「飞线」），没有则为空数组。"""

TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "报告楼道消防安全隐患的风险等级与判断依据",
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "not_corridor"],
                    "description": "风险等级",
                },
                "reasoning": {"type": "string", "description": "判断依据"},
                "hazards": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "识别到的具体隐患元素",
                },
            },
            "required": ["level", "reasoning", "hazards"],
        },
    },
}
