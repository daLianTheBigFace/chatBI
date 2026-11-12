"""Render the simplified multimodal-RAG architecture (v2).

Outputs:
  docs/images/mmrag-architecture-v2.png   main architecture
  docs/images/mmrag-doc-state.png         state machine (re-rendered)
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
REMOTE = (234, 88, 12)


def font(size, bold=False):
    return ImageFont.truetype(FB if bold else FR, size)


def mix(c, bg, t):
    return tuple(int(c[i] * (1 - t) + bg[i] * t) for i in range(3))


def wrap(text, fnt, maxw):
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9&+\-_/\.\{\}:]*|\s+|.", text)
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


def draw_lane(d, x, y, w, h, label_lines, accent=None):
    d.rounded_rectangle([x, y, x + w, y + h], radius=16,
                        fill=(246, 248, 251), outline=(228, 233, 240), width=2)
    d.rounded_rectangle([x, y, x + 140, y + h], radius=16,
                        fill=(238, 242, 247), outline=(228, 233, 240), width=2)
    d.rectangle([x + 126, y + 2, x + 140, y + h - 2], fill=(238, 242, 247))
    sizes = [(15, True, INK if not accent else accent), (12, False, MUTED), (11, False, MUTED)]
    total = 0
    for i, ln in enumerate(label_lines):
        s, b, _ = sizes[i]
        total += s + (10 if i == 0 else 6)
    cy = y + (h - total) / 2 + 2
    for i, ln in enumerate(label_lines):
        s, b, col = sizes[i]
        fnt = font(s, b)
        tw = fnt.getlength(ln)
        d.text((x + 70 - tw / 2, cy), ln, font=fnt, fill=col)
        cy += s + (10 if i == 0 else 6)


# ---------------------------------------------------------------- main figure
W, H = 1900, 1400
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

LX, LW = 110, 1660
CX0 = 272
CXE = 1745
CW = CXE - CX0

d.text((40, 24), "多模态 RAG · 系统架构（简化版）", font=font(32, True), fill=INK)
d.text((42, 74), "本机 = 前端 + 接口 + 索引 + 问答；实例 = 只做 GPU 解析。经典 编码 → 存储 → 检索 → 生成",
       font=font(16), fill=MUTED)

lanes = [
    (["前端", "Web · 本机"], 150, 132, None),
    (["接口", "FastAPI · 本机"], 312, 132, None),
    (["消息", "Kafka · 实例", "本机经隧道投递"], 474, 112, None),
    (["解析", "实例 · GPU", "远程容器（见 .env）"], 616, 150, REMOTE),
    (["存储", "本机磁盘"], 796, 132, None),
    (["索引", "离线 · 本机"], 958, 112, None),
    (["问答", "在线 · 本机"], 1100, 150, None),
]
for lab, y, h, acc in lanes:
    draw_lane(d, LX, y, LW, h, lab, acc)

# remote lane emphasis
d.rounded_rectangle([LX + 4, 616 + 4, LX + LW - 4, 616 + 146], radius=14,
                    outline=mix(REMOTE, WHITE, 0.35), width=2, )
for i in range(0, 0):
    pass


def row(count, gap, y, h, x0=CX0, w_avail=CW):
    w = int((w_avail - gap * (count - 1)) / count)
    return [(x0 + i * (w + gap), y, w, h) for i in range(count)]


def c(bx):
    x, y, w, h = bx
    return (x + w / 2, y + h / 2)


L1 = row(4, 26, 166, 100)
draw_box(d, *L1[0], "多文件上传", "PDF 多选 / 拖拽 · 逐个提交 · 单文件进度", C_FE, ts=14, bs=11)
draw_box(d, *L1[1], "文档列表", "待解析 / 解析中 / 完成 · 失败可重试", C_FE, ts=14, bs=11)
draw_box(d, *L1[2], "对话区", "图文混排渲染 · 图片卡片 · 标注引用页码", C_FE, ts=14, bs=11)
draw_box(d, *L1[3], "带图提问", "一条消息 = 文字 + 图（multipart）", C_FE, ts=14, bs=11)

L2 = row(3, 30, 328, 100)
draw_box(d, *L2[0], "POST /api/mmrag/docs/upload", "存原 PDF → documents 插行 pending → 投 Kafka", C_API, ts=14, bs=11)
draw_box(d, *L2[1], "GET /api/mmrag/docs · /events", "列表 · 批量状态 · 全局 SSE：pending→parsing→done", C_API, ts=14, bs=11)
draw_box(d, *L2[2], "POST /api/mmrag/chat/stream", "multipart(question, image?) → 检索 → 模型 → SSE", C_API, ts=14, bs=11)

L3 = row(2, 20, 490, 82)
draw_box(d, *L3[0], "topic: doc.parse.request", "key = doc_id｜3 分区｜同文档分区内有序｜手动提交 offset", C_KAFKA, ts=14, bs=11)
draw_box(d, *L3[1], "doc.parse.result · doc.parse.dlq", "回传 doc_id / 产物路径 / 页数 / 耗时｜失败进 DLQ 可重投", C_KAFKA, ts=14, bs=11)

L4 = row(4, 24, 640, 104)
draw_box(d, *L4[0], "① 消费 · 幂等", "手动 commit｜SETNX parse:lock:{doc_id}:{version}", C_WORK, ts=14, bs=11)
draw_box(d, *L4[1], "② 调 MinerU", "parse -p all --tier standard｜出 md + content_list + images", C_WORK, ts=14, bs=11)
draw_box(d, *L4[2], "③ 产物就绪", "留在实例 parsed/｜本机 bridge 随后 scp 拉回", C_WORK, ts=14, bs=11)
draw_box(d, *L4[3], "④ 回写结果", "发 result：doc_id / 路径 / 页数 / 耗时", C_WORK, ts=14, bs=11)

L5 = row(3, 30, 812, 100)
draw_box(d, *L5[0], "原始文件 raw.pdf", "data/mmrag/docs/{doc_id}/raw.pdf｜按 doc_id 分目录", C_STORE, ts=14, bs=11)
draw_box(d, *L5[1], "解析产物 parsed/", "*.md 正文 · content_list.json(bbox) · images/*.png", C_STORE, ts=14, bs=11)
draw_box(d, *L5[2], "SQLite rag.db", "documents / chunks / messages｜状态 · 块 · 会话", C_STORE, ts=14, bs=11)

L6 = row(3, 30, 974, 82)
draw_box(d, *L6[0], "① 切块", "按 page_idx + type 切｜文本 500–800 字，表格整块，图单独成块", C_RAG, ts=14, bs=11)
draw_box(d, *L6[1], "② Embedding", "bge-m3（本机 3080Ti）｜文本块一向量，图片配同页文字", C_RAG, ts=14, bs=11)
draw_box(d, *L6[2], "③ 写向量库", "vectors.npy + chunks 回填｜按 chunk_id 对齐向量行", C_RAG, ts=14, bs=11)

L7 = row(5, 20, 1116, 118)
draw_box(d, *L7[0], "① 问题向量化", "文本 → bge-m3；带图时先 caption 或直接给 VLM", C_API, ts=13, bs=10)
draw_box(d, *L7[1], "② Top-K 检索", "向量 top-8｜同页图片一起召回", C_API, ts=13, bs=10)
draw_box(d, *L7[2], "③ Prompt 拼装", "系统提示 + 命中文块 + 图片路径", C_API, ts=13, bs=10)
draw_box(d, *L7[3], "④ LLM 生成", "有视觉能力就传图，否则用 caption 文本", C_API, ts=13, bs=10)
draw_box(d, *L7[4], "⑤ answer_blocks", "[{type:text},{type:image}] + citations", C_API, ts=13, bs=10)

# ---------------------------------------------------------------- arrows
# 上传 -> upload
polyline(d, [(446, 266), (446, 294), (507, 294), (507, 328)])
# 带图提问 -> chat
polyline(d, [(1568, 266), (1568, 300), (1509, 300), (1509, 328)])
# 状态 SSE：events -> 文档列表（向上）
polyline(d, [(1008, 328), (1008, 306), (820, 306), (820, 266)],
         color=C_API, dashed=True, width=2)
# upload -> kafka
polyline(d, [(507, 428), (507, 490)], color=C_KAFKA)
# kafka -> worker
polyline(d, [(635, 572), (635, 601), (447, 601), (447, 640)], color=C_KAFKA)
# worker chain
for i in range(3):
    polyline(d, [(L4[i][0] + L4[i][2], c(L4[i])[1]), (L4[i + 1][0], c(L4[i + 1])[1])],
             color=C_WORK)
# worker -> parsed
polyline(d, [(1195, 744), (1195, 782), (1008, 782), (1008, 812)], color=C_STORE)
# worker -> result topic
polyline(d, [(1744, 692), (1800, 692), (1800, 605), (1382, 605), (1382, 572)],
         color=C_KAFKA, dashed=True)
# result topic -> events api
polyline(d, [(1382, 490), (1382, 462), (1008, 462), (1008, 428)],
         color=C_KAFKA, dashed=True)
# parsed -> chunking
polyline(d, [(1008, 912), (1008, 944), (507, 944), (507, 974)], color=C_STORE)
# index chain
for i in range(2):
    polyline(d, [(L6[i][0] + L6[i][2], c(L6[i])[1]), (L6[i + 1][0], c(L6[i + 1])[1])],
             color=C_RAG, )
# index -> sqlite (write back)
polyline(d, [(1509, 974), (1509, 912)], color=C_STORE, dashed=True)
# index -> retrieval
polyline(d, [(1509, 1056), (1509, 1080), (709, 1080), (709, 1116)], color=C_STORE)
# chat request door -> 问答 lane
polyline(d, [(1509, 428), (1509, 446), (1836, 446), (1836, 1092), (411, 1092), (411, 1116)])
# answer back to UI
polyline(d, [(1742, 1175), (1878, 1175), (1878, 120), (1194, 120), (1194, 150)],
         color=C_RAG, dashed=True)

# ---------------------------------------------------------------- arrow labels
def lab(x, y, t, col=MUTED):
    d.text((x, y), t, font=font(11), fill=col)


lab(519, 448, "投递")
lab(648, 588, "消费")
lab(1108, 760, "bridge 拉回")
lab(1224, 446, "状态推进")
lab(700, 924, "读产物")
lab(1020, 1062, "读向量")

# ---------------------------------------------------------------- legend
ly = 1290
d.text((40, ly), "图例", font=font(14, True), fill=INK)
legend = [
    (LINE, False, "请求 / 调用"),
    (C_KAFKA, False, "Kafka 事件流"),
    (C_STORE, False, "数据 / 状态读写"),
    (C_API, True, "SSE 状态推送"),
    (C_RAG, True, "答案回流（SSE 图文混排）"),
]
lx = 110
for col, dash, label in legend:
    if dash:
        dashed_line(d, (lx, ly + 10), (lx + 44, ly + 10), col)
    else:
        d.line([(lx, ly + 10), (lx + 44, ly + 10)], fill=col, width=2)
    arrow_head(d, (lx + 36, ly + 10), (lx + 44, ly + 10), col, size=7)
    d.text((lx + 52, ly), label, font=font(13), fill=SUB)
    lx += 52 + font(13).getlength(label) + 42

d.text((40, ly + 46),
       "文档状态：pending 待解析 → parsing 解析中 → done 解析完成；failed 失败（DLQ 兜底，可重投）；看门狗每 5s 扫一次，parsing 超 30 分钟自动判 failed",
       font=font(13), fill=SUB)
d.text((40, ly + 74),
       "实例只提供 MinerU（GPU 解析）；本机经 SSH 调用并把产物 rsync 回来，因此实例停机后已解析内容不受影响",
       font=font(13), fill=SUB)

img.save(os.path.join(OUT_DIR, "mmrag-architecture-v2.png"))
print("saved architecture v2")

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

polyline(d2, [(xs[1] + sm_w / 2, sm_y + sm_h), (xs[1] + sm_w / 2, fy)],
         color=C_RAG, dashed=True)
d2.text((xs[1] + sm_w / 2 + 12, sm_y + sm_h + 12), "异常 / 超时 / 重试耗尽",
        font=font(12), fill=C_RAG)
polyline(d2, [(xs[1], fy + 34), (xs[0] + sm_w / 2, fy + 34),
              (xs[0] + sm_w / 2, sm_y + sm_h)], color=C_RAG, dashed=True)
d2.text((xs[0] + sm_w / 2 + 12, fy + 12), "手动重投", font=font(12), fill=C_RAG)
polyline(d2, [(xs[2] + sm_w / 2, sm_y), (xs[2] + sm_w / 2, 104),
              (xs[0] + sm_w / 2, 104), (xs[0] + sm_w / 2, sm_y)], dashed=True)
d2.text((xs[0] + sm_w / 2 + 12, 80), "重新解析（version + 1，旧版本软删）",
        font=font(12), fill=MUTED)

img2.save(os.path.join(OUT_DIR, "mmrag-doc-state.png"))
print("saved state machine")






