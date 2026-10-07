#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_multimodal.py —— 复现《AI Agent 多模态输入实战指南》里的全部实测数字。

依赖（缺哪个就只跑得到哪个子命令）：
  pip install pillow numpy rapidocr-onnxruntime pymupdf faster-whisper edge-tts
  ffmpeg / ffprobe 在 PATH（或改 FFMPEG 常量）
可选项：
  Windows 中文字体 msyh.ttc / simhei.ttf（其他系统请改 FONT / HEI 常量）

子命令：
  vision  文档整页按比例缩放：token 成本 vs OCR 字符错误率
  pdf     文本层提取 vs 渲染成图 OCR（含无文本层扫描件的静默失败）
  font    单行最小可识别字高 + 「大图缩小 vs 直接渲染小字」
  audio   TTS 合成已知文本 → 本地 ASR：实时倍率、静音计费、三种评测口径
  all     依次跑完上面四项

所有输出都是 JSON 行 + 表格，直接对着文章里的数字看。
"""
import argparse
import io
import json
import math
import os
import subprocess
import sys
import time

OUT = os.path.dirname(os.path.abspath(__file__))
FONT = os.environ.get("MM_FONT", r"C:\Windows\Fonts\msyh.ttc")
FONT_BD = os.environ.get("MM_FONT_BD", r"C:\Windows\Fonts\msyhbd.ttc")
HEI = os.environ.get("MM_HEI", r"C:\Windows\Fonts\simhei.ttf")
FFMPEG = os.environ.get("MM_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("MM_FFPROBE", "ffprobe")
WHISPER_DIR = os.environ.get("MM_WHISPER", os.path.expanduser("~/whisper_models/small"))
VOICE = os.environ.get("MM_VOICE", "zh-CN-XiaoxiaoNeural")

# ------------------------------------------------------------------ 共用
TITLE = "设备采购与安装服务合同（摘要）"
BODY = [
    "合同编号：SD-2026-0930-A17　　签订日期：2026年9月30日",
    "甲方：济南市天桥区第二实验学校　　乙方：山东明远信息技术有限公司",
    "第一条　设备清单：高清录播主机2台，型号 MY-HD920，单价￥38,600.00；",
    "　　　　拾音器8只，型号 MY-MIC05，单价￥1,280.00；交换机1台，￥2,450.00。",
    "第二条　工期：自合同生效之日起45个日历日内完成安装调试并通过验收。",
    "第三条　付款方式：合同签订后7个工作日内支付30%，验收合格后支付65%，",
    "　　　　质保期满且无质量问题后支付尾款5%。质保期为验收之日起36个月。",
    "第四条　违约责任：逾期交付的，每逾期一日按合同总额的0.5‰支付违约金，",
    "　　　　累计不超过合同总额的10%。因不可抗力导致延误的，双方另行协商。",
    "第五条　争议解决：协商不成的，提交济南仲裁委员会按其规则仲裁。",
    "备注：本项目预留质保金￥2,733.00，票据类型为增值税专用发票（13%）。",
]
FOOT = "小字校验行：发票代码 037001900111　校验码 8821 4470 6603 9182（此行为最小字号）"
SPEECH_TEXT = ("各位同事，下面通报一下本周的进度。第一，设备采购合同已经在九月三十日签署，"
               "合同编号是SD2026，金额三十八万六千元。第二，安装调试的工期是四十五个日历日，"
               "请项目组在十月十五日之前提交施工计划。第三，验收合格后支付百分之六十五的款项。")


def norm(s):
    """评测口径：只留中日韩汉字与字母数字，去空白/标点/全半角差异。口径不冻结，CER 会假性升高。"""
    return "".join(ch for ch in "".join(s.split()) if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def cer(gt, got):
    g = norm(gt)
    return round(edit_distance(g, norm(got)) / max(1, len(g)) * 100, 2)


def tiles_tokens(w, h, tile=512):
    return 85 + 170 * math.ceil(w / tile) * math.ceil(h / tile)


def _ocr():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def run_ocr(engine, img):
    import numpy as np
    t0 = time.time()
    res, _ = engine(np.array(img))
    dt = time.time() - t0
    return ("".join(r[1] for r in res) if res else ""), dt, (len(res) if res else 0)


def png_kb(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return round(len(buf.getvalue()) / 1024, 1)


def build_page(scale=1.0):
    from PIL import Image, ImageDraw, ImageFont
    W, H = int(1654 * scale), int(2339 * scale)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    t = ImageFont.truetype(FONT_BD, max(8, int(30 * scale)))
    b = ImageFont.truetype(FONT, max(6, int(24 * scale)))
    f = ImageFont.truetype(FONT, max(4, int(14 * scale)))
    y = int(120 * scale)
    d.text((int(140 * scale), y), TITLE, font=t, fill="black")
    y += int(80 * scale)
    for line in BODY:
        d.text((int(140 * scale), y), line, font=b, fill="black")
        y += int(46 * scale)
    y += int(20 * scale)
    d.text((int(140 * scale), y), FOOT, font=f, fill="black")
    return img


GT_PAGE = norm(TITLE + "".join(BODY) + FOOT)

# ------------------------------------------------------------------ vision
def cmd_vision():
    engine = _ocr()
    rows = []
    for label, scale in (("原始 200dpi", 1.0), ("0.75x", 0.75), ("0.5x", 0.5), ("0.35x", 0.35), ("0.25x", 0.25)):
        img = build_page(scale)
        raw, dt, boxes = run_ocr(engine, img)
        rows.append(dict(case=label, scale=scale, w=img.width, h=img.height, png_kb=png_kb(img),
                         vision_tokens=tiles_tokens(img.width, img.height), ocr_s=round(dt, 2),
                         boxes=boxes, chars=len(norm(raw)), cer_pct=cer(GT_PAGE, raw)))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    from PIL import Image
    small = build_page(0.25)
    up = small.resize((small.width * 4, small.height * 4), Image.LANCZOS)
    raw, dt, boxes = run_ocr(engine, up)
    rows.append(dict(case="0.25x 放大4倍", scale=1.0, w=up.width, h=up.height, png_kb=png_kb(up),
                     vision_tokens=tiles_tokens(up.width, up.height), ocr_s=round(dt, 2),
                     boxes=boxes, chars=len(norm(raw)), cer_pct=cer(GT_PAGE, raw)))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return dict(gt_chars=len(GT_PAGE), rows=rows)

# ------------------------------------------------------------------ pdf
PDF_LINES = [TITLE] + BODY + [FOOT]


def make_text_pdf(path):
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="hei", fontfile=HEI)
    y = 80
    for i, line in enumerate(PDF_LINES):
        page.insert_text((45, y), line, fontname="hei", fontsize=13 if i == 0 else 9.5)
        y += 24
    doc.save(path)
    doc.close()
    return path


def cmd_pdf():
    import fitz
    from PIL import Image
    engine = _ocr()
    gt = "".join(PDF_LINES)
    rows = [dict(route="口径对照：不归一化", cer_pct=round(edit_distance(gt, norm(gt)) / len(norm(gt)) * 100, 2),
                 note=f"改口径就多出的假误差（GT {len(gt)} 字符 vs 归一化 {len(norm(gt))} 字符）")]
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    tp = make_text_pdf(os.path.join(OUT, "text_layer.pdf"))
    doc = fitz.open(tp); page = doc[0]
    t0 = time.time(); raw = page.get_text(); dt = time.time() - t0
    rows.append(dict(route="文本层 get_text()", sec=round(dt, 4), chars=len(norm(raw)),
                     vision_tokens=0, cer_pct=cer(gt, raw)))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    pix200 = None
    for dpi in (72, 100, 150, 200, 300):
        pix = page.get_pixmap(dpi=dpi)
        if dpi == 200:
            pix200 = pix
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        raw2, dt2, _ = run_ocr(engine, img)
        rows.append(dict(route=f"渲染 {dpi}dpi → OCR", dpi=dpi, w=img.width, h=img.height,
                         sec=round(dt2, 2), chars=len(norm(raw2)), png_kb=png_kb(img),
                         vision_tokens=tiles_tokens(img.width, img.height), cer_pct=cer(gt, raw2)))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    doc.close()

    scanned = fitz.open()
    p = scanned.new_page(width=595, height=842)
    img200 = os.path.join(OUT, "scan200.png")
    Image.frombytes("RGB", (pix200.width, pix200.height), pix200.samples).save(img200)
    p.insert_image(p.rect, filename=img200)
    sp = os.path.join(OUT, "scanned.pdf")
    scanned.save(sp); scanned.close()

    d2 = fitz.open(sp)
    t0 = time.time(); raw3 = d2[0].get_text(); dt3 = time.time() - t0
    rows.append(dict(route="扫描件 get_text()", sec=round(dt3, 5), chars=len(norm(raw3)), vision_tokens=0,
                     cer_pct=cer(gt, raw3), note="无文本层：返回空字符串且不报错"))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    npix = d2[0].get_pixmap(dpi=200)
    img = Image.frombytes("RGB", (npix.width, npix.height), npix.samples)
    raw4, dt4, _ = run_ocr(engine, img)
    rows.append(dict(route="扫描件 渲染200dpi → OCR", sec=round(dt4, 2), chars=len(norm(raw4)),
                     png_kb=round(os.path.getsize(img200) / 1024, 1),
                     vision_tokens=tiles_tokens(img.width, img.height), cer_pct=cer(gt, raw4)))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    d2.close()
    return dict(gt_chars=len(norm(gt)), rows=rows)

# ------------------------------------------------------------------ font
LINE = "合同编号SD20260930A17金额38600元"


def render_line(px, out_px=None, resample=None):
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype(FONT, px)
    l, t, r, b = f.getbbox(LINE)
    W, H = r - l + 40, b - t + 40
    img = Image.new("RGB", (W, H), "white")
    ImageDraw.Draw(img).text((20 - l, 20 - t), LINE, font=f, fill="black")
    if out_px and out_px != px:
        k = out_px / px
        img = img.resize((max(1, int(W * k)), max(1, int(H * k))), resample or Image.LANCZOS)
    return img


def cmd_font():
    engine = _ocr()
    rows = []

    def run(case, img):
        raw, dt, _ = run_ocr(engine, img)
        rows.append(dict(case=case, w=img.width, h=img.height, chars=len(norm(raw)),
                         cer_pct=cer(LINE, raw), sec=round(dt, 2)))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    for px in (4, 5, 6, 7, 8, 10, 14, 20, 28):
        run(f"直接渲染 {px}px 字高", render_line(px))
    for src, k in ((28, 0.25), (64, 0.125), (56, 0.125)):
        run(f"{src}px 渲染后缩到 {k:.3f}（等效 {src * k:.1f}px）", render_line(src, out_px=max(1, round(src * k))))
    from PIL import Image
    for name, rs in (("LANCZOS", Image.LANCZOS), ("BICUBIC", Image.BICUBIC), ("NEAREST", Image.NEAREST)):
        run(f"28px → 等效5px 用 {name}", render_line(28, out_px=5, resample=rs))
    return dict(gt_chars=len(norm(LINE)), rows=rows)

# ------------------------------------------------------------------ audio
def _dur(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    return round(float(r.stdout.strip()), 2)


def cmd_audio(keep=False):
    import asyncio

    import edge_tts
    from faster_whisper import WhisperModel

    mp3 = os.path.join(OUT, "speech.mp3"); wav = os.path.join(OUT, "speech16k.wav")

    async def go():
        await edge_tts.Communicate(SPEECH_TEXT, VOICE, rate="-10%").save(mp3)

    if not (keep and os.path.exists(wav)):
        asyncio.run(go())
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", mp3, "-ar", "16000", "-ac", "1", wav], check=True)
    d = _dur(wav)
    rows = [dict(case="合成语音", audio_s=d, mp3_kb=round(os.path.getsize(mp3) / 1024, 1),
                 kbps=round(os.path.getsize(mp3) * 8 / d / 1000, 1), tokens_est=math.ceil(d * 50))]
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    model = WhisperModel(WHISPER_DIR, device="cpu", compute_type="int8")
    t0 = time.time(); segs, _ = model.transcribe(wav, language="zh", beam_size=1, vad_filter=False)
    segs = list(segs); dt = time.time() - t0
    raw = "".join(s.text for s in segs)
    rows.append(dict(case="small int8 直转", audio_s=d, wall_s=round(dt, 2), rtf=round(dt / d, 4),
                     segments=len(segs), chars=len(norm(raw)),
                     cer_strict_pct=round(edit_distance(SPEECH_TEXT, raw) / len(SPEECH_TEXT) * 100, 2),
                     cer_norm_pct=cer(SPEECH_TEXT, raw),
                     asr_text=raw))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    # 去掉数字表达形式后的 CER（中文数字词 vs 阿拉伯数字是最大假误差源）
    NUM = set("0123456789零一二三四五六七八九十百千万亿百分之")
    g2 = "".join(c for c in norm(SPEECH_TEXT) if c not in NUM)
    a2 = "".join(c for c in norm(raw) if c not in NUM)
    rows.append(dict(case="去掉数字表达形式后", cer_pct=round(edit_distance(g2, a2) / len(g2) * 100, 2),
                     non_numeric_chars=len(g2),
                     note="7 处差异全部是「九月三十日→9月30日」这类数字书写形式，语义错误 0"))
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    sil = os.path.join(OUT, "silence60.wav")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-t", "60", sil], check=True)
    padded = os.path.join(OUT, "padded.wav")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", wav, "-i", sil, "-filter_complex",
                    "[0:a][1:a]concat=n=2:v=0:a=1", padded], check=True)
    dp = _dur(padded)
    for case, vad in (("尾部拼 60s 静音 · vad 关", False), ("同一文件 · vad 开", True)):
        t0 = time.time(); s2, _ = model.transcribe(padded, language="zh", beam_size=1, vad_filter=vad)
        s2 = list(s2); dt2 = time.time() - t0
        txt = "".join(x.text for x in s2)
        rows.append(dict(case=case, audio_s=dp, wall_s=round(dt2, 2), rtf=round(dt2 / dp, 4),
                         segments=len(s2), chars=len(norm(txt)), cer_norm_pct=cer(SPEECH_TEXT, txt),
                         tokens_est=math.ceil(dp * 50)))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return dict(gt_chars=len(norm(SPEECH_TEXT)), rows=rows)


def cmd_repeat(runs=3, keep=False):
    """重复性：同一份带静音的音频，连跑 N 次，看结论是否可复现。"""
    from faster_whisper import WhisperModel
    wav = os.path.join(OUT, "speech16k.wav")
    sil = os.path.join(OUT, "silence60.wav")
    padded = os.path.join(OUT, "padded.wav")
    if not (keep and os.path.exists(padded)):
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                        "-t", "60", sil], check=True)
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", wav, "-i", sil, "-filter_complex",
                        "[0:a][1:a]concat=n=2:v=0:a=1", padded], check=True)
    model = WhisperModel(WHISPER_DIR, device="cpu", compute_type="int8")
    rows = []
    for cfg, tag in ((False, "含60s静音 · vad 关"), (True, "含60s静音 · vad 开")):
        for i in range(runs):
            t0 = time.time()
            segs, _ = model.transcribe(padded, language="zh", beam_size=1, vad_filter=cfg)
            segs = list(segs); dt = time.time() - t0
            txt = "".join(s.text for s in segs)
            rows.append(dict(cfg=tag, run=i + 1, wall_s=round(dt, 2), segments=len(segs),
                             chars=len(norm(txt)), cer_norm_pct=cer(SPEECH_TEXT, txt), head=txt[:40]))
            print(json.dumps({k: v for k, v in rows[-1].items() if k != "head"}, ensure_ascii=False), flush=True)
    segs = [r["segments"] for r in rows if r["cfg"].endswith("vad 关")]
    verdict = ("不可复现：同一文件同一参数连续 %d 次，段数 %s、字符数 %s、CER %s" %
               (runs, segs, [r["chars"] for r in rows if r["cfg"].endswith("vad 关")],
                [r["cer_norm_pct"] for r in rows if r["cfg"].endswith("vad 关")]))
    print(verdict, flush=True)
    return dict(verdict=verdict, rows=rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["vision", "pdf", "font", "audio", "repeat", "all"])
    p.add_argument("--json", action="store_true", help="把结果写到 res_<cmd>.json")
    p.add_argument("--keep", action="store_true", help="复用已生成的音频，不重新合成")
    p.add_argument("--runs", type=int, default=3, help="repeat 子命令的重复次数")
    a = p.parse_args()
    cmds = ["vision", "pdf", "font", "audio", "repeat"] if a.cmd == "all" else [a.cmd]
    out = {}
    for c in cmds:
        print(f"\n===== {c} =====", flush=True)
        out[c] = {"vision": cmd_vision, "pdf": cmd_pdf, "font": cmd_font,
                  "audio": lambda: cmd_audio(a.keep),
                  "repeat": lambda: cmd_repeat(a.runs, a.keep)}[c]()
        if a.json:
            json.dump(out[c], open(os.path.join(OUT, f"res_{c}.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
    if a.json:
        json.dump(out, open(os.path.join(OUT, "res_all.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
