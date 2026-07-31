#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert docs/articles/*.md into standalone SEO HTML pages (dark theme, meta tags)."""
import os, re, markdown

ARTICLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "articles")

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <meta name="description" content="{description}">
    <meta name="keywords" content="{keywords}">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://felixwang007.github.io/awesome-content-tools/articles/{slug}.html">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #0f0f1a; color: #e0e0e0; line-height: 1.8; }}
        .container {{ max-width: 780px; margin: 0 auto; padding: 40px 24px; }}
        h1 {{ font-size: 1.9em; color: #fff; margin-bottom: 16px; line-height: 1.4; }}
        h2 {{ font-size: 1.4em; color: #8b8bf0; margin: 40px 0 16px; padding-bottom: 8px; border-bottom: 2px solid #2a2a5a; }}
        h3 {{ font-size: 1.15em; color: #fff; margin: 28px 0 12px; }}
        p {{ margin-bottom: 16px; color: #c8c8d8; }}
        a {{ color: #8b8bf0; text-decoration: none; }}
        a:hover {{ color: #a29bfe; }}
        blockquote {{ border-left: 4px solid #6c5ce7; background: #1a1a35; padding: 12px 20px; margin: 16px 0; border-radius: 0 8px 8px 0; color: #b0b0d0; }}
        code {{ background: #1a1a35; padding: 2px 8px; border-radius: 4px; font-size: 0.9em; color: #a29bfe; }}
        pre {{ background: #1a1a35; padding: 16px 20px; border-radius: 8px; overflow-x: auto; margin: 16px 0; }}
        pre code {{ background: none; padding: 0; color: #e0e0e0; }}
        ul, ol {{ padding-left: 24px; margin-bottom: 16px; color: #c8c8d8; }}
        li {{ margin-bottom: 8px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
        th, td {{ border: 1px solid #2a2a5a; padding: 10px 14px; text-align: left; }}
        th {{ background: #1a1a35; color: #fff; }}
        hr {{ border: none; border-top: 1px solid #2a2a5a; margin: 32px 0; }}
        .back {{ display: inline-block; margin-bottom: 24px; color: #6c5ce7; font-size: 0.9em; }}
        .footer {{ text-align: center; padding: 40px 20px; color: #555; font-size: 0.85em; border-top: 1px solid #1a1a35; margin-top: 40px; }}
        .footer a {{ color: #8b8bf0; text-decoration: none; }}
        strong {{ color: #fff; }}
        em {{ color: #a29bfe; }}
        @media (max-width: 768px) {{ .container {{ padding: 24px 16px; }} h1 {{ font-size: 1.5em; }} }}
    </style>
</head>
<body>
<div class="container">
    <a class="back" href="../index.html">← 返回导航站首页</a>
{content}
    <hr>
    <p>📚 <strong>更多创作资源</strong>: <a href="https://afdian.com/a/felix007">https://afdian.com/a/felix007</a></p>
</div>
<div class="footer">
    <p>© 2026 <a href="https://afdian.com/a/felix007">小帅在创作</a> · 自媒体创作与AI工具导航</p>
</div>
</body>
</html>
"""

META = {
    "ai-agent-automation-guide": {
        "title": "AI Agent自动化实战指南：从踩坑到真正落地",
        "description": "2026年AI Agent自动化落地指南：MCP协议详解、自动化五步法、真实踩坑记录（HTTP 200陷阱/状态漂移/批量垃圾）、免费工具清单与实战案例。",
        "keywords": "AI Agent,自动化,MCP,Agent框架,Claude Code,工作流自动化,AI工具,爬虫自动化",
    },
    "headline-formulas": {
        "title": "头条号标题怎么写才能爆？9大标题公式拆解（附30+案例）",
        "description": "头条号爆款标题9大公式：数字反差、悬念钩子、热点借势、痛点直击、对比冲突、权威背书、情感共鸣、反常识、指令引导，附30+实战案例和自检清单。",
        "keywords": "头条号,标题公式,爆款标题,自媒体写作,头条运营,点击率",
    },
    "ai-video-guide": {
        "title": "AI视频创作入门：从Seedance到成片全流程",
        "description": "AI视频创作完整流程：ComfyUI+Ideogram 4.0生成参考图、Seedance 2.0图生视频、ffmpeg合成、TTS配音，新手常见问题解答。",
        "keywords": "AI视频,Seedance,ComfyUI,图生视频,视频创作,ffmpeg,短剧",
    },
}

def main():
    for slug, meta in META.items():
        md_path = os.path.join(ARTICLES_DIR, slug + ".md")
        if not os.path.exists(md_path):
            print(f"SKIP {slug}: md not found")
            continue
        with open(md_path, encoding="utf-8") as f:
            md_text = f.read()
        # Strip the final CTA line already duplicated by template footer? Keep body as-is.
        body_html = markdown.markdown(md_text, extensions=["tables", "fenced_code", "nl2br"])
        html = PAGE_TEMPLATE.format(
            title=meta["title"],
            description=meta["description"],
            keywords=meta["keywords"],
            slug=slug,
            content=body_html,
        )
        out_path = os.path.join(ARTICLES_DIR, slug + ".html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"OK {slug}.html ({len(html)} bytes)")

if __name__ == "__main__":
    main()
