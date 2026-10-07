# 多模态输入预算与预处理工具

配套文章：[AI Agent 多模态输入实战指南](https://felixwang007.github.io/awesome-content-tools/articles/agent-multimodal-input-guide.html)

| 文件 | 作用 |
| --- | --- |
| `mm_budget.py` | 纯标准库计算器：送模型前先算 token 与路由（图片瓦片/PDF 文本层/音频静音），带 15 项 selftest |
| `bench_multimodal.py` | 复现文章全部实测数字（图片缩放、PDF 双路线、字高悬崖、音频 VAD 与重复性） |
| `res_*.json` | 本机实跑的原始结果，文章里的每一个数字都能对上 |

## 快速开始

```bash
# 1) 先算账（零依赖）
python mm_budget.py selftest
python mm_budget.py image --width 1654 --height 2339 --glyph-px 14
python mm_budget.py pdf   --pages 20 --extracted-chars 0
python mm_budget.py audio --seconds 89.21 --speech-seconds 29.21

# 2) 复现实测（需要下面这些依赖）
pip install pillow numpy rapidocr-onnxruntime pymupdf faster-whisper edge-tts
python bench_multimodal.py vision|pdf|font|audio|repeat  [--json]
```

## 实测结论速览（Windows / CPU OCR / faster-whisper small int8）

- 整页 A4 从 1.0x 缩到 0.5x：视觉 token 3485 → 1105（**省 68.3%**），字符错误率 0.26% → 0.51%。
- 缩到 0.35x/0.25x：CER 跳到 11.51% / 25.32%；把 0.25x 放大 4 倍回不去（35.29%），token 却花回满额。
- PDF 有文本层时：`get_text()` 0.0112s、0 token、CER 0%；渲染 200dpi 再 OCR 要 2.62s、3485 token。
- 扫描件 `get_text()` 返回空字符串且**不报错**（0.0083s），下游只看“调用成功”就会拿到 0 字。
- 音频里 60s 静音：token 1461 → 4461，本地转写 2.5s → 52.65s，而且**同一文件三次结果都不同**（11/6/12 段）。
- 开 VAD 后三次完全一致（6 段 / 2.45-2.55s）。
- 同一份**完全正确**的识别结果，CER 可以是 0% 也可以是 25.64%——差别只在你有没有冻结评测口径。

## 环境变量

`MM_FONT` / `MM_FONT_BD` / `MM_HEI`（中文字体路径）、`MM_FFMPEG` / `MM_FFPROBE`、`MM_WHISPER`（本地模型目录）、`MM_VOICE`（TTS 音色）。
