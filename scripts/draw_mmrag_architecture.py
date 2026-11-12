"""Render the multimodal-RAG backend architecture diagrams to PNG.

Outputs:
  docs/images/mmrag-architecture.png   main layered architecture
  docs/images/mmrag-doc-state.png      document parse state machine
"""
import os
import re

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs", "images")
os.makedirs(OUT_DIR, exist_ok=True)

FR = "C:/Windows/Fonts/msyh.ttc"
FB = "C:/Windows/Fonts/msyhbd.ttc"

WHITE = (255, 255, 255)
BG = (250, 251, 253)
INK = (15, 23, 42)
SUB = (71, 85, 105)
MUTED = (100, 116, 139)
LINE = (148, 163, 184)

C_FE = (37, 99, 235)
C_API = (16, 185, 129)
C_KAFKA = (217, 119, 6)
C_WORK = (124, 58, 237)
C_STORE = (13, 148, 136)
C_RAG = (219, 39, 119)


def font(size, bold=False):
    return ImageFont.truetype(FB if bold else FR, size)


def mix(c, bg, t):
    return tuple(int(c[i] * (1 - t) + bg[i] * t) for i in range(3))


def wrap(text, fnt, maxw):
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9&+\-_/\.]*|\s+|.", text)
    lines, cur = [], ""
    for tk in tokens:
        if tk.isspace():
            cand = cur + " "
            if fnt.getlength(cand.rstrip()) <= maxw:
                cur = cand
            continue
        cand = cur + tk
        if fnt.getlength(cand) <= maxw or not cur.strip():
            cur = cand
        else:
            lines.append(cur.rstrip())
            cur = tk
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def dashed_line(d, p1, p2, color, width=2, dash=9, gap=7):
    x1, y1 = p1
    x2, y2 = p2
    dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if dist == 0:
        return
    ux, uy = (x2 - x1) / dist, (y2 - y1) / dist
    pos = 0.0
    while pos < dist:
        end = min(pos + dash, dist)
        d.line([(x1 + ux * pos, y1 + uy * pos), (x1 + ux * end, y1 + uy * end)],
               fill=color, width=width)
        pos += dash + gap


def arrow_head(d, p_prev, p_end, color, size=9):
    x1, y1 = p_prev
    x2, y2 = p_end
    dx, dy = x2 - x1, y2 - y1
    ln = (dx * dx + dy * dy) ** 0.5 or 1
    ux, uy = dx / ln, dy / ln
    px, py = -uy, ux
    base = (x2 - ux * size * 1.6, y2 - uy * size * 1.6)
    d.polygon([(x2, y2),
               (base[0] + px * size * 0.75, base[1] + py * size * 0.75),
               (base[0] - px * size * 0.75, base[1] - py * size * 0.75)],
              fill=color)


def polyline(d, pts, color=LINE, width=2, dashed=False, head=True):
    for i in range(len(pts) - 1):
        if dashed:
            dashed_line(d, pts[i], pts[i + 1], color, width)
        else:
            d.line([pts[i], pts[i + 1]], fill=color, width=width, joint="curve")
    if head:
        arrow_head(d, pts[-2], pts[-1], color)


def draw_box(d, x, y, w, h, title, body, color, ts=15, bs=12, tcol=INK, bcol=SUB):
    d.rounded_rectangle([x, y, x + w, y + h], radius=13,
                        fill=mix(color, WHITE, 0.94), outline=mix(color, WHITE, 0.45), width=2)
    d.rounded_rectangle([x + 16, y + 12, x + 16 + 30, y + 16], radius=2, fill=color)

    ft = font(ts, True)
    fb = font(bs, False)
    tl = wrap(title, ft, w - 32)
    bl = wrap(body, fb, w - 30) if body else []
    tlh, blh = ts + 7, bs + 6
    total = len(tl) * tlh + (5 + len(bl) * blh if bl else 0)
    cy = y + (h - total) / 2
    for ln in tl:
        d.text((x + 16, cy), ln, font=ft, fill=tcol)
        cy += tlh
    if bl:
        cy += 3
        for ln in bl:
            d.text((x + 15, cy), ln, font=fb, fill=bcol)
            cy += blh


