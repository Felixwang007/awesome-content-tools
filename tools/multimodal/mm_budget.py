#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mm_budget.py —— 多模态输入的「先算账再送模型」计算器（纯标准库，无网络、无模型调用）

用途：在把图片/PDF/音频送进模型之前，先算出它会花多少 token、走哪条路线、
以及哪条路线是本地能做完就不必送模型的。所有阈值来自实测（见同目录 bench_multimodal.py
与文章 docs/articles/agent-multimodal-input-guide.md）。

子命令：
  image  --width 1654 --height 2339 [--glyph-px 24]
  pdf    --pages 20 --extracted-chars 307 [--render-dpi 200]
  audio  --seconds 89.2 [--speech-seconds 29.2]
  text   --chars 12000 [--bytes 40000]
  selftest
"""
import argparse
import json
import math
import sys

# ---------------------------------------------------------------- 实测阈值
# 来源：本机 RTX3080 / RapidOCR / PyMuPDF / faster-whisper small 实跑（见文章表格）
GATES = {
    # 整页文档图缩放：1.0x/0.75x/0.5x 的整页 CER 为 0.26%/0.26%/0.51%，0.35x 跳到 11.51%
    "max_downscale": 0.75,
    "safe_downscale": 0.5,
    # 单行字高：≥10px 稳定 0%；≤6px 崩到 12%-24%；7-8px 结果抖动（样本 25 字，不做承诺）
    "min_glyph_px": 10,
    "unstable_glyph_px": 8,
    # PDF 文本层健康度：每页字符数低于此值按「空文本层」处理，转 OCR
    "min_chars_per_page": 50,
    # 音频：静音照样按秒计费，所以切掉静音是纯赚
    "stt_tokens_per_sec": 50,   # 估算口径，各家用不同系数，请以账单为准
    "rtf_no_vad": 0.556,        # 实测：含 60s 静音的 89.21s 音频，VAD 关 = 0.556
    "rtf_vad": 0.029,           # 实测：同文件 VAD 开 = 0.029
}


def tiles(w, h, tile=512):
    return math.ceil(w / tile) * math.ceil(h / tile)


def vision_tokens(w, h, detail="high", tile=512):
    """OpenAI 系视觉输入的 tile 计费模型：high 走瓦片，low 固定 85。"""
    if detail == "low":
        return 85
    return 85 + 170 * tiles(w, h, tile)


def cost_saved_pct(w0, h0, w1, h1):
    a, b = vision_tokens(w0, h0), vision_tokens(w1, h1)
    return round((1 - b / a) * 100, 1)


def audio_tokens(seconds, rate=None):
    rate = rate or GATES["stt_tokens_per_sec"]
    return int(math.ceil(seconds * rate))


def text_tokens(s):
    """粗口径：CJK 每字 1 token，ASCII 每 4 字符 1 token。只用于同级比较。"""
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    other = len(s) - cjk
    return cjk + math.ceil(other / 4)


def plan_image(w, h, glyph_px=None, detail="high"):
    cur = vision_tokens(w, h, detail)
    out = {"width": w, "height": h, "vision_tokens": cur, "tiles": tiles(w, h)}
    if detail == "low":
        out["decision"] = "low 模式固定 85 token，但长图会被压到 512 方框，文档类内容会失真"
        return out
    # 先按保守阈值缩放
    k = 1.0
    if glyph_px:
        # 缩到字高刚好等于 min_glyph_px 的比例
        k = min(1.0, GATES["min_glyph_px"] / glyph_px)
        reason = f"按最小字高 {glyph_px}px 反推：缩到 {round(k,3)} 时字高刚好 {GATES['min_glyph_px']}px"
    else:
        k = GATES["max_downscale"]
        reason = f"未给字高，按保守阈值缩到 {GATES['max_downscale']}"
    k = max(k, 0.1)
    nw, nh = max(1, int(w * k)), max(1, int(h * k))
    out.update({
        "suggest_scale": round(k, 3),
        "suggest_size": [nw, nh],
        "suggest_tokens": vision_tokens(nw, nh),
        "saved_pct": cost_saved_pct(w, h, nw, nh),
        "reason": reason,
    })
    if glyph_px and glyph_px < GATES["min_glyph_px"]:
        out["decision"] = "别送模型：有效字高已低于可读阈值，先找到原始分辨率或原始 PDF"
    elif k < 1.0:
        out["decision"] = f"可缩放到 {round(k,3)} 再送：省 {out['saved_pct']}% 视觉 token"
    else:
        out["decision"] = "维持原图"
    return out


def plan_pdf(pages, extracted_chars, render_dpi=200, page_pt=(595, 842)):
    per_page = extracted_chars / max(1, pages)
    out = {"pages": pages, "extracted_chars": extracted_chars, "chars_per_page": round(per_page, 1)}
    if per_page >= GATES["min_chars_per_page"]:
        out.update({
            "route": "text_layer",
            "vision_tokens": 0,
            "why": "文本层可直接提取：本机实测每页 0.011s、0 token、0 错误",
        })
        return out
    if extracted_chars == 0:
        out["silent_failure"] = True
        out["why_empty"] = ("提取为空且不报错——扫描件就是这个形态；"
                            "下游如果只看「调用成功」会把 0 字当成功")
    w = int(page_pt[0] * render_dpi / 72)
    h = int(page_pt[1] * render_dpi / 72)
    out.update({
        "route": "ocr",
        "render_dpi": render_dpi,
        "render_size": [w, h],
        "vision_tokens_per_page_if_sent_raw": vision_tokens(w, h),
        "why": "文本层不健康，必须走 OCR；OCR 出来的文本再送模型，成本按文本算而不是按图算",
    })
    return out


def plan_audio(seconds, speech_seconds=None):
    speech = seconds if speech_seconds is None else speech_seconds
    silent = max(0.0, seconds - speech)
    saved_tokens = audio_tokens(silent)
    out = {
        "seconds": round(seconds, 2),
        "speech_seconds": round(speech, 2),
        "silent_seconds": round(silent, 2),
        "tokens_if_sent_raw": audio_tokens(seconds),
        "tokens_after_vad": audio_tokens(speech),
        "token_saved_pct": round(saved_tokens / max(1, audio_tokens(seconds)) * 100, 1),
        "wall_before_s": round(seconds * GATES["rtf_no_vad"], 2),
        "wall_after_s": round(seconds * GATES["rtf_vad"], 2),
    }
    if silent > 0:
        out["decision"] = (f"先做 VAD/静音裁剪：{silent:.1f}s 静音会照单计费 "
                           f"（{audio_tokens(silent)} token 换 0 个字），本地转写耗时也从 "
                           f"{out['wall_before_s']}s 降到 {out['wall_after_s']}s")
    else:
        out["decision"] = "无静音可裁；本地先转文本再送模型（文本 token 通常只有音频的 1/10）"
    return out


def plan_text(chars=None, text=None):
    s = text if text is not None else "中" * (chars or 0)
    return {"chars": len(s), "tokens_est": text_tokens(s), "note": "文本永远比音频/图片便宜，能转文本就先转"}


# ---------------------------------------------------------------- 自检
def selftest():
    ok = []
    ck = lambda name, cond: ok.append((name, bool(cond)))  # noqa: E731

    ck("瓦片数 1654x2339 = 4*5=20", tiles(1654, 2339) == 20)
    ck("视觉 token 1654x2339 = 3485", vision_tokens(1654, 2339) == 3485)
    ck("视觉 token 827x1169 = 1105", vision_tokens(827, 1169) == 1105)
    ck("low 模式固定 85", vision_tokens(9999, 9999, "low") == 85)
    ck("缩一半省 68.3%", cost_saved_pct(1654, 2339, 827, 1169) == 68.3)

    im = plan_image(1654, 2339, glyph_px=24)
    ck("字高 24px 时可缩到 0.417", im["suggest_scale"] == 0.417)
    ck("缩放后 token 变少", im["suggest_tokens"] < im["vision_tokens"])
    ck("低字高不给缩放方案", plan_image(1654, 2339, glyph_px=5)["decision"].startswith("别送模型"))

    pdf = plan_pdf(20, 6000)
    ck("文本层健康走 text_layer", pdf["route"] == "text_layer" and pdf["vision_tokens"] == 0)
    pdf2 = plan_pdf(20, 0)
    ck("0 字判空文本层", pdf2["route"] == "ocr" and pdf2["silent_failure"] is True)
    pdf3 = plan_pdf(1, 30, render_dpi=100)
    ck("低于每页 50 字转 OCR", pdf3["route"] == "ocr" and pdf3["render_size"] == [826, 1169])

    au = plan_audio(89.21, 29.21)
    ck("静音被算出 3000 token", au["tokens_if_sent_raw"] - au["tokens_after_vad"] == 3000)
    ck("VAD 墙钟从 49.6s 降到 2.59s", au["wall_before_s"] == 49.6 and au["wall_after_s"] == 2.59)
    ck("无静音时提示直接本地转写", "本地先转文本" in plan_audio(29.21)["decision"])

    ck("文本 token 估算 CJK=1", plan_text(text="合同编号SD2026")["tokens_est"] == 4 + 2)

    bad = [n for n, v in ok if not v]
    for n, v in ok:
        print(("PASS " if v else "FAIL ") + n)
    print(f"\n{len(ok) - len(bad)}/{len(ok)} passed")
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="多模态输入预算计算器")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("image"); pi.add_argument("--width", type=int, required=True)
    pi.add_argument("--height", type=int, required=True); pi.add_argument("--glyph-px", type=float)
    pi.add_argument("--detail", default="high", choices=["high", "low"])

    pp = sub.add_parser("pdf"); pp.add_argument("--pages", type=int, required=True)
    pp.add_argument("--extracted-chars", type=int, required=True); pp.add_argument("--render-dpi", type=int, default=200)

    pa = sub.add_parser("audio"); pa.add_argument("--seconds", type=float, required=True)
    pa.add_argument("--speech-seconds", type=float)

    pt = sub.add_parser("text"); pt.add_argument("--chars", type=int, required=True)

    sub.add_parser("selftest")

    a = p.parse_args(argv)
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "image":
        r = plan_image(a.width, a.height, a.glyph_px, a.detail)
    elif a.cmd == "pdf":
        r = plan_pdf(a.pages, a.extracted_chars, a.render_dpi)
    elif a.cmd == "audio":
        r = plan_audio(a.seconds, a.speech_seconds)
    else:
        r = plan_text(a.chars)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
