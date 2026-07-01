# AI视频创作入门：从Seedance到成片全流程

> 不用学PR，不用买昂贵的设备，一个人用AI就能做出专业级短视频。本文手把手教你完整流程。

---

## AI视频创作，到底能不能用？

先说结论：**能，而且效果越来越好。**

2026年的AI视频生成已经过了「实验阶段」，Seedance 2.0、Wan 2.1等模型生成的视频质量，在很多场景下已经可以商用。

## 完整工作流

```
写提示词 → AI生成参考图 → 图生视频 → 视频合成 → 配音 → 导出
```

### 第一步：生成参考图（ComfyUI + Ideogram 4.0）

用结构化的JSON提示词生成高质量底图：

```json
{
  "high_level_description": "赛博朋克城市夜景，霓虹灯，雨夜街道",
  "style_description": "电影级光影，4K，超写实",
  "compositional_deconstruction": {
    "elements": [
      {"name": "背景城市", "position": "远景", "bbox": [0, 0, 1, 0.7]},
      {"name": "霓虹招牌", "position": "中景", "bbox": [0.3, 0.2, 0.5, 0.4]}
    ]
  }
}
```

> 💡 完整出图工作流 + 提示词模板: [ComfyUI出图全流程教程](https://afdian.com/item/29c5daa6754011f184ee5254001e7c00)

### 第二步：图生视频（Seedance 2.0）

把参考图输入Seedance 2.0，加动态描述词：

```
行人走动，霓虹灯闪烁，车流缓慢移动，雨滴下落，整体氛围宁静
```

关键参数：
- duration: 11秒（最佳质量）
- ratio: 9:16（竖屏短视频）
- 参考图: data URI格式

### 第三步：视频合成与处理

用ffmpeg做后期处理：

```bash
# 慢放 + Ken Burns效果
ffmpeg -i input.mp4 -filter_complex "setpts=1.5*PTS,zoompan=z=1.01:..."
# 多段合成+crossfade
ffmpeg -i clip1.mp4 -i clip2.mp4 -filter_complex "xfade=transition=fade:duration=1"
```

### 第四步：TTS配音叠

Edge TTS中文女声：

```bash
edge-tts --voice zh-CN-XiaoxiaoNeural --text "你的旁白文案" --write-media output.mp3
```

## 新手常见问题

**Q: 电脑配置要求高吗？**
A: ComfyUI推荐12GB以上显存，RTX 3060(12GB)就可以跑

**Q: 视频最长能做多久？**
A: Seedance单段15秒，多段合成可以做任意长度

**Q: 中文文字显示有问题？**
A: 底图不要带文字，用PIL后叠加系统字体

---

📦 **完整视频创作教程含脚本和提示词库**:
👉 [AI视频创作从入门到实战](https://afdian.com/item/270b25be754011f18a705254001e7c00) — ¥14.99

📚 **更多创作资源**: [https://afdian.com/a/felix007](https://afdian.com/a/felix007)