def draw_lane(d, x, y, w, h, label_lines):
    d.rounded_rectangle([x, y, x + w, y + h], radius=16,
                        fill=(246, 248, 251), outline=(228, 233, 240), width=2)
    d.rounded_rectangle([x, y, x + 134, y + h], radius=16,
                        fill=(238, 242, 247), outline=(228, 233, 240), width=2)
    d.rectangle([x + 122, y + 2, x + 134, y + h - 2], fill=(238, 242, 247))
    total = len(label_lines) * 24
    cy = y + (h - total) / 2 + 2
    for i, ln in enumerate(label_lines):
        fnt = font(15, True) if i == 0 else font(12, False)
        tw = fnt.getlength(ln)
        d.text((x + 67 - tw / 2, cy), ln, font=fnt, fill=INK if i == 0 else MUTED)
        cy += 24 if i == 0 else 18


# ---------------------------------------------------------------- main figure
W, H = 1860, 1450
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

LX, LW = 170, 1560
CX0 = 344
CXE = 1680
CW = CXE - CX0

d.text((40, 26), "多模态 RAG · 后端架构", font=font(34, True), fill=INK)
d.text((42, 78), "前端上传 PDF → Kafka → MinerU 解析 → 多模态索引 → 图 / 文混合问答",
       font=font(16), fill=MUTED)

lanes = [
    (["前端", "Web"], 132, 150),
    (["接入层", "FastAPI"], 312, 150),
    (["消息层", "Kafka"], 492, 140),
    (["解析", "MinerU"], 662, 150),
    (["存储", "Storage"], 842, 150),
    (["问答", "RAG"], 1028, 214),
]
for lab, y, h in lanes:
    draw_lane(d, LX, y, LW, h, lab)


def row(count, gap, y, h, x0=CX0, w_avail=CW):
    w = int((w_avail - gap * (count - 1)) / count)
    return [(x0 + i * (w + gap), y, w, h) for i in range(count)]


def c(bx):
    x, y, w, h = bx
    return (x + w / 2, y + h / 2)


# L1 frontend
L1 = row(4, 32, 165, 84)
draw_box(d, *L1[0], "多文件上传", "PDF 批量 · 拖拽 · 分片直传", C_FE)
draw_box(d, *L1[1], "文档列表", "状态徽标：待解析 / 解析中 / 解析完成", C_FE)
draw_box(d, *L1[2], "对话区", "图文混排渲染 · 图片卡片 · 引用来源", C_FE)
draw_box(d, *L1[3], "图问输入", "图片 + 文字 一起提交（multipart）", C_FE)

# L2 api
L2 = row(3, 36, 345, 86)
draw_box(d, *L2[0], "POST /api/docs/upload", "落对象存储 → 建文档(pending) → 投 Kafka", C_API)
draw_box(d, *L2[1], "POST /api/chat/stream", "SSE 流式：纯文本 ｜ 图片 + 文本", C_API)
draw_box(d, *L2[2], "GET /api/docs/{id} · SSE /events", "状态推进：pending→parsing→done", C_API)

# L3 kafka
KW = int((CW - 18) / 2)
k1 = (CX0, 523, KW, 78)
k2 = (CX0 + KW + 18, 523, KW, 78)
draw_box(d, *k1, "topic: doc.parse.request", "key = doc_id，同一文档分区内有序", C_KAFKA)
draw_box(d, *k2, "doc.parse.result · doc.parse.dlq", "回写状态 + 失败隔离重试", C_KAFKA)

# L4 worker
L4 = row(5, 26, 695, 86)
draw_box(d, *L4[0], "① 消费 + 幂等", "Redis SETNX doc_id", C_WORK, ts=14)
draw_box(d, *L4[1], "② MinerU 解析", "版面 / 表格 / 公式 / OCR", C_WORK, ts=14)
draw_box(d, *L4[2], "③ 切图切表", "图片 → MinIO，保留页码 bbox", C_WORK, ts=14)
draw_box(d, *L4[3], "④ Chunk + Embedding", "层级切分 + 文本 / 图像向量", C_WORK, ts=14)
draw_box(d, *L4[4], "⑤ 落库", "Milvus + Postgres(chunks/images)", C_WORK, ts=14)

