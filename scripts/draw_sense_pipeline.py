"""画「情感识别 → 回应生成」两进程流水线的流程图。

没有 graphviz / mmdc，用 Pillow 手绘。输出 docs/images/sense-pipeline-flow.png。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "sense-pipeline-flow.png"

W, H = 1500, 1660
BG = "#FFFFFF"
INK = "#101828"
MUTED = "#667085"
LINE = "#98A2B3"

FRONT = "#2563EB"
PROC1 = "#7C3AED"
PROC2 = "#0D9488"
BUS = "#D97706"
STORE = "#DB3E77"

FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def box(d, x, y, w, h, title, detail, color, tag):
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill="#FCFCFD", outline="#E4E7EC", width=1)
    d.rounded_rectangle([x, y, x + 6, y + h], radius=3, fill=color)
    d.text((x + 22, y + 12), title, font=font(19, True), fill=INK)
    d.text((x + 22, y + 42), detail, font=font(14), fill=MUTED)
    return  # 右侧标签与左侧泳道标签重复，不画



def arrow(d, x1, y1, x2, y2):
    d.line([x1, y1, x2, y2], fill=LINE, width=2)
    if y2 > y1:
        d.polygon([(x2, y2), (x2 - 6, y2 - 9), (x2 + 6, y2 - 9)], fill=LINE)
    else:
        d.polygon([(x2, y2), (x2 - 6, y2 + 9), (x2 + 6, y2 + 9)], fill=LINE)


STEPS = [
    ("接口", FRONT, "01  POST /api/sense", "session_id + text（可选图片引用）；同会话加 Redis 锁 30s", "接口"),
    ("接口", FRONT, "02  建 request_id，写 messages(user)", "同步返回 202 {request_id, events_url}", "接口"),
    ("总线", BUS, "03  XADD stream:sense", "MAXLEN 10000，字段 data=<SenseRequest>", "Redis"),
    ("进程1", PROC1, "04  XREADGROUP stream:sense", "group=sense-worker，BLOCK 5000ms，每次 1 条", "进程1"),
    ("进程1", PROC1, "05  幂等 + 情感识别", "SETNX sense:done:{id}；LLM 结构化输出 label/score/evidence", "进程1"),
    ("总线", BUS, "06  PUBLISH judged + XADD stream:reply", "事件先发（前端先看到判定），结果再入队", "Redis"),
    ("进程2", PROC2, "07  XREADGROUP stream:reply", "group=reply-worker，与进程1 用不同 group", "进程2"),
    ("存储", STORE, "08  读最近 N 轮 messages", "session_id 维度，默认 6 条，超 8000 字截断", "SQLite"),
    ("进程2", PROC2, "09  选策略", "positive→鼓励｜negative→排解｜mixed→排解+鼓励两段｜neutral→接话", "进程2"),
    ("进程2", PROC2, "10  流式生成", "两个 plan 并行发，PUBLISH delta{plan, seq, text}", "进程2"),
    ("存储", STORE, "11  落库 + PUBLISH done", "写 messages(assistant, blocks) → XACK 两条流", "SQLite"),
    ("接口", FRONT, "12  SSE 转发前端", "GET /api/sense/{id}/events 订阅 sense:events:{id}", "接口"),
]

NOTES = [
    ("失败与重投", "处理异常不 XACK；60s 后 XAUTOCLAIM 交给别的消费者；同一 request 重试 3 次进 stream:reply:dead"),
    ("超时预算", "进程1 10s，进程2 20s；接口侧 35s 整体超时后发 error 并断开 SSE"),
    ("幂等键", "sense:done:{request_id}（进程1）、reply:done:{request_id}（进程2），处理过直接 XACK"),
]


def main() -> None:
    image = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(image)

    d.text((40, 34), "情感识别 → 回应生成 · 两进程流水线", font=font(30, True), fill=INK)
    d.text(
        (40, 76),
        "进程1 只做判定，进程2 负责说话；中间只经 Redis，多轮记忆读 SQLite",
        font=font(15),
        fill=MUTED,
    )

    x, w, h = 200, 1060, 74
    y = 130
    gap = 30
    for lane, color, title, detail, tag in STEPS:
        box(d, x, y, w, h, title, detail, color, tag)
        lw = d.textlength(lane, font=font(13, True))
        d.text((x - lw - 18, y + h / 2 - 10), lane, font=font(13, True), fill=color)
        if y > 130:
            arrow(d, x + w / 2, y - gap + 2, x + w / 2, y - 4)
        y += h + gap

    # 侧栏：三条约束
    ny = y - gap + 24
    for title, detail in NOTES:
        d.rounded_rectangle([x, ny, x + w, ny + 58], radius=8, fill="#FFFBEB", outline="#FDE68A")
        d.text((x + 16, ny + 10), title, font=font(14, True), fill="#92400E")
        d.text((x + 16, ny + 32), detail, font=font(13), fill="#78350F")
        ny += 68

    d.text(
        (40, H - 30),
        "图例：左侧色条 = 所属角色；箭头 = 单向流转，没有编排者转发",
        font=font(13),
        fill=MUTED,
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()




