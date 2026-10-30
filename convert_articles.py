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
    "agent-clock-calibration-guide": {
        "title": "time.monotonic 把 5 毫秒读成 0、本机时钟快 3.26 秒：Agent 计时的四个钟，只有一个是量具",
        "description": "Agent 的超时、限速、耗时统计、事件时间戳都默认「刻度够细、钟不会偏、时间戳能排序」，2026-10-30 在本机（Windows 11 + Python 3.14.6）实测三条全不成立，数字全部原样输出。①四个钟的真实刻度：20 万次连续读只统计「值变过」的次数——time.time 只推进 10 次（刻度 0.000500s）、datetime.now 78 次、time.monotonic 只推进 1 次（刻度 0.015000s，跨 5 秒 320 次推进＝0.016×200+0.015×120）、perf_counter 199,375 次（100ns）、perf_counter_ns 184,783 次；结论是「测耗时用 monotonic」这句流行建议在本平台只解决「不倒退」、没解决「看不清」。②同一负载三种钟记账（60 次 sleep(0.005)，真值 0.3257s）：time.time +0.064%、monotonic +0.720%、perf_counter −0.027%——但 monotonic 单次读数 65% 读成 0、35% 读成 15.6ms，量化误差求和时互相抵消、单点上完全不抵消（「平均值正确」掩盖「单点错误」）。③sleep 九档实测：请求 0.0001s 实得 0.000499（4.99×）、0.001 实得 0.001495（1.50×）、0.002→1.21×、0.005→1.08×、0.01→1.04×、0.0156→1.02×、0.02→1.02×、0.05 与 0.1→1.00×，固定开销约 +0.5ms，故 sleep 的语义是「不早于」不是「等于」。④限速两种写法同机器对比：naive（每轮 sleep(1/rate)）在 10/50/100/500/1000/2000 每秒下达成率 99.8/98.8/96.5/82.1/67.3/49.3%，绝对时刻表（next=last+1/rate，落后跳过睡眠）全部 100.0%——naive 误差逐轮累加，达成率＝interval/(interval+0.5ms)。⑤逐次记账 vs 一次测量，4 个 n × 每个连跑 3 次：墙钟累加误差 n=200 −100%/三跑、n=600 +16.1%/+14.2%/+129.5%、n=1500 −1.3%/−3.1%/−3.0%、n=3000 −0.5%/−26.2%/−75.6%（方向和幅度都在跳）；单调钟累加 12 次全部落在 −17.9%~−20.0%（漏的是循环体与第二次读钟的开销，有方向有解释）。⑥时间戳不能当顺序：2000 个快事件用 time.time 打戳只有 3 个不同取值、并列率 99.9%，perf_counter 并列率 0.0%——按时间戳排序等于按排序算法稳定性排序。⑦墙钟本身不准：UDP NTP 三源 offset time.windows.com +3.259s / pool.ntp.org +3.266s / ntp.aliyun.com +3.263s（极差 6.4ms），HTTP Date 头交叉 +3.496s / +4.020s（秒级分辨率）→ 本机比真实时间快 3.26 秒，此时「跨机比时间」「用本机 now 判过期」全部静默错 3 秒。⑧用墙钟判 50ms deadline：300 次里 202 次越界（67.3%）、最大越界 0.527ms＝一个刻度（1.05%）、32.7% 样本读成「小于预算」——精度下界由刻度决定不由代码决定。⑨工具 tools/clock/clock_calib.py（ticks/sleep/pacer/account/deadline/witness/ntp/all/selftest，纯标准库，22 项自检全绿，退出码 0 正常/2 发现异常/3 前置条件不满足/4 零输入/5 自检失败），今日 ntp 退出码就是 2（verdict=CLOCK_OFFSET）。⑩自检抓到的两个真问题：第一版断言「单调钟记账损失≤墙钟」当场 FAIL（mono 19.33% vs wall 3.97%，因为墙钟向上取整会把账记成比事实还多、损失可为负）；「零增量占多数」只能在「刻度>10×单笔耗时」时断言，否则记 SKIP 并打印原因，乱断言比跳过更糟。含 12 项上线检查、七行「钟–用途」绑定表与七条边界声明（读数只属本机本次、Windows monotonic 走 GetTickCount64 而 Linux 通常纳秒级要重测、SNTP 未认证只能测量级、HTTP Date 只作交叉印证、busy-wait 故意的、不测系统时钟被跳变、不替代 profiler）。",
        "keywords": "time.monotonic 分辨率,monotonic 15.6ms,GetTickCount64,time.time 刻度,perf_counter,perf_counter_ns,datetime.now,计时不准,耗时统计,量化误差,单次读数,求和误差,profiling 开销,sleep 精度,Windows timer,sleep 超时倍数,节拍器,限速写法,绝对时刻表,naive sleep rate limit,达成率,令牌桶,burst,逐次记账,累加误差,时钟刻度,事件时间戳,并列率,tie,排序稳定性,seq,单调递增,墙钟偏移,NTP,SNTP,时钟同步,offset 测量,HTTP Date 交叉验证,deadline 判定,超时精度,busy-wait,Agent 工程,自动化可靠性,可观测性,时间语义,跨机时间对齐,Python 标准库,selftest,退出码门禁,条件断言,伪不变量,Windows 计时,时钟校准",
    },

    "timeout-phase-splitting-guide": {
        "title": "同一个端口，timeout=2 判超时、timeout=3 判「拒绝连接」：把超时拆成五段，才知道该修哪一段",
        "description": "2026-10-28 本机实跑的请求超时分层测量（数字全部原样输出）：①一个 21 秒的悬案——会话开始时 curl https://github.com/ 返回 000 / time_total=21.025939，同一会话稍后同一 URL 返回 SLOW_UP / 71,240 字节 / 0.803 秒；单独测量「系统何时放弃」得到 21.015 秒（WinError 10060），两者只差 10.94 毫秒，证明那 21 秒不是任何人的超时，而是内核 SYN 重试放弃点——把阈值从 10 改成 30/60 在这一层等于什么都没做。②十个目标的逐阶段读数（DNS/TCP/TLS/首字节/整包 + 实等 elapsed）：api.github.com 0.0147/0.0963/0.2038/0.1579/0.7549、github.com 0.0066/0.0795/0.0827/0.2366/0.8030、Pages CDN 1.7553、api.deepseek.com 0.1461、本机开着端口 0.0472、raw.githubusercontent.com（TCP 从未建立）6.0003、10.255.255.1 6.0000、不存在的域名 0.0010——**最快与最慢的失败差 6000 倍，而在日志里都叫 timeout**。③同一个关闭端口、只改阈值就翻转判定：关闭环回端口的 RST 到达 2.0149/2.0090/2.0064 秒（三次复跑），timeout=2.0 判 CONNECT_TIMEOUT（tcp=2.0002）而 timeout=3.0 判 CONNECT_REFUSED（tcp=2.0129）——判定名不是事实，读数才是。④六态判定（UP/SLOW_UP/STALLED_NO_BYTES/STALLED_PARTIAL/TRUNCATED/CONNECT_*/TLS_FAIL/DNS_FAIL）与「有进展=字节到达，不是时间」的判据；相位占比不稳定（同一目标十分钟内 TLS 段 0.5202→0.0827，6.3 倍），相位拆分只用于定位断点、不用于容量估算。⑤每段归谁管（DNS/内核与过滤驱动/证书与服务端/上游排队/链路）与七行重试动作表，红线是「请求已发出且可能产生副作用之后不重试」。⑥重试预算公式与实测：3 次 × 6 秒阈值串行 18.01s（复跑 18.19s），并行 3 次 6.05s（3.0x），对「连不上」串行重试是纯乘法。⑦分相位超时的四种写法（requests (connect,read) 元组 / httpx Timeout 四段 / 裸 socket settimeout / curl 的 --connect-timeout 与 --max-time 之分，后者不是分相位）。⑧工具 tools/timeout/phase_probe.py（probe/classify/selftest，纯标准库，32 项自检，退出码 0 有进展/2 连上没走完/3 没连上/4 零输入/5 自检失败），classify 对今天十行读数离线复判与在线判定 10/10 一致，样本 tools/timeout/samples/live_readings_20261028.json。⑨写这个工具时自检抓出的三个真 bug：中途解析的头部被当成最终头部（正常 200 被判 TRUNCATED）、断言写成了平台假设（「关闭端口必 RST<0.05s」在本机实测 2.01s 直接打脸，改为锁不变量）、相位缺失被当成相位失败（https 且 tls_s 为空被判 TLS_FAIL，把两个「TCP 从未建立」的样本判成两种相反动作）——三条都已补成回归断言。含十条落地规则与七条边界声明（只描述一次测量、不预测下一次、read_cap 是工具自己的上限不冒充对端截断、无 deepest_phase 时不猜）。",
        "keywords": "超时,timeout,connect timeout,read timeout,分相位超时,DNS解析,TCP连接,TLS握手,首字节,TTFB,time_total,curl --connect-timeout,--max-time,requests timeout tuple,httpx Timeout,settimeout,SYN重试,WinError 10060,WinError 10061,RST,连接被拒绝,CONNECT_REFUSED,CONNECT_TIMEOUT,DNS_FAIL,TLS_FAIL,STALLED_NO_BYTES,STALLED_PARTIAL,TRUNCATED,Content-Length,chunked,假成功,重试预算,retry budget,指数退避,幂等重试,负期望重试,网络诊断,链路排查,CLI工具,退出码门禁,selftest,采样与判定分离,离线复判,classify,AI Agent工程,自动化可靠性,Python标准库,Windows网络,过滤器驱动,页面无响应排查",
    },
    "agent-output-truncation-detection-guide": {
        "title": "翻到空页不等于读完了：533,692 条里拿到 1,000 条，服务端在你越过上限之后把「总量」也改成了 0",
        "description": "Agent「读到」与「读完」之间隔着一个必须有人提供的数字（数字均为 2026-10-26 本机原样读数）。①四种「读了一半」只有一种会喊出声：好截断（读 465,000 字节/5,000 行文件返回 1,042 行，同时给 truncated_by=bytes + total_lines=5000 + next_offset=1043）、坏截断（GitHub commits per_page=1000：5/5 次 200+半截 body，IncompleteRead(49311 bytes, 16225 more expected)，49311+16225=65536 正好一个 64KiB 分块）、静默封顶（请求 limit=5000 实得 1000 且无 total/has_more/next/Link 任何总量字段）、假完整（翻到空页被当成读完）。②同一端点只改每页条数：per_page=1 一次成功、per_page=100 十八次只成功一次（5.6%）、per_page=1000 五次全半截——「接口不稳」多半是「一次要得太多」；且那唯一一次干净的 200（634,981 字节/100 条）自身带着 rel=next 指向第 1,102,337 页。③Algolia 现场：nbHits=533,689 但 hitsPerPage=5000 只给 1,000 条且 nbPages=1；hitsPerPage=100 翻到 page=9 正常（第 1,000 条），page=10 越过硬顶返回 200 + hits=0 + nbHits=0 + nbPages=0——行业默认的「空页即结束」循环会安静成功退出、拿到 1,000/533,692（0.19%）且没有任何字段与「已取全集」矛盾；三次读到的总量 533,688/533,689/533,692 分钟级漂移，故对账不能要求纯相等。④判定必须是三态而非两态：COMPLETE 需要正面证据（total==items / has_more=false / 结束标记 / 行数字节都对上），TRUNCATED 需要截断证据，UNKNOWN 是默认结局而非「应该没问题」。⑤八条规则 TR001-TR009 分三层（流层 Link/字节数、集合层 total/has_more/覆盖区间、语义层截断措辞/空页归因），其中 TR006「请求大于实得且实得命中常见上限且零总量字段」故意判 UNKNOWN 而不是 TRUNCATED——分不出来就别写成已确认。⑥工具 tools/trunc/trunc_probe.py（judge/selftest，纯标准库，25/25 自检，退出码 0 COMPLETE/2 TRUNCATED/3 UNKNOWN/4 零输入/5 自检失败）+ 六份快照（四份现场实测：Algolia、GitHub 两个、本地大文件；两份文件名里直说 constructed_ 的构造样本），六条判定输出逐字贴出。⑦四条落地硬规则：工具返回必须带总量三件套之一、UNKNOWN 不许写成产出、终止判据不能只看空页且要允许总量漂移、重试前先分类错误（200+parse fail=半截要降 page size，4xx=请求错，200+parse ok+零总量字段=去补证据）。含六条边界声明（快照是调用方提供的事实、不做语义判重、TR006 是怀疑不是结论、抖动时不判 COMPLETE 是故意的、续读点只是建议 Algolia 那条路径 1,000 就是硬顶、自检不覆盖网络行为）。",
        "keywords": "输出截断,truncation,分页,pagination,翻页,空页即结束,终止判据,total vs items,nbHits,nbPages,has_more,next_offset,Link rel=next,分块传输,chunked,IncompleteRead,半截正文,HTTP 200,parse fail,静默封顶,silent cap,limit 上限,数据完整性,全集证明,三态判定,COMPLETE TRUNCATED UNKNOWN,假绿灯,退出码门禁,selftest,GitHub API,Algolia,HN 搜索,大文件读取,read budget,续读点,resume,重试分类,错误分类,降 page size,AI Agent 工程,上下文截断,context truncation,RAG 取全,爬虫翻页,数据对账,零输入不是干净输入",
    },
    "parallel-tool-call-safety-guide": {
        "title": "同一个追加写，Linux 丢 0 条、Windows 丢 309 条：并行工具调用里什么能并、什么必须排队",
        "description": "并行工具调用的三层判据与全程实测（数字均为本机原样输出）：①并行到底能省多少——四类负载同一并发度差 10 倍以上：24 个 3MB 文件 sha256 串行 0.658s，P4 0.416s/P8 0.413s/P24 0.420s 封顶 1.59x；20 次进程启动 0.400s→P4 0.039s（10.4x）；12 次纯等待 3.004s→P4 0.753s(3.99x)/P12 0.252s(11.94x)；12 次真 HTTP 11.521s→P4 0.642s(17.96x) 但 P12 掉到 0.886s(13.00x)。并给出「加速比 19.53x 超过并发度 4」的假象来源：串行基线里含每次约 0.4s 的进程开销，纯线程复测立刻回到 3.99x——加速比衡量的是固定开销占比，不是并发能力。②写为什么必须排队：8 写者×10 轮的读改写丢 87%（期望 80 实得 10/11/10）；追加写也丢（18/29/24）。2×2 实测定位变量：屏障同步+每条重开丢 67/240、屏障同步+单 fd 只活 15/15/16 条（丢 194/240）、错峰启动两格全 0；同一脚本在 WSL Ubuntu 四格全 0/240——Windows 261 条 vs Linux 0 条，得出「Windows 的 O_APPEND 是打开时取位置、不是每次 write 原子」，并说明错峰不丢只是时序没重叠的运气（probe 复跑读到 0 条与 20 条两种结果）。③三层判据分离（声明的 plan / 实际的 ledger / 底层的 probe）与各自单独使用的失效方式。④真实 9 任务文档站刷新 spec：9 调用→5 批、最大宽度 4、理论加速仅 1.73x（受最长串行链 sitemap→commit→push 限制，调大 max_parallel 无效）。⑤分批器第一版真实 bug：贪心只查冲突不查方向，把 render_sitemap 排到其生产者 build_index 之前（输出自洽、accounting_ok 为真），修法=批号必须大于所有冲突前置 job 的批号，批数 3→5、加速 2.69x→1.73x，并补 5 条方向断言。⑥批内失败无回滚实测（[T,F,T,T,T] → 落地 4 个文件、rolled_back 为空），批边界是唯一恢复点。⑦ledger 对账抓到的那一对正是丢掉更新的那一对（并发 final_n=5 丢 3 → RACE 检出 w7×w6 重叠 0.0150s；串行 8/0 → NO_OVERLAP），含读读重叠不算竞态、边界相接不算重叠两条判定。⑧工具 tools/parbatch/par_batch.py（plan/ledger/probe/selftest，纯标准库，44 项自检，退出码 0/2/3/5）+ make_ledger_samples.py 现场生成真实 ledger。含一页并行规则表与边界声明（plan 只知道你声明的效果、贪心非最优、probe 只测 append 语义、毫秒数为本机一次实测）。",
        "keywords": "并行工具调用,并发安全,丢失更新,lost update,读改写,WW冲突,RW冲突,exclusive冲突,文件锁,O_APPEND,append原子性,Windows文件并发,Windows丢数据,Linux原子追加,WSL对照,跨平台差异,竞态检测,race detector,ledger对账,声明与实际,原子性探测,分批,分批执行,批边界,回滚,部分失败,依赖排序,贪心着色,生产者消费者顺序,串行链,理论加速上限,max_parallel,并发度,进程启动开销,HTTP并发,P12更慢,AI Agent工程,自动化正确性,Python标准库,selftest,退出码门禁",
    },
    "agent-skill-supply-chain-audit-guide": {
        "title": "装 AI 技能/工具之前的三层验尸：1105 星的仓库里只有一个 README，写着「不要信这个仓库」",
        "description": "2026-10-21 本机实跑的供应链审计记录，三个样本对应三层检查，任何一层单独用都会给出「看起来正确」的错误结论。①结构取证（离线读 GitHub API 回包，不解包不 import）：30 个 created:>10-14 的仓库判 FARM_SUSPECT=1/NEEDS_EYES=2/ORGANIC=27，4 个无 license、4 个体积<60KB、星数中位数 1093；同一套阈值在 10-14 那轮是 FARM_SUSPECT=0/NEEDS_EYES=7/ORGANIC=23——阈值是触发器不是测量。star:watch 被显式弃用：农场的 39.5 落在正常库 40.8~570.4 区间内，完全重叠。②内容静态审计（tar.gz 不解包、不落盘）：某 MIT 红队技能库 97 文件出 203 条 HIGH/MEDIUM（H5×125/M4×25/H6×21/M6×15/H4×8/H3×7/M3×1/H9×1），按文件类型拆开后 201 条在 .md、2 条在 .json、可执行代码 0 条，安装面 install.sh 是纯拷贝零网络——命中条数不携带风险，落点才携带。给出「安装即执行 > 导入即执行 > 需调用 > 仅文档」四档判别表。③读那个文件：被判 FARM_SUSPECT 的 1105 星仓库内容扫描 exit 0 CLEAN（1 个文件 0.00MB 零命中），而那唯一的 README 内容是作者本人声明「这里没有官方 XScreenSaver 仓库……你应当不要相信它」——结构层对、内容层对，结论只有第三层给得出。附带反直觉对照实验：两轮 60 个样本把 events 全部拿掉重跑，判定 0 处变化、reasons 含到达形态 0 条，说明 WatchEvent 挤簇判据在这批样本上从未当过判据，只是解释字段。含退出码四态语义（0 干净/2 需看/3 拒绝/4 零输入/5 自检失败，「没输入」不是「输入干净」）、两个工具 selftest 20/20 与 11/11 当场读数、扫描成本 158ms/206ms/944ms、今日踩坑「--quiet 会连 --json 审计证据一起安静掉」、装之前 12 项检查清单与边界声明。",
        "keywords": "供应链审计,AI技能安全,装工具之前,GitHub仓库取证,star农场,farm suspect,star:watch,fork比,首发延迟,tar.gz不解包扫描,静态规则,curl|bash,install hook,安装即执行,在场不等于危险,命中落点,退出码门禁,零输入假绿灯,selftest,WatchEvent,到达形态,挤簇,阈值校准,启发式不是事实,codeload,gh api,repo_forensics,supply_audit,AI Agent工程,零依赖Python",
    },
    "github-push-blocked-fallback-publish-guide": {
        "title": "git push 卡死 21 秒，api.github.com 却 548 毫秒返回：把产物送上线的三条通道与 226 个 blob 的对账",
        "description": "同一天同一台机器的两次实测（数字全部为本机原样输出）：一次 git push 完全推不上去、40 分钟后又能推。核心是三条判据与一次内容寻址对账。①通道判活三态：github.com 网页与 smart-http 的 connect=0.000000、total 21.005s/21.021s、HTTP 000（TCP 根本没建立，重试无意义），同一链路 api.github.com connect=0.0966s/total 0.548s 返回 200、codeload 0.515s、Pages CDN 1.238s；恢复后 smart-http 匿名探测返回 401（=端点可达只要认证，不是失败），git ls-remote 实测 1047ms。②三条通道对比：git push 1 次连接 1 次构建 vs Git Data API 2+N 次调用 1 次构建 vs Contents API 每文件一次提交即 N 次构建；Pages 构建记录实测最近 6 次提交对应 6 次构建，耗时 16.8/17.5/18.8/21.7/26.1/56.7 秒，逐文件推等于把 17 秒乘以文件数并留下半生效窗口。③五步配方（GET ref → GET commit 取 base_tree → POST blobs → POST tree → POST commit → PATCH ref force:false）与三个静默毁结果的坑：漏 base_tree 会用新树替换整棵树、漏 mode 脚本丢可执行位、删除必须显式 sha:null、422 是并发写者信号必须中止不可 force。bench 实测六步 8 次调用 7.40s、单次往返 582–1593ms。④内容寻址对账：推送后 226 个 blob 中 224 个 SHA 相同、2 个不同，不同的两个全是纯 CRLF 文件；根因 core.autocrlf=true 且无 .gitattributes——工作区 21499B/309 CRLF/0 裸LF vs 索引 21190B/0 CRLF/309 LF，delta 恰为 309 行；blob SHA 索引 530dc37 vs 工作区原始字节 ea76284（=远端），本地树 6aaca1ec ≠ 远端树 330aed73。修法：用 git cat-file 取索引字节而非工作区字节，两侧 SHA 才恒等、树 SHA 才可作证据。⑤一次翻车：git add -A 把 33 个文件（7 个一次性脚本、2 个 .pyc、5.1MB 样本目录）塞进一次发布提交，改成点名 9 个路径 + .gitignore。⑥上线验证三件套（新页 200/21499B + 线上 sitemap 6775B + 线上 index 18531B，均需真实 UA 与 ?cb= 破缓存）。⑦工具 tools/publish/gh_tree_push.py：probe/bench/push/verify/selftest 五子命令、纯标准库、20 项自检、退出码三态（0 通过 / 2 对账不一致 / 3 前置条件不满足）。含 12 项上线检查清单与边界声明（不支持 tag/LFS/签名提交/合并）。",
        "keywords": "git push失败,github无法推送,smart-http,info/refs,401,connect超时,HTTP 000,git ls-remote,Git Data API,Git Trees API,base_tree,blob SHA,内容寻址,对账,reconcile,CRLF,core.autocrlf,.gitattributes,git hash-object,cat-file,索引字节,工作区字节,Contents API,Pages 构建次数,构建耗时,force:false,422非快进,并发写者,发布流水线,上线验证,cache buster,git add -A,.gitignore,自动化发布,Agent工程,Python标准库",
    },
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