# L5 storage
L5 = row(3, 36, 877, 80)
draw_box(d, *L5[0], "MinIO · 对象存储", "原始 PDF · 图片切片（doc/page/bbox）", C_STORE)
draw_box(d, *L5[1], "Postgres · 元数据", "documents / chunks / images / jobs", C_STORE)
draw_box(d, *L5[2], "Milvus + Redis", "向量索引(text|image) · 任务态 / 幂等", C_STORE)

# L6 rag
L6 = row(6, 22, 1082, 92)
rag_txt = [
    ("意图路由", "text ｜ image+text"),
    ("Query 改写", "多轮消歧 · 补全"),
    ("混合检索", "向量 + BM25 → RRF"),
    ("Rerank", "Top-K 精排 · 过滤"),
    ("Prompt 拼装", "文本块 + 图片(URL)"),
    ("VLM 生成", "图文混排 answer_blocks"),
]
for (t, b), bx in zip(rag_txt, L6):
    draw_box(d, *bx, t, b, C_RAG, ts=14, bs=11)

# ---------------------------------------------------------------- arrows
# A1 -> B1
polyline(d, [(c(L1[0])[0], 249), (c(L1[0])[0], 297), (c(L2[0])[0], 297), (c(L2[0])[0], 345)])
# A4 -> B2
polyline(d, [(c(L1[3])[0], 249), (c(L1[3])[0], 297), (c(L2[1])[0], 297), (c(L2[1])[0], 345)])
# A2 -> B3 (status polling, SSE)
polyline(d, [(c(L1[1])[0], 249), (c(L1[1])[0], 305), (c(L2[2])[0], 305), (c(L2[2])[0], 345)],
         dashed=True)

# B1 -> C1
polyline(d, [(c(L2[0])[0], 431), (c(L2[0])[0], 523)], color=C_KAFKA)
# C1 -> C2
polyline(d, [(k1[0] + k1[2], c(k1)[1]), (k2[0], c(k2)[1])], color=C_KAFKA)
# C1 -> D1
polyline(d, [(c(k1)[0], 601), (c(k1)[0], 647), (c(L4[0])[0], 647), (c(L4[0])[0], 695)],
         color=C_KAFKA)
# D1..D5
for i in range(4):
    polyline(d, [(L4[i][0] + L4[i][2], c(L4[i])[1]), (L4[i + 1][0], c(L4[i + 1])[1])])
# D3 -> E1 (image assets)
polyline(d, [(c(L4[2])[0], 781), (c(L4[2])[0], 827), (c(L5[0])[0], 827), (c(L5[0])[0], 877)],
         color=C_STORE)
# D5 -> E3 (vector index)
polyline(d, [(c(L4[4])[0], 781), (c(L4[4])[0], 828), (c(L5[2])[0], 828), (c(L5[2])[0], 877)],
         color=C_STORE)
# D5 -> E2 (chunk / image metadata)
polyline(d, [(c(L4[4])[0], 781), (c(L4[4])[0], 837), (c(L5[1])[0], 837), (c(L5[1])[0], 877)],
         color=C_STORE, dashed=True)
# D5 -> C2 (result event, right channel x=1745)
polyline(d, [(L4[4][0] + L4[4][2], c(L4[4])[1]), (1745, c(L4[4])[1]),
             (1745, c(k2)[1]), (k2[0] + k2[2], c(k2)[1])], color=C_KAFKA, dashed=True)

# E3 -> F3 (vector retrieval)
polyline(d, [(c(L5[2])[0], 957), (c(L5[2])[0], 996),
             (c(L6[2])[0] - 34, 996), (c(L6[2])[0] - 34, 1082)], color=C_STORE)
# E2 -> F3 (metadata filter)
polyline(d, [(c(L5[1])[0], 957), (c(L5[1])[0], 1008),
             (c(L6[2])[0] + 34, 1008), (c(L6[2])[0] + 34, 1082)], color=C_STORE, dashed=True)

# B2 -> F1 (chat request, down into gap, then right channel x=1780)
polyline(d, [(c(L2[1])[0], 431), (c(L2[1])[0], 477), (1780, 477),
             (1780, 1020), (c(L6[0])[0], 1020), (c(L6[0])[0], 1082)])

# F chain
for i in range(5):
    polyline(d, [(L6[i][0] + L6[i][2], c(L6[i])[1]), (L6[i + 1][0], c(L6[i + 1])[1])])

