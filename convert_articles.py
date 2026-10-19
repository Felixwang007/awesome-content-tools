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
    "cli-gate-false-green-audit-guide": {
        "title": "门禁工具自己怎么查体：一条静态规则命中率只有 11%，而它漏掉的那个才是真缺陷",
        "description": "给校验脚本/linter/自检工具做体检的实战记录（全部数字为本机实跑）：核心缺陷形态「空集合满足全称命题」——扫描范围 0 个文件时「没有发现问题」自动成立，于是 0 个文件被判成满分/可以合并（真实样本：某代码审查包 SKILL.md 自证「可直接拿来做 CI 门禁」，指向空目录却输出「审查范围: 0 个文件，0 行」「健康分: 100/100 — ✅ 可以合并」并 exit 0）。三轮判据迭代：①静态规则 6 条扫 29 个技能包 141 个 .py（GATE_FAIL 24/WEAK 29/CLEAN 88；G001×20、G002×8、G003×9、G004×4、G005×30、G006×33 即 positional 26/flag 3/两套都有 4）②动态探针（48 个 scripts 脚本 × 空目录/无参数各一次，44/48 正确拒绝空输入，2 个确认缺陷、4 个只打用法退 0）③契约层（读 SKILL.md 判断工具是否自称门禁）。关键读数：静态 G003 precision 1/9=11%、recall 1/2=50%，8 个误报中 5 个把 `%d 个文件` 格式串读成真实零；G004 precision 1/4=25% 且其中一个静态指对了工具说错了机制（静态称无参数成功，实测 exit 1，真洞在空目录维度）；G001 命中 20 个但动态 18/20 都正确拒绝空输入——事实与含义是两回事。动态层自身也会误报（把性能计时器当门禁），故引入契约层 reads_as_gate；三层读数并排放让矛盾显形（license_check.py 三层互斥=需人看）。工具 gate_lint.py 纯标准库 selftest 38/38、退出码三态（0 通过/2 有 high 或抓到放行/3 零输入不算通过）、probe 默认拒绝执行需 --allow-exec、台账核对行保证每个观测恰好落进一个桶（第一版曾静默吞掉 1 行）。含 12 项上线检查清单与边界声明。",
        "keywords": "门禁工具,自检工具,false green,假绿灯,零输入,空集合全称命题,exit code,退出码三态,扫描范围,健康分,linter自检,selftest,静态规则精度,precision,recall,动态探针,契约层,SKILL.md契约,CI门禁,pre-commit,判据外置,一致性核对,观测台账,CLI体检,Python标准库,Agent工程质量,AI工具可靠性",
    },
    "windows-agent-automation-pitfalls-guide": {
        "title": "Windows 上跑 AI Agent 自动化：同一个 /tmp 有两个、端口被系统留着、PYTHONPATH 会劫持 import",
        "description": "Windows 上跑 AI Agent 自动化的三类环境坑与实测数据：bash 与原生程序的 /tmp 指向不同目录（文件写了下游读不到）、端口落在 Hyper-V/WSL 保留段报 WinError 10013、PYTHONPATH 继承导致子进程 import 别人项目的模块。含 11 条规则的自检工具 win_env_doctor（29 项 selftest）、原生命令编码 GBK/UTF-8 混用的解码策略、后台进程脱离会话与安全杀进程、12 项上线检查清单。",
        "keywords": "Windows,AI Agent,自动化,/tmp,PYTHONPATH,WinError 10013,端口保留,Hyper-V,git-bash,MSYS,编码GBK,自检工具,环境变量污染,Playwright自动化",
    },
    "agent-multimodal-input-guide": {
        "title": "AI Agent 多模态输入实战指南：图片、PDF、音频的预处理管线与成本悬崖",
        "description": "2026年AI Agent多模态输入工程实战（全部数字为本机实跑）：三类模态的计费公式（视觉瓦片85+170×瓦片数／音频按秒／文本按字符，同一页合同送图3485 token vs 转文本391 token差8.9倍）、图片缩放五档实测（1.0x→0.5x省68.3% token而错误率仅0.26%→0.51%，0.35x崩到11.51%、0.25x崩到25.32%）、放大不回血（0.25x放大4倍后错误率反升到35.29%且token花回满额）、PDF两条路线（文本层get_text 0.0112s/0token/0错误 vs 渲染200dpi+OCR 2.62s/3485token，快234倍）、扫描件静默失败（空字符串不报错0.0083s，只看调用成功会拿到0字）、渲染dpi从72到300（token涨7.9倍而错误率始终0-0.26%，「提分辨率买准确率」是伪命题）、字高悬崖实测（≥10px稳定0%，≤6px崩到12-24%，7-8px抖动且诚实标注样本仅25字不足）、大图缩小vs直接渲染小字（同一等效7px，一条0%一条32%）、音频静音计费（拼60s静音token 1461→4461，墙钟2.5s→52.65s，VAD开回到2.54s）、不可复现实证（同一文件同参数连跑三次段数11/6/12、字符179/102/179、CER 113.21%/44.34%/113.21%）、VAD开三次完全一致、评测口径的三层假误差（文本层23.53%→0%；ASR严格25.64%/归一化20.75%/去数字书写形式0.00%；繁简导致45.28%而真实同音错误仅2处）、可跑工具mm_budget.py（15项selftest零依赖）与bench_multimodal.py（五个子命令复现全部表格）、12项上线检查、边界声明。",
        "keywords": "多模态输入,视觉token,图片压缩,OCR,字符错误率,CER,PDF文本层,get_text,扫描件,VAD,静音裁剪,faster-whisper,语音转写,瓦片计费,tile,渲染dpi,字高,复现性,评测口径,繁简,数字规整,AI Agent工程,预处理管线,RapidOCR,PyMuPDF",
    },
    "ai-flavor-detection-rewrite-guide": {
        "title": "AI味检测与去AI化改写实战指南：零依赖检测脚本 + 4份真实文本实测",
        "description": "2026年中文AI味检测与去AI化实战（全部分数为本机实跑原样输出）：零依赖检测脚本deai_lint.py（纯标准库·不调用模型·同一文本永远同一分数）、4个统计量（句长变异系数CV·段落长度离散度·具体数字密度·句首最高重复次数）+6类词表与句式库（空转连接词·升华口号·空洞形容·无源数据·元话语说教·直呼读者·破折号密度）与完整计分公式（扣分按次数封顶·加分项存在导致总分非单调）、4份真实文本实测（模型原生68分REWRITE·同模型加4条要求改写后90分·通讯社特稿100分·气象通报100分·含数字密度0.0→39.51/千字对比）、两条被样本否掉的规则全过程（列举≠排比：第一版2-8字顿号三连规则命中30次=该稿判0分；CV硬门槛：同一稿件三种取文方式CV漂0.038>人机差距0.018）、4条硬门槛不许互相补偿（为什么不能只卡总分）、10条改写对照表、边界声明（文字层≠行为层：同一账号后台展现3536/3540而阅读为0）、13项selftest断言、复现命令与取文代码、上线检查清单11项。",
        "keywords": "AI味检测,去AI味,AI写作痕迹,文本检测脚本,零依赖,deai_lint,词表检测,句长变异系数,变异系数CV,数字密度,列举与排比,评分卡设计,硬门槛,内容门禁,改写对照表,文字层与行为层,假成功,内容质检,自媒体写作,写作规范,selftest,统计量,中文写作工具",
    },
    "agent-rule-binding-audit-guide": {
        "title": "AI Agent规则绑定实战指南：为什么「自报成功」会掩盖零产出（附绑定审计工具）",
        "description": "2026年AI Agent验收工程实战（全部数字为本机实跑输出）：读侧计数器vs写侧计数器（规则被读到12次、产物0件=在场≠绑定）、三档证据分级（SELF自报/PROC过程/ARTF产物·为什么exit 0不算证据·零输入返回非成功状态）、四象限判定（BOUND/UNBOUND_GREEN绿灯无产物/MIRAGE状态失真/HONEST_FAIL）、可跑工具tools/bind/bind_audit.py（账本六字段·file/ledger/url三类证据核验·0字节文件不算产物·未核验URL记DECLARED不参与绑定率·退出码即门禁）、样例账本23条记录6种故障形态真实输出（绑定率0.25·静默12.2天·门禁退出码2）、对真实26个定时任务的脱敏扫描（启用17/26·产物证据0·过程证据8·仅自报18·绿灯残留在未启用任务上7个最长59.3天）、URL证据真请求核验（200→VERIFIED/404→BROKEN）与「不带UA时不存在页面也返回200空壳」的审计陷阱、19项selftest断言、五个反模式、12项落地清单、边界声明。",
        "keywords": "AI Agent验收,规则绑定,binding,自报成功,假绿灯,false green,last_status,静默失败,零产出,外部产物,判据外置,验收外置,证据分级,exit code不算证据,缺勤检测,账本对账,JSONL账本,URL核验,User-Agent空壳,门禁设计,selftest,Agent工程,自动化可靠性,cron监控",
    },
    "agent-record-replay-tape-guide": {
        "title": "AI Agent调试成本控制实战：工具调用磁带（record-replay）与行为漂移检出",
        "description": "2026年AI Agent调试工程实战（全部数字为本机实跑输出）：工具调用磁带record-replay设计——append-only JSONL七字段（seq/tool/key/args/result/ms/side_effect）、缓存键=工具名+规范化参数哈希、三种事件区分（命中/漂移TapeDrift/落空TapeMiss）、严格模式与replay+record补录模式对比、副作用短路（回放实测live_tool_calls=0/real_side_effects=0）、五种规范化用例实测（dict键序·首尾空白·全角空格U+3000·数字vs字符串·布尔vs整数）、五轮演示真实输出（录制252.68ms→回放0.06ms）、注入式回放离线复现上游500降级分支、Windows文本模式CRLF导致工具自报70字节而磁盘72字节的真实字节差异、磁带边界声明（不做验收测试/会固化上游漂移）、与monkeypatch/HTTP层录制/fixture五种方案对比、七个反模式、上线前12项检查清单。",
        "keywords": "Agent调试,record replay,磁带回放,工具调用录制,行为漂移,drift检测,回归测试,确定性调试,JSONL磁带,参数指纹,规范化canonicalization,mock副作用,注入式回放,CI断言,Agent工程,LLM成本控制",
    },
    "agent-model-routing-guide": {
        "title": "AI Agent模型路由实战指南：健康探测、错误分类与降级链",
        "description": "2026年AI Agent多模型路由实战（全部数据实测）：liveness与readiness双探测设计（端点活着≠能推理）、五类错误分类表附带真实响应体（400 invalid model ID / 403地区限制含routing_funnel归因 / 404模型下线墓碑文案 / 429 / 5xx / 超时）、HTTP 200但content为空的假成功（reasoning_tokens吃光token预算，16→64复现验证）、约150行可跑的route_probe.py降级链脚本与实测trace、先约束后价格再延迟的选路策略、OpenRouter 460个模型真实价格分布（中位$0.42/M、p90 $3.00/M、357倍价差、24个免费、175个≥1M上下文）、粘性路由与提示词缓存税（cached_tokens实测）、按usage.cost记账、七个反模式、12项上线检查清单。",
        "keywords": "模型路由,Model Routing,多模型降级,fallback chain,健康探测,health check,liveness,readiness,模型下线404,429退避,假成功,reasoning_tokens,finish_reason length,提示词缓存,prompt caching,cached_tokens,OpenRouter,LLM网关,成本记账,Agent工程",
    },
    "video-batch-editing-automation-guide": {
        "title": "AI视频批量剪辑自动化实战指南：脚本化拼装、字幕烧录与质量门",
        "description": "2026年视频批量剪辑自动化实战（Windows+ffmpeg 8.1实测）：值不值得自动化的判断表、manifest清单驱动四段流水线、两种合轨路线对比（concat demuxer -c copy vs filter_complex concat，附实测时长）、静图zoompan运镜参数、字幕三大坑（Windows路径冒号被当选项分隔符报错原文/中文字体显式指定/硬字幕vs软字幕）、配音与BGM混音比例、loudnorm响度两遍法（实测-21.58→-16.00）、setpts+atempo时长对齐、六项质量门检查脚本、断点续跑账本、剪映草稿draft_content.json结构路线、十个实测踩坑对照表、15项上线检查清单。",
        "keywords": "视频批量剪辑,ffmpeg批量处理,自动剪辑,字幕烧录,subtitles滤镜,Windows路径转义,zoompan,Ken Burns,响度标准,loudnorm LUFS,concat拼接,剪映草稿,draft_content.json,短视频自动化,图文成片,口播视频,pix_fmt yuv420p,ffprobe质量检查",
    },
    "agent-long-task-resume-guide": {
        "title": "AI Agent长任务实战指南：断点续跑、幂等与进度对账",
        "description": "2026年AI Agent长任务（批量生成/批量爬取/数据集构建/批量转码）工程实战：可恢复执行单元四要素（清单manifest/账本ledger/幂等键/恢复入口）、四档执行粒度选择表、append-only JSONL账本字段设计、幂等三写法（幂等键/读回校验/自然键去重）与tmp+原子改名防呆、配置指纹防陈旧状态、约60行可照抄的ResumableRunner执行器、令牌桶限流与并发隔离熔断、三方对账与缺勤检测、五个真实踩坑对照表、上线前15项检查清单。",
        "keywords": "AI Agent长任务,断点续跑,checkpoint,幂等,idempotency,进度账本,JSONL,任务恢复,resumable,批量任务,对账,reconciliation,闲时续跑,Agent工程,批处理流水线,状态管理",
    },
    "agent-structured-output-guide": {
        "title": "AI Agent结构化输出实战指南：JSON Schema、约束解码与校验修复",
        "description": "2026年AI Agent结构化输出工程实战：四条约束路线对比（提示词/原生json_schema/约束解码/校验层）、schema设计十条规则、七类解析失败对照表（代码块包裹/截断/类型漂移/幻觉枚举/全角标点）、机械修复与校验重试循环代码、错误回注三条纪律、假成功三防护、版本钉扎与字段级不变量回归、宁失败不猜原则、14项上线检查清单。",
        "keywords": "结构化输出,Structured Output,JSON Schema,JSON Mode,约束解码,guided decoding,GBNF grammar,function calling,Agent工程,输出校验,jsonschema,大模型输出解析,提示词工程",
    },
    "agent-tool-calling-guide": {
        "title": "AI Agent工具调用实战指南：从工具设计到权限边界",
        "description": "2026年AI Agent工具调用（Tool Calling）工程实战：按副作用给工具分域（读/算/写/通信）、工具schema与description写法对照表、渐进式工具披露三跳、六类失败分类与重试纪律、写工具幂等键、假成功的三种来源与回读产物修法、权限即注册边界三档模型、大结果指针化、并行与冲突检测、五个可观测指标、14项上线检查清单。",
        "keywords": "AI Agent工具调用,Tool Calling,Function Calling,工具schema设计,MCP工具,权限边界,幂等,重试策略,假成功,Agent工程,LLM工具,function calling最佳实践",
    },
    "agent-browser-automation-guide": {
        "title": "AI Agent浏览器自动化实战指南：从Playwright MCP到登录态持久化",
        "description": "2026年AI Agent浏览器自动化落地指南：五种驱动方式选型对比、MCP服务在Windows上的三个部署坑（stdio失效/守护进程被杀/交互提示卡死）、登录态三层降级策略与Chrome v20 Cookie加密的破解路线、SPA页面点击三件套（自定义元素坐标点击/富文本输入/文案选择器）、反检测真实边界（假成功/Cloudflare全站挑战识别）、上线检查清单。",
        "keywords": "AI Agent浏览器自动化,Playwright MCP,浏览器自动化,browser use,登录态持久化,Cookie导出,反爬,反检测,SPA自动化,Agent工具,网页自动化,CDP",
    },
    "agent-cost-optimization-guide": {
        "title": "AI Agent成本优化实战指南：把Token花在能验证的产出上",
        "description": "2026年AI Agent成本工程实战：Token成本结构拆解、三类最贵浪费（静默成功/同因重试/常驻上下文臃肿）、六个降本手法（模型分层路由/提示词缓存/上下文压缩/本地模型/max_tokens设置/状态指纹缓存）、流水线四道门控、单位产出成本度量五指标、七天成本审计清单。",
        "keywords": "AI Agent成本,Token优化,大模型成本控制,提示词缓存,模型路由,本地模型,Agent自动化,成本工程,API费用优化,LLM成本",
    },
    "rag-knowledge-base-guide": {
        "title": "RAG检索增强生成实战指南：从零搭建企业知识库问答系统",
        "description": "2026年RAG落地完整指南：文档清洗/文本切分/Embedding选型/向量数据库对比/混合检索与重排优化/Agentic RAG/评估与调优/真实踩坑清单,附四周起步路线图。",
        "keywords": "RAG,检索增强生成,知识库,向量数据库,Embedding,混合检索,Rerank,Agentic RAG,企业知识库问答,大模型应用",
    },
    "agent-skills-guide": {
        "title": "AI Agent技能化实战指南：从一条提示词到一个能卖钱的技能包",
        "description": "2026年Agent技能(Skill)工程完整指南：SKILL.md结构与渐进披露原理、为什么技能化取代大提示词、四个生产环境踩坑记录、经验转技能五步法、技能市场变现地图(虾评/Agensi/Apify/Capafy)、上架实操要点与自查清单。",
        "keywords": "AI Agent技能,SKILL.md,Agent Skills,技能市场,提示词工程,Claude Skills,技能变现,渐进披露,Agent工程,技能化",
    },
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
