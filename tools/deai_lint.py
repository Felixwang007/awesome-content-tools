#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deai_lint.py — 中文文本「AI味」确定性检测器（离线 / 零依赖 / 退出码即结论）

用法:
  python deai_lint.py <file> [<file> ...]     体检并打印报告
  python deai_lint.py --json <file>           输出机器可读 JSON
  python deai_lint.py selftest                自检（自带样例 + 断言 + 退出码）
  python deai_lint.py --threshold 80 <file>   自定义及格线（默认 75）

退出码:
  0 = 全部文件通过（AI味分数 >= 及格线）
  1 = 至少一个文件不通过（需要改写）
  2 = 用法错误 / 文件不存在

设计原则:
  1) 只做确定性判断，不调用任何模型 —— 同一份文本永远得到同一个分数。
  2) 词表命中只是「嫌疑」，真正区分人写与生成的是统计量（句长变异系数、
     数字密度、段落离散度）。所以报告同时给两类读数。
  3) 分数不是审美判决，是「返工提示」：分数低 = 有具体行号可改。
"""
import json
import os
import re
import statistics
import sys

__version__ = "0.4.0"

# ---------------------------------------------------------------- 词表

LEX = {
    "空转连接词": (
        ["首先", "其次", "再次", "最后", "值得注意的是", "值得一提的是", "不难发现",
         "不难看出", "众所周知", "综上所述", "总而言之", "总的来说", "换句话说",
         "与此同时", "毋庸置疑", "需要指出的是", "需要注意的是", "事实上", "实际上",
         "显而易见", "简而言之", "一言以蔽之", "总而言之", "由此可见"],
        4, "直接说事，删掉过渡词；句与句之间靠内容衔接，不靠连接词"),
    "升华口号": (
        ["让我们一起", "未来可期", "相信在不久的将来", "拭目以待", "愿你", "共赴",
         "向阳而生", "未来已来", "开启新篇章", "书写新篇章", "赋能", "助力", "焕新",
         "点亮", "绽放", "共振", "破局", "深耕", "打磨", "闭环", "抓手", "底层逻辑",
         "新范式", "新高度", "新高地", "新的里程碑", "澎湃", "迸发"],
        6, "删掉结尾升华；用一句具体的事、一个数字或一句原话收尾"),
    "空洞形容": (
        ["极大地", "显著提升", "全面提升", "完美地", "充分体现", "深刻诠释",
         "生动展现", "淋漓尽致", "令人印象深刻", "丰富多彩", "意义重大", "影响深远"],
        5, "把形容词换成可核对的读数（从 X 到 Y、几次、几分钟、多少钱）"),
    "无源数据": (
        ["研究表明", "数据显示", "据统计", "调查发现", "专家表示", "业内人士指出",
         "有研究显示", "分析人士认为"],
        7, "补出处或改成你自己跑出来的数（本机实测/官方通报/文件名+页码）"),
    "元话语说教": (
        ["你有没有想过", "你是否", "不知道你有没有", "相信很多人", "大家都知道",
         "我们不难发现", "其实很多人不知道", "说到这里"],
        5, "别对读者喊话；把结论放进故事、场景或数据里，让读者自己得出"),
    "直呼读者": (
        ["你的存款", "你的房子", "你的钱", "你的生活", "你自己"], 3,
        "少用「你的 X」式直呼，用画面/数字让读者代入"),
}

# 句式模式（正则，flags 命中即计数）
PATTERNS = [
    ("不是…而是…", re.compile(r"不是[^，。；\n]{2,18}而是"), 3,
     "同一篇里最多用 2 次，第 3 次起是模板腔"),
    ("既…又…还…", re.compile(r"既[^，。；\n]{2,12}[，、]?又[^，。；\n]{2,12}[，、]?还"), 5,
     "三项对偶是生成器最爱的对称结构，改成长短句混排"),
    ("三项排比（长词组顿号三连）", re.compile(r"[^，。；：、\n]{5,14}、[^，。；：、\n]{5,14}、[^，。；：、\n]{5,14}、"), 4,
     "三连排比密度过高；留一项，剩下两项用不同句式展开（短词列举如地名/项名不计入，见下方计数）"),
    ("三段式收尾", re.compile(r"不仅[^，。\n]{2,20}而且[^，。\n]{2,20}更"), 5,
     "递进三连是典型生成句，删掉一层"),
    ("口号式感叹", re.compile(r"[！!]{1,}"), 2,
     "段落末尾连续感叹号是空洞信号，用陈述句收"),
]

# 数字/实体密度（人味信号，越高越好）
CONCRETE = re.compile(r"(\d+(\.\d+)?%?|\d{4}年|\d+月\d+日|[一二三四五六七八九十百千万]+元|"
                      r"[0-9]+[秒分时天周月年人个次条篇万])")

# 短词顿号列举（地名/项名罗列）——**不扣分**，只作为对照读数：
# 官方通报、天气预报、清单类文本天然大量出现，若把它当"排比"会把正常人写文本误判。
ENUM_SHORT = re.compile(r"(?:[^，。；：、\n]{1,4}、){3,}")


def _sentences(text):
    return [s.strip() for s in re.split(r"[。！？；\n]+", text) if s.strip()]


def _paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def mask_examples(text):
    """遮掉"引用的样板词、表格、代码块"，返回（遮罩后文本, 遮掉片段数）。

    为什么必须做这一步：讲"AI味"的文章本身就要把「首先/其次/最后」「让我们一起」
    这些词当例子列出来。如果检测器把这些示例当成作者自己的口头禅，就会出现
    "越认真讲清楚这件事的文章，分数越低"的荒谬结果（本工具 v0.3 实测：一篇
    讲去AI味的文章被自己判了 0 分，命中全部来自示例和表格）。
    语义上也更对：引用别人的话、代码块里的字符串、表格里的对照项，不是作者的自述。
    """
    lines, out, in_fence, masked = text.splitlines(), [], False, 0

    def blank(m):
        nonlocal masked
        masked += 1
        return " " * len(m.group(0))

    for ln in lines:
        s = ln
        if s.lstrip().startswith("```"):
            in_fence = not in_fence
            s = " " * len(ln)
        elif in_fence or s.lstrip().startswith("|"):
            s = " " * len(ln)
        else:
            s = re.sub(r"`[^`]*`", blank, s)
            s = re.sub(r"「[^」\n]{0,60}」", blank, s)
            s = re.sub(r"“[^”\n]{0,60}”", blank, s)
            s = re.sub(r"\"[^\"\n]{0,60}\"", blank, s)
        out.append(s)
    return "\n".join(out), masked


def analyze(text):
    """返回 dict：metrics（统计量）+ hits（命中清单）+ score。"""
    text, masked = mask_examples(text)
    n_chars = len(re.sub(r"\s", "", text)) or 1
    per_1k = lambda n: round(n * 1000.0 / n_chars, 2)

    # ---- 统计量
    sents = _sentences(text)
    lens = [len(s) for s in sents]
    mean = round(statistics.fmean(lens), 1) if lens else 0
    sd = round(statistics.pstdev(lens), 1) if len(lens) > 1 else 0
    cv = round(sd / mean, 3) if mean else 0          # 句长变异系数：人写通常 >0.5
    paras = _paragraphs(text)
    plen = [len(p) for p in paras]
    p_cv = round(statistics.pstdev(plen) / statistics.fmean(plen), 3) if len(plen) > 1 else 0

    # 句首重复（同一起手式出现 ≥3 次）——先剥掉 Markdown 记号，否则标题/清单会被当成"起手式重复"
    MD_MARK = re.compile(r"^(?:\s*#{1,6}\s+|>\s*|[-*•]\s+|\d+[\.\)]\s+|\[\s?\]\s*|\*\*|__|--\s*)+")
    heads = []
    for s in sents:
        t = MD_MARK.sub("", s)
        if len(t) > 3:
            heads.append(t[:2])
    head_dup = max((heads.count(h) for h in set(heads)), default=0) if heads else 0

    concrete = len(CONCRETE.findall(text))
    digits = per_1k(concrete)

    # ---- 命中
    hits = []
    penalty = 0.0
    for label, (words, weight, advice) in LEX.items():
        for w in words:
            cnt = text.count(w)
            if not cnt:
                continue
            lines = [i + 1 for i, ln in enumerate(text.splitlines()) if w in ln]
            p = weight * min(cnt, 6)
            penalty += p
            hits.append({"type": label, "pattern": w, "count": cnt, "penalty": p,
                         "lines": lines[:8], "advice": advice})
    for label, rx, weight, advice in PATTERNS:
        found = rx.findall(text) if rx.groups == 0 else [m.group(0) for m in rx.finditer(text)]
        cnt = len(found)
        if not cnt:
            continue
        p = weight * min(cnt, 6)
        penalty += p
        hits.append({"type": "句式模板", "pattern": label, "count": cnt, "penalty": p,
                     "lines": [i + 1 for i, ln in enumerate(text.splitlines()) if rx.search(ln)][:8],
                     "advice": advice})
    if head_dup >= 3:
        p = 4 * min(head_dup - 2, 4)
        penalty += p
        hits.append({"type": "句式模板", "pattern": f"句首重复（同一开头 {head_dup} 次）",
                     "count": head_dup, "penalty": p, "lines": [], "advice": "换起手式；把其中几句并进上一句"})

    # 破折号按**密度**判（每千字 > 3 才算解释癖），单次使用不扣分：
    # 少量破折号是正常中文标点，逐次扣分会让"正常写法"被误伤。
    dash_1k = per_1k(text.count("——"))
    if dash_1k > 3:
        p = 3 * min(int(dash_1k / 3), 6)
        penalty += p
        hits.append({"type": "句式模板", "pattern": f"破折号密度 {dash_1k}/千字", "count": int(dash_1k),
                     "penalty": p, "lines": [i + 1 for i, ln in enumerate(text.splitlines()) if "——" in ln][:8],
                     "advice": "每千字超过 3 个就该改成句号或括号"})

    # ---- 统计量惩罚 / 加分
    metrics_notes = []
    if cv and cv < 0.30:
        penalty += 10
        metrics_notes.append(f"句长变异系数 {cv} < 0.30：句子长短几乎没有起伏，读起来是打点机节奏")
    elif cv and cv < 0.45:
        metrics_notes.append(f"句长变异系数 {cv} 偏均匀（仅作参考读数，未扣分——实测专业记者稿也在 0.33 附近）")
    elif cv >= 0.7:
        penalty -= 6
        metrics_notes.append(f"句长变异系数 {cv} ≥ 0.7：长短句混排，像人写的")
    if p_cv and p_cv < 0.3:
        penalty += 8
        metrics_notes.append(f"段落长度离散度 {p_cv} < 0.3：每段都一样长，读起来像清单不像文章")
    if digits < 3:
        penalty += 10
        metrics_notes.append(f"具体数字密度 {digits}/千字 < 3：通篇没有可核对的事实，是空泛的主要来源")
    elif digits >= 12:
        penalty -= 8
        metrics_notes.append(f"具体数字密度 {digits}/千字 ≥ 12：细节充足")

    score = int(max(0, min(100, round(100 - penalty))))
    return {
        "chars": n_chars,
        "sentences": len(sents),
        "metrics": {"句均长": mean, "句长标准差": sd, "句长变异系数": cv,
                    "段落数": len(paras), "段落离散度": p_cv,
                    "句首最高重复次数": head_dup, "具体数字个数": concrete,
                    "数字密度/千字": digits,
                    "短词列举/千字": per_1k(len(ENUM_SHORT.findall(text))),
                    "示例/引用片段数": masked,
                    "破折号/千字": per_1k(text.count("——")),
                    "感叹号/千字": per_1k(text.count("！") + text.count("!"))},
        "hits": sorted(hits, key=lambda h: -h["penalty"]),
        "metrics_notes": metrics_notes,
        "score": score,
    }


def gate_results(r, threshold):
    """硬门槛：四项独立判据，不许互相补偿（数字密度再高也不能抵消模板词和空泛结尾）。

    为什么没有把"句长变异系数"放进门槛：本次 4 份真实样本（2 份模型输出、2 份职业记者稿）
    实测人写稿的 CV 落在 0.34~0.65、模型稿落在 0.27~0.42，两段区间重叠——它筛不出东西，
    所以只作为参考读数保留。阈值标定需要上百份样本，这是本工具的已知边界。
    """
    m = r["metrics"]
    types = {h["type"] for h in r["hits"]}
    return [
        (f"总分 ≥ {threshold}（当前 {r['score']}）", r["score"] >= threshold),
        (f"具体数字密度 ≥ 3/千字（当前 {m['数字密度/千字']}）", m["数字密度/千字"] >= 3),
        ("无「升华口号」命中", "升华口号" not in types),
        ("无「无源数据」命中", "无源数据" not in types),
    ]


def report(name, r, threshold):
    g = gate_results(r, threshold)
    verdict = "PASS" if all(ok for _, ok in g) else "REWRITE"
    out = [f"=== {name} · AI味分数 {r['score']}/100 · {verdict}（及格线 {threshold}，deai_lint v{__version__}）",
           f"字数 {r['chars']} · 句子 {r['sentences']} · 句均长 {r['metrics']['句均长']} · "
           f"变异系数 {r['metrics']['句长变异系数']} · 数字密度 {r['metrics']['数字密度/千字']}/千字 · "
           f"短词列举 {r['metrics']['短词列举/千字']}/千字"]
    out.append("-- 硬门槛（不可互相补偿，任一不过即返工）--")
    out += [f"  {'PASS' if ok else 'FAIL'} {label}" for label, ok in g]
    if r["metrics_notes"]:
        out.append("-- 统计量提示 --")
        out += [f"  · {n}" for n in r["metrics_notes"]]
    if r["hits"]:
        out.append("-- 词表/句式命中（按扣分排序）--")
        for h in r["hits"][:12]:
            loc = f" 行{','.join(map(str, h['lines']))}" if h["lines"] else ""
            out.append(f"  [{h['type']}] 「{h['pattern']}」×{h['count']}（-{h['penalty']}）{loc}")
            out.append(f"      → {h['advice']}")
    else:
        out.append("-- 无词表命中 --")
    return "\n".join(out)


def selftest():
    ai = (
        "在当今这个快速发展的时代，提升个人效率已经成为每个人都必须面对的重要课题。\n\n"
        "首先，我们需要明确目标。明确的目标是我们前进的方向，也是我们不断成长的动力。"
        "其次，我们需要合理规划时间。合理的时间规划能够帮助我们更好地平衡工作与生活。"
        "最后，我们需要持续复盘。持续的复盘能够让我们不断优化方法，实现自我提升。\n\n"
        "值得注意的是，效率的提升不是一蹴而就的，而是需要长期坚持的过程。"
        "研究表明，养成良好的习惯能够显著提升我们的工作效率。\n\n"
        "总而言之，提升效率既需要方法，又需要坚持，更需要良好的心态。"
        "让我们一起行动起来，未来可期！"
    )
    human = (
        "腊月二十九下午，我在阳台上擦玻璃。抹布拧干第三遍，水还是黑的。\n\n"
        "楼下修车摊的老周喊我，说他那儿有块旧毛巾，吸水。我下去拿，顺手看了他那台气泵——"
        "压力表针抖得厉害，停在 6 公斤上不去。他说坏了半个月了，凑合用，生意少三成。\n\n"
        "回到楼上，玻璃是干净了。可我站在那儿看了很久，看的是老周那块表。"
        "有些东西坏得并不响，就是慢慢不够用了。"
    )
    ra, rh = analyze(ai), analyze(human)
    # 回归：短词顿号列举（地名/项名）不得触发"三项排比"扣分
    enum = "昨日，重庆、湖北、安徽、江苏、浙江等地出现大到暴雨。未来三天，华西地区至江汉、江淮等地有持续降雨。"
    re_enum = analyze(enum)
    # 回归：数字堆出来的"假通过"必须被节奏门槛拦住
    dense = ("3月12日早上7点，他在仓库清点出42箱货，用了26分钟。\n\n"
             "3月13日早上7点，他照样清点出42箱货，用时25分钟。\n\n"
             "3月14日晚上8点，他清点完42箱货，总共用了27分钟。")
    rd = analyze(dense)
    checks = [
        ("AI 样例命中空转连接词", any(h["pattern"] == "首先" for h in ra["hits"])),
        ("AI 样例命中升华口号", any(h["type"] == "升华口号" for h in ra["hits"])),
        ("AI 样例命中无源数据", any(h["pattern"] == "研究表明" for h in ra["hits"])),
        ("AI 样例分数低于人写样例", ra["score"] < rh["score"]),
        ("AI 样例低于及格线", ra["score"] < 75),
        ("人写样例通过及格线", rh["score"] >= 75),
        ("人写样例无口号命中", not any(h["type"] == "升华口号" for h in rh["hits"])),
        ("短词地名列举不触发排比扣分",
         not any(h["pattern"].startswith("三项排比") for h in re_enum["hits"])),
        ("短词地名列举被计入对照读数", re_enum["metrics"]["短词列举/千字"] > 0),
        ("数字堆砌能拿高总分", rd["score"] >= 75),
        ("节奏指标不作为硬门槛（样本上判不出人机差异）",
         not any("变异系数" in lb for lb, _ in gate_results(ra, 75))),
        ("人写样例通过全部硬门槛", all(ok for _, ok in gate_results(rh, 75))),
        ("AI 样例未通过硬门槛", not all(ok for _, ok in gate_results(ra, 75))),
    ]
    print(f"AI 样例分数 {ra['score']} · 人写样例分数 {rh['score']}")
    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
        ok = ok and passed
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    threshold = 75
    files, as_json = [], False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "selftest":
            return selftest()
        if a == "--json":
            as_json = True
        elif a == "--threshold":
            i += 1
            threshold = int(argv[i])
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            files.append(a)
        i += 1
    if not files:
        print(__doc__)
        return 2
    results, worst = {}, 0
    for f in files:
        if not os.path.exists(f):
            print(f"文件不存在: {f}", file=sys.stderr)
            return 2
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        r = analyze(text)
        results[f] = r
        if not all(ok for _, ok in gate_results(r, threshold)):
            worst = 1
        if not as_json:
            print(report(f, r, threshold))
            print()
    if as_json:
        print(json.dumps({"threshold": threshold, "results": results},
                         ensure_ascii=False, indent=2))
    if len(files) > 1 and not as_json:
        print("-- 汇总（硬门槛为准）--")
        for f, r in results.items():
            g = gate_results(r, threshold)
            print(f"  {r['score']:>3}  {'PASS   ' if all(o for _, o in g) else 'REWRITE'} {f}")
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