# F6 -> A3 (answer stream back to UI, right channel x=1812)
polyline(d, [(L6[5][0] + L6[5][2], c(L6[5])[1]), (1812, c(L6[5])[1]),
             (1812, 112), (c(L1[2])[0], 112), (c(L1[2])[0], 165)],
         color=C_RAG, dashed=True)

# ---------------------------------------------------------------- legend
ly = 1290
d.text((40, ly), "图例", font=font(14, True), fill=INK)
legend = [
    (LINE, False, "请求 / 控制流"),
    (C_KAFKA, False, "Kafka 事件流"),
    (C_STORE, True, "数据 / 状态读写"),
    (C_RAG, True, "结果回流（SSE 图文混排）"),
]
lx = 120
for col, dash, label in legend:
    if dash:
        dashed_line(d, (lx, ly + 10), (lx + 46, ly + 10), col)
    else:
        d.line([(lx, ly + 10), (lx + 46, ly + 10)], fill=col, width=2)
    arrow_head(d, (lx + 38, ly + 10), (lx + 46, ly + 10), col, size=7)
    d.text((lx + 54, ly), label, font=font(13), fill=SUB)
    lx += 54 + font(13).getlength(label) + 46

d.text((40, ly + 44),
       "文档状态：pending 待解析 → parsing 解析中 → done 解析完成；失败 failed（DLQ 兜底，可重投）",
       font=font(13), fill=SUB)

img.save(os.path.join(OUT_DIR, "mmrag-architecture.png"))
print("saved architecture")

# ------------------------------------------------------------ state machine
W2, H2 = 1180, 410
img2 = Image.new("RGB", (W2, H2), BG)
d2 = ImageDraw.Draw(img2)
d2.text((36, 20), "文档解析状态机（documents.status）", font=font(22, True), fill=INK)

sm_y, sm_w, sm_h = 150, 210, 76
xs = [44, 350, 656]
cy = sm_y + sm_h / 2
draw_box(d2, xs[0], sm_y, sm_w, sm_h, "pending", "待解析 · 已入库", C_API, ts=16, bs=12)
draw_box(d2, xs[1], sm_y, sm_w, sm_h, "parsing", "解析中 · worker 占用", C_KAFKA, ts=16, bs=12)
draw_box(d2, xs[2], sm_y, sm_w, sm_h, "done", "解析完成 · 可检索", C_STORE, ts=16, bs=12)
fy = 296
draw_box(d2, xs[1], fy, sm_w, 68, "failed", "失败 · 保留错误信息，可重投", C_RAG, ts=16, bs=12)

polyline(d2, [(xs[0] + sm_w, cy), (xs[1], cy)])
polyline(d2, [(xs[1] + sm_w, cy), (xs[2], cy)])
d2.text((xs[0] + sm_w + 16, cy - 24), "投 Kafka", font=font(11), fill=MUTED)
d2.text((xs[1] + sm_w + 8, cy - 24), "向量化完成", font=font(11), fill=MUTED)

# parsing -> failed
polyline(d2, [(xs[1] + sm_w / 2, sm_y + sm_h), (xs[1] + sm_w / 2, fy)],
         color=C_RAG, dashed=True)
d2.text((xs[1] + sm_w / 2 + 12, sm_y + sm_h + 12), "异常 / 超时 / 重试耗尽",
        font=font(12), fill=C_RAG)
# failed -> pending
polyline(d2, [(xs[1], fy + 34), (xs[0] + sm_w / 2, fy + 34),
              (xs[0] + sm_w / 2, sm_y + sm_h)], color=C_RAG, dashed=True)
d2.text((xs[0] + sm_w / 2 + 12, fy + 12), "手动重投", font=font(12), fill=C_RAG)
# done -> pending (re-parse, version + 1)
polyline(d2, [(xs[2] + sm_w / 2, sm_y), (xs[2] + sm_w / 2, 104),
              (xs[0] + sm_w / 2, 104), (xs[0] + sm_w / 2, sm_y)], dashed=True)
d2.text((xs[0] + sm_w / 2 + 12, 80), "重新解析（version + 1，旧版本软删）",
        font=font(12), fill=MUTED)

img2.save(os.path.join(OUT_DIR, "mmrag-doc-state.png"))
print("saved state machine")


