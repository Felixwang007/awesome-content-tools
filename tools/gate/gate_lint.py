#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_lint.py — 给「自检 / 门禁类 CLI 工具」本身查体。

两类检查：
  scan   静态：不执行被检脚本，按规则给出候选缺陷（零输入放行、退出码两态、缺自检…）
  probe  动态：用「空目录 + 无参数」真跑一遍被检脚本，看它退什么码、给什么结论
         —— 零范围放行这类缺陷静态无法定论（`%d 个文件` 是格式串还是真的 0，
         文本里看不出来），必须动态确认。probe 会执行被检脚本，默认拒绝运行，
         需要显式 --allow-exec。

用法:
    python gate_lint.py scan <目录或文件...> [--glob *.py] [--json] [--min severity]
    python gate_lint.py probe <包目录> --allow-exec [--timeout 20] [--json]
    python gate_lint.py selftest        # 位置参数约定
    python gate_lint.py --selftest      # 同义，两种约定都支持

退出码（三态，便于上层区分“没跑”和“跑干净”）:
    0  扫到文件且没有 high 级发现 / 探针没抓到放行
    2  存在 high 级发现 / 探针抓到零范围放行 / probe 未确认 --allow-exec
    3  零输入，没扫到任何文件 —— 不算通过
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile

VERSION = "1.2.0"

SEV_ORDER = {"info": 0, "medium": 1, "high": 2}

# ---------------------------------------------------------------- 规则定义
RULES = {
    "G001_NO_SELFTEST": {
        "sev": "high",
        "title": "没有自检入口：工具本身无法被验证",
        "why": "门禁工具坏了会静默放行。没有 selftest 就等于没人知道的假绿灯。",
    },
    "G002_SELFTEST_NO_COUNT": {
        "sev": "medium",
        "title": "自检不打印断言计数：通过=一句话，无法判断覆盖",
        "why": "只输出“全部通过”的自检，无法区分“12/12 通过”和“0 项执行后没报错”。",
    },
    "G003_ZERO_SCOPE_CANDIDATE": {
        "sev": "medium",
        "title": "零范围文案与通过口径同现：疑似 0 个文件 → 满分/可以合并",
        "why": "“没输入”不等于“输入干净”。空集合永远满足全称命题，必被误判为满分。"
               "静态只能给候选（格式串 `%d 个文件` 与真的 0 在文本里长得一样），"
               "定论要靠 probe。",
    },
    "G004_NOARG_SUCCESS": {
        "sev": "high",
        "title": "无参数调用没有非零退出：缺输入走了默认分支",
        "why": "CI 里漏传参数会得到绿灯；缺输入必须是独立的失败态。",
    },
    "G005_TWO_STATE_EXIT": {
        "sev": "medium",
        "title": "退出码只有两态：无法区分“输入错”与“发现问题”",
        "why": "上层要么重试无效输入，要么把配置错误当成真实告警，两种都错。",
    },
    "G006_CONVENTION": {
        "sev": "info",
        "title": "自检入口约定：位置参数 / --flag",
        "why": "两套约定混用会让统一 harness 漏跑一半工具的自检。",
    },
}

# ---------------------------------------------------------------- 检测器
RE_SELFTEST_POS = re.compile(r"add_parser\(\s*['\"]selftest['\"]|"
                             r"argv\[1\]\s*==\s*['\"]selftest['\"]|"
                             r"\bcmd_selftest\b|\bdef\s+selftest\b|"
                             r"['\"]selftest['\"]\s*:")
RE_SELFTEST_FLAG = re.compile(r"--selftest\b|['\"]--selftest['\"]")
RE_RATIO = re.compile(r"\d+\s*/\s*\d+")
RE_SCORE_WORD = re.compile(r"健康分|得分|评分|分数|score|rating|%")
RE_COUNT = re.compile(r"assertions?\b|\d+\s*项(断言|检查|通过)|"
                      r"PASS(ED)?[ _]*\d|通过\s*\d+|失败\s*\d+")
RE_OK_HELPER = re.compile(r"def\s+(ok|check|expect|assert_)\w*\s*\(")
RE_ZERO_SCOPE = re.compile(r"0\s*个文件|0\s*个\s*files|0\s*files\b|"
                           r"(范围|扫描|检查)[:：][^\n]{0,12}\b0\b|"
                           r"\bno\s+files\b|\b0\s+lines?\b|0\s*行")
RE_ZERO_SCOPE_FMT = re.compile(r"%d\s*个文件|%s\s*个文件|\{\}\s*个文件|f['\"][^'\"]*个文件")
RE_PASS_VERDICT = re.compile(r"100\s*/\s*100|✅|可以合并|全部通过|无问题|"
                             r"\bPASS(ED)?\b|通过率\s*100|健康分")
RE_NOARG_GUARD = re.compile(r"if\s+not\s+(args\.\w+|paths?|targets?|files?|argv)\b")
RE_OPTIONAL_POSITIONAL = re.compile(r"nargs\s*=\s*['\"][*?]['\"]")
RE_EXIT_ANY = re.compile(r"sys\.exit\s*\(\s*([0-9]+)\s*\)|raise\s+SystemExit\s*\(\s*([0-9]+)")
RE_EXIT_NON1 = re.compile(r"sys\.exit\s*\(\s*(2|3|[4-9])\s*\)|"
                          r"raise\s+SystemExit\s*\(\s*(2|3|[4-9])")
# 「这是在打印帮助」的迹象：argparse 自己打的 usage，或手写的子命令清单
RE_HELP_TEXT = re.compile(r"(?m)^\s*usage:\s|\bpositional arguments:|"
                          r"命令[:：]\s*$|子命令[:：]|options:\s*$|"
                          r"\bprint_help\s*\(|\bformat_help\s*\(")
# 契约层：这个工具在 SKILL.md 里怎么向外承诺自己的语义
# 注意「门禁」一词在本仓里也出现在「应急预案：…→ 门禁」这类流程描述里，
# 所以只认「门禁/CI/pre-commit」+「文档化退出码」同时出现的契约。
RE_CONTRACT_GATE = re.compile(r"CI\s*门禁|门禁|pre-commit|上线前检查|gate", re.I)
RE_CONTRACT_EXITCODE_DOC = re.compile(r"退出码[^\n]{0,40}`?\s*[0-9]")


def reads_as_gate(text):
    """纯函数：这段契约文本是否声称自己是「门禁」。
    为什么需要：同一个行为（0 个文件 → exit 0）对一个门禁是缺陷，对一个计时分析器
    是正常。判据「算不算缺陷」取决于工具的契约，而契约写在文档里，静态/动态都看不到。"""
    if not text:
        return False
    return bool(RE_CONTRACT_GATE.search(text) and RE_CONTRACT_EXITCODE_DOC.search(text))


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _snippet(text, pos, width=90):
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:width]


def has_assert_count(text):
    """断言计数检测。注意：\"100/100\" 可能是评分而不是断言计数——
    落在健康分/评分语境里的比例一律不算计数。这是本检测器自身的第一条误报来源。"""
    if RE_COUNT.search(text):
        return True
    for m in RE_RATIO.finditer(text):
        window = text[max(0, m.start() - 40):m.end() + 40]
        if not RE_SCORE_WORD.search(window):
            return True
    return False


def analyse_text(text, path="<text>"):
    """纯函数：文本 -> findings 列表。所有规则只读文本，不执行代码。"""
    findings = []

    def add(rule, pos, evidence):
        findings.append({
            "rule": rule,
            "sev": RULES[rule]["sev"],
            "line": _line_of(text, pos),
            "evidence": evidence,
        })

    has_pos = bool(RE_SELFTEST_POS.search(text))
    has_flag = bool(RE_SELFTEST_FLAG.search(text))
    has_selftest = has_pos or has_flag
    is_cli = ("argparse" in text) or ("sys.argv" in text) or ("add_parser" in text)

    if is_cli and not has_selftest:
        m = re.search(r"def\s+main\s*\(|argparse\.ArgumentParser|add_parser", text)
        add("G001_NO_SELFTEST", m.start() if m else 0, _snippet(text, m.start() if m else 0))
    elif has_selftest:
        if not (has_assert_count(text) or RE_OK_HELPER.search(text)):
            m = RE_SELFTEST_POS.search(text) or RE_SELFTEST_FLAG.search(text)
            add("G002_SELFTEST_NO_COUNT", m.start(), _snippet(text, m.start()))

    # G003 零范围文案 + 通过口径同现（候选，需 probe 定论）
    zs = RE_ZERO_SCOPE.search(text) or RE_ZERO_SCOPE_FMT.search(text)
    pv = RE_PASS_VERDICT.search(text)
    if zs and pv:
        guarded = False
        for m in RE_EXIT_NON1.finditer(text):
            window = text[max(0, m.start() - 400):m.end()]
            if RE_ZERO_SCOPE.search(window) or "个文件" in window:
                guarded = True
                break
        if not guarded:
            add("G003_ZERO_SCOPE_CANDIDATE", zs.start(),
                "零范围文案: %s | 通过口径: %s" % (_snippet(text, zs.start()),
                                                _snippet(text, pv.start())))

    # G004 无参数没有非零退出
    if is_cli:
        optional_positional = bool(RE_OPTIONAL_POSITIONAL.search(text))
        guard = bool(RE_NOARG_GUARD.search(text))
        exits = [m.group(1) or m.group(2) for m in RE_EXIT_ANY.finditer(text)]
        nonzero = any(int(e) != 0 for e in exits if e and e.isdigit())
        if optional_positional and not guard and not nonzero:
            m = RE_OPTIONAL_POSITIONAL.search(text)
            add("G004_NOARG_SUCCESS", m.start(), _snippet(text, m.start()))

    # G005 退出码两态
    if is_cli:
        exits = [m.group(1) or m.group(2) for m in RE_EXIT_ANY.finditer(text)]
        codes = sorted({int(e) for e in exits if e and e.isdigit()})
        nonzero_codes = [c for c in codes if c != 0]
        if nonzero_codes and not any(c >= 2 for c in nonzero_codes):
            m = RE_EXIT_ANY.search(text)
            add("G005_TWO_STATE_EXIT", m.start(),
                "非零退出码仅: %s" % ",".join(str(c) for c in nonzero_codes))
        elif not codes and RE_PASS_VERDICT.search(text):
            m = RE_PASS_VERDICT.search(text)
            add("G005_TWO_STATE_EXIT", m.start(), "全文无显式退出码，仅打印结论")

    if has_selftest:
        conv = "both" if (has_pos and has_flag) else ("positional" if has_pos else "flag")
        m = (RE_SELFTEST_POS.search(text) or RE_SELFTEST_FLAG.search(text))
        findings.append({
            "rule": "G006_CONVENTION",
            "sev": "info",
            "line": _line_of(text, m.start()),
            "evidence": "自检入口约定: %s" % conv,
        })

    return findings


def collect_files(targets, glob_pat="*.py", recursive=True):
    files = []
    for t in targets:
        if os.path.isfile(t):
            files.append(t)
        elif os.path.isdir(t):
            if recursive:
                for root, dirs, names in os.walk(t):
                    dirs[:] = [d for d in dirs
                               if d not in ("__pycache__", ".git", ".venv", "node_modules")]
                    for n in names:
                        if fnmatch.fnmatch(n, glob_pat):
                            files.append(os.path.join(root, n))
            else:
                for n in os.listdir(t):
                    if fnmatch.fnmatch(n, glob_pat):
                        files.append(os.path.join(t, n))
    return sorted(set(files))


def scan(targets, glob_pat="*.py", recursive=True, min_sev="info"):
    files = collect_files(targets, glob_pat, recursive)
    results = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            results.append({"file": f, "error": str(e), "findings": [], "verdict": "UNREADABLE"})
            continue
        fs = [x for x in analyse_text(text, f) if SEV_ORDER[x["sev"]] >= SEV_ORDER[min_sev]]
        worst = "info"
        for x in fs:
            if SEV_ORDER[x["sev"]] > SEV_ORDER[worst]:
                worst = x["sev"]
        verdict = "GATE_CLEAN" if not fs else ("GATE_FAIL" if worst == "high" else "GATE_WEAK")
        results.append({"file": f, "findings": fs, "verdict": verdict})
    return files, results


# ---------------------------------------------------------------- 动态探针
def classify_probe(exit_code, output):
    """纯函数：把一次「空输入运行」的结果分类。不执行任何东西，便于断言。
    返回 (verdict, reason)。

    注意这里只产出「观测」，不下「缺陷」结论——同观测对不同契约含义不同，
    是否算缺陷由 reads_as_gate(SKILL.md) 在 probe() 里叠加判定。"""
    out = output or ""
    zero = bool(RE_ZERO_SCOPE.search(out))
    green = bool(RE_PASS_VERDICT.search(out))
    if exit_code == 0 and RE_HELP_TEXT.search(out) and not zero:
        return "HELP_EXIT_ZERO", "退出码 0 且只打印了用法/子命令清单（没干活但不算失败）"
    if exit_code == 0 and zero and green:
        return "ZERO_SCOPE_GREEN", "退出码 0 + 零范围文案 + 通过口径：0 个文件被判合格"
    if exit_code == 0 and zero:
        return "ZERO_SCOPE_GREEN", "退出码 0 + 明确报告扫到 0 个：空范围给了绿灯"
    if exit_code == 0:
        return "GREEN_UNKNOWN_SCOPE", "退出码 0 且输出未报告扫描数量，无法证明扫到了东西"
    if exit_code == 2:
        return "REJECTED", "输入错误被拒（退出码 2）"
    if exit_code == 3:
        return "REJECTED", "零输入被拒（退出码 3）"
    if exit_code == 124:
        return "TIMEOUT", "超时"
    return "REJECTED", "退出码 %d（非 0）" % exit_code


def _run(cmd, cwd, timeout):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as e:
        return 125, str(e)


def probe(pkg_dir, python_exe=None, timeout=20, json_out=False):
    """动态探针。每个脚本跑两次：无参数、空目录。

    观测分两栏：observation（测到的行为）与 defect（叠加契约后的判定）。
    判定规则：只有该包在 SKILL.md 里声称自己是门禁（reads_as_gate）时，
    「空范围给绿灯 / 缺输入给绿灯」才算缺陷；否则只记为观察项。"""
    python_exe = python_exe or sys.executable
    scripts = [f for f in collect_files([pkg_dir])
               if f.replace("\\", "/").count("/scripts/") == 1]

    def contract_of(pkg_path):
        md = os.path.join(pkg_path, "SKILL.md")
        if os.path.isfile(md):
            try:
                return open(md, encoding="utf-8", errors="replace").read()
            except OSError:
                return ""
        return ""

    rows = []
    with tempfile.TemporaryDirectory() as td:
        empty = os.path.join(td, "empty_scope")
        os.makedirs(empty)
        for s in scripts:
            pkg_path = os.path.dirname(os.path.dirname(s))
            pkg = os.path.basename(pkg_path)
            claims_gate = reads_as_gate(contract_of(pkg_path))
            rc_a, out_a = _run([python_exe, s], pkg_path, timeout)
            v_a, why_a = classify_probe(rc_a, out_a)
            rc_b, out_b = _run([python_exe, s, empty], pkg_path, timeout)
            v_b, why_b = classify_probe(rc_b, out_b)
            bad_b = claims_gate and v_b == "ZERO_SCOPE_GREEN"
            bad_a = claims_gate and v_a in ("ZERO_SCOPE_GREEN", "GREEN_UNKNOWN_SCOPE")
            rows.append({
                "package": pkg, "script": os.path.basename(s), "path": s,
                "claims_gate": claims_gate,
                "noarg": {"exit": rc_a, "verdict": v_a, "reason": why_a, "defect": bad_a},
                "emptydir": {"exit": rc_b, "verdict": v_b, "reason": why_b, "defect": bad_b},
            })

    if json_out:
        print(json.dumps({"probed": len(rows), "rows": rows}, ensure_ascii=False, indent=2))
        return rows

    confirmed = [r for r in rows if r["emptydir"]["defect"]]
    obs = [r for r in rows if not r["emptydir"]["defect"]
           and r["emptydir"]["verdict"] == "ZERO_SCOPE_GREEN"]
    noarg_bad = [r for r in rows if r["noarg"]["defect"]]
    noarg_obs = [r for r in rows if not r["noarg"]["defect"]
                 and r["noarg"]["verdict"] in ("ZERO_SCOPE_GREEN", "GREEN_UNKNOWN_SCOPE")
                 and r["noarg"]["verdict"] != "HELP_EXIT_ZERO"]
    help0 = [r for r in rows if r["noarg"]["verdict"] == "HELP_EXIT_ZERO"]
    gates = [r for r in rows if r["claims_gate"]]

    # 台账自检：每个观测必须恰好落进一个桶，不许有行悄悄消失
    covered_empty = (len(confirmed) + len(obs)
                     + sum(1 for r in rows if r["emptydir"]["verdict"] == "REJECTED")
                     + sum(1 for r in rows if r["emptydir"]["verdict"] == "GREEN_UNKNOWN_SCOPE")
                     + sum(1 for r in rows if r["emptydir"]["verdict"] == "TIMEOUT"))
    covered_noarg = (len(noarg_bad) + len(noarg_obs) + len(help0)
                     + sum(1 for r in rows if r["noarg"]["verdict"] == "REJECTED")
                     + sum(1 for r in rows if r["noarg"]["verdict"] == "TIMEOUT"))

    print("- 探针执行: %d 个脚本（空目录 + 无参数各一次）" % len(rows))
    print("- 契约自查: 声称自己是门禁的包 %d 个" % len(set(r["package"] for r in gates)))
    print("- 已确认缺陷（空目录仍给绿灯 且 契约声称是门禁）: %d 个" % len(confirmed))
    print("- 观察项（同样行为，但契约没声称是门禁，不当缺陷）: %d 个" % len(obs))
    print("- 无参数即绿灯（契约声称是门禁）: %d 个" % len(noarg_bad))
    print("- 无参数即绿灯（契约未声称门禁，观察项）: %d 个" % len(noarg_obs))
    print("- 无参数只打用法退 0（轻微，不计缺陷）: %d 个" % len(help0))
    print("- 空目录输入被拒（exit≠0）: %d 个"
          % sum(1 for r in rows if r["emptydir"]["verdict"] == "REJECTED"))
    print("- 无参数输入被拒（exit≠0）: %d 个"
          % sum(1 for r in rows if r["noarg"]["verdict"] == "REJECTED"))
    print("- 台账核对: 空目录 %d/%d，无参数 %d/%d（不等就是有观测被吞了）"
          % (covered_empty, len(rows), covered_noarg, len(rows)))
    if confirmed:
        print("")
        print("### 已确认缺陷")
        for r in confirmed:
            print("- %s / %s  exit=%d  %s"
                  % (r["package"], r["script"], r["emptydir"]["exit"],
                     r["emptydir"]["reason"]))
    if noarg_bad:
        print("")
        print("### 无参数即绿灯")
        for r in noarg_bad:
            print("- %s / %s  exit=%d  %s"
                  % (r["package"], r["script"], r["noarg"]["exit"], r["noarg"]["reason"]))
    if obs:
        print("")
        print("### 观察项（空目录，契约未声称门禁，仅记录）")
        for r in obs:
            print("- %s / %s  exit=%d  %s"
                  % (r["package"], r["script"], r["emptydir"]["exit"],
                     r["emptydir"]["reason"]))
    if noarg_obs:
        print("")
        print("### 观察项（无参数，契约未声称门禁，仅记录）")
        for r in noarg_obs:
            print("- %s / %s  exit=%d  %s"
                  % (r["package"], r["script"], r["noarg"]["exit"], r["noarg"]["reason"]))
    if help0:
        print("")
        print("### 无参数只打印用法且退 0（轻微）")
        for r in help0:
            print("- %s / %s" % (r["package"], r["script"]))
    return rows


# ---------------------------------------------------------------- 渲染
def render(results, total_files):
    fails = [r for r in results if r["verdict"] == "GATE_FAIL"]
    weaks = [r for r in results if r["verdict"] == "GATE_WEAK"]
    clean = [r for r in results if r["verdict"] == "GATE_CLEAN"]
    counts = {}
    for r in results:
        for x in r["findings"]:
            counts[x["rule"]] = counts.get(x["rule"], 0) + 1

    out = []
    out.append("- 扫描文件: %d 个" % total_files)
    if total_files == 0:
        out.append("- 结论: 没有扫到任何文件（零输入 ≠ 通过），退出码 3")
        return "\n".join(out)
    out.append("- 判定: FAIL %d / WEAK %d / CLEAN %d" % (len(fails), len(weaks), len(clean)))
    out.append("- 命中规则: " + (", ".join("%s×%d" % (k, counts[k]) for k in sorted(counts))
                              or "无"))
    if fails:
        out.append("")
        out.append("### GATE_FAIL（作为门禁会放行不该放行的东西）")
        for r in fails:
            out.append("- `%s`" % r["file"])
            for x in r["findings"]:
                if x["sev"] == "high":
                    out.append("  - [%s] L%s %s" % (x["rule"], x["line"], x["evidence"]))
    if weaks:
        out.append("")
        out.append("### GATE_WEAK")
        for r in weaks:
            out.append("- `%s`" % r["file"])
            for x in r["findings"]:
                if x["sev"] == "medium":
                    out.append("  - [%s] L%s %s" % (x["rule"], x["line"], x["evidence"]))
    return "\n".join(out)


# ---------------------------------------------------------------- selftest
SAMPLE_BAD = '''#!/usr/bin/env python3
import argparse, sys
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    files = collect(args.targets)
    print("- 扫描范围: %d 个文件" % len(files))
    print("- 健康分: 100/100 - 可以合并")
    return 0
if __name__ == "__main__":
    sys.exit(main())
'''

SAMPLE_GOOD = '''#!/usr/bin/env python3
import argparse, sys
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if not args.targets:
        print("no input", file=sys.stderr)
        sys.exit(2)
    files = collect(args.targets)
    if not files:
        print("0 files scanned", file=sys.stderr)
        sys.exit(3)
    return 0 if clean else 2
if __name__ == "__main__":
    sys.exit(main())
'''


def selftest():
    passed = 0
    failed = []

    def ok(name, cond, extra=""):
        nonlocal passed
        if cond:
            passed += 1
            print("  ok   %s" % name)
        else:
            failed.append(name)
            print("  FAIL %s %s" % (name, extra))

    print("[1] 规则 G001：无自检入口")
    fs = analyse_text(SAMPLE_BAD)
    ok("坏样本命中 G001", any(x["rule"] == "G001_NO_SELFTEST" for x in fs))
    ok("G001 为 high", all(x["sev"] == "high" for x in fs if x["rule"] == "G001_NO_SELFTEST"))
    fs2 = analyse_text(SAMPLE_GOOD)
    ok("好样本不命中 G001", not any(x["rule"] == "G001_NO_SELFTEST" for x in fs2))

    print("[2] 规则 G003：零范围候选（medium，需 probe 定论）")
    ok("坏样本命中 G003", any(x["rule"] == "G003_ZERO_SCOPE_CANDIDATE" for x in fs))
    ok("G003 为 medium 而非 high",
       all(x["sev"] == "medium" for x in fs if x["rule"].startswith("G003")))
    ok("好样本不命中 G003", not any(x["rule"].startswith("G003") for x in fs2))
    ok("format 串也进候选（静态无法区分）",
       any(x["rule"].startswith("G003")
           for x in analyse_text('print("%d 个文件" % n)\nprint("健康分: 100/100")')))

    print("[3] 规则 G004/G005：缺输入与退出码两态")
    ok("坏样本命中 G004", any(x["rule"] == "G004_NOARG_SUCCESS" for x in fs))
    ok("好样本不命中 G004", not any(x["rule"] == "G004_NOARG_SUCCESS" for x in fs2))
    ok("坏样本命中 G005", any(x["rule"] == "G005_TWO_STATE_EXIT" for x in fs))
    ok("好样本不命中 G005", not any(x["rule"] == "G005_TWO_STATE_EXIT" for x in fs2))

    print("[4] 规则 G002：自检不打印计数")
    no_count = SAMPLE_BAD.replace("--json", "--selftest")
    fs3 = analyse_text(no_count)
    ok("有 --selftest 但无计数 → G002", any(x["rule"] == "G002_SELFTEST_NO_COUNT" for x in fs3))
    withcount = SAMPLE_GOOD.replace('print("no input"', 'print("11/11 ok"); print("no input"')
    fs4 = analyse_text(withcount)
    ok("带 n/m 计数 → 不报 G002", not any(x["rule"] == "G002_SELFTEST_NO_COUNT" for x in fs4))
    ok("评分语境 100/100 不算断言计数", has_assert_count('print("健康分: 100/100")') is False)
    ok("真实计数 12/12 被识别", has_assert_count('print("12/12 通过")') is True)

    print("[5] 规则 G006：约定识别")
    ok("SAMPLE_GOOD 识别为 flag 约定",
       any(x["rule"] == "G006_CONVENTION" and "flag" in x["evidence"] for x in fs2))
    ok("SAMPLE_BAD 无约定项", not any(x["rule"] == "G006_CONVENTION" for x in fs))

    print("[6] 动态探针分类（纯函数，不执行任何东西）")
    v, _ = classify_probe(0, "- 审查范围: 0 个文件\n- 健康分: 100/100 — ✅ 可以合并")
    ok("0 + 零范围 + 通过口径 → ZERO_SCOPE_GREEN", v == "ZERO_SCOPE_GREEN", v)
    v, _ = classify_probe(0, "扫描 0 个文件，未发现问题")
    ok("0 + 报告零范围 → ZERO_SCOPE_GREEN", v == "ZERO_SCOPE_GREEN", v)
    v, _ = classify_probe(2, "输入错误: 路径不存在")
    ok("退出码 2 → REJECTED", v == "REJECTED", v)
    v, _ = classify_probe(3, "")
    ok("退出码 3 → REJECTED", v == "REJECTED", v)
    v, _ = classify_probe(0, "已检查 12 个文件，全部通过")
    ok("0 + 有范围无零值 → GREEN_UNKNOWN_SCOPE", v == "GREEN_UNKNOWN_SCOPE", v)
    v, _ = classify_probe(124, "")
    ok("超时 → TIMEOUT", v == "TIMEOUT", v)
    v, _ = classify_probe(125, "can't open file")
    ok("无法执行 → REJECTED", v == "REJECTED", v)

    print("[7] 边界")
    ok("空文本不抛异常", analyse_text("") == [])
    ok("非 CLI 文本不误报 G001", not any(x["rule"] == "G001_NO_SELFTEST"
                                     for x in analyse_text("x = 1\ny = x + 1\n")))
    ok("CRLF 文本行号正确",
       any(x["rule"] == "G001_NO_SELFTEST" and x["line"] > 0
           for x in analyse_text(SAMPLE_BAD.replace("\n", "\r\n"))))
    ok("零文件时 render 说明零输入≠通过", "零输入" in render([], 0))

    print("[8] 帮助文本 vs 零范围：别把打印用法当成跑过")
    v, _ = classify_probe(0, "usage: tool.py [-h] {msg,lint}\n\npositional arguments:\n")
    ok("只有 usage 的退 0 → HELP_EXIT_ZERO", v == "HELP_EXIT_ZERO", v)
    v, _ = classify_probe(0, "usage: tool.py\n\n- 审查范围: 0 个文件\n- 健康分: 100/100")
    ok("usage + 零范围报告 → 仍算 ZERO_SCOPE_GREEN", v == "ZERO_SCOPE_GREEN", v)

    print("[9] 契约层：算不算缺陷取决于工具声称自己是什么")
    gate_md = ("## 用法\n退出码：`0` 无致命问题，`2` 存在致命问题，可直接拿来做 CI 门禁。\n")
    prof_md = ("scripts/profile_scan.py  性能回归：定位哪个文件 / 规则慢\n")
    ok("声称门禁的契约 → reads_as_gate True", reads_as_gate(gate_md) is True)
    ok("没声称门禁的契约 → reads_as_gate False", reads_as_gate(prof_md) is False)
    ok("空契约 / 缺失契约 → False", reads_as_gate("") is False)
    ok("提了退出码但没提门禁语义 → False",
       reads_as_gate("退出码：0 成功，2 失败\n") is False)

    print("[10] 自身必须是干净门禁")
    here = os.path.abspath(__file__)
    fs5 = analyse_text(open(here, encoding="utf-8").read(), here)
    ok("自身不命中 G003", not any(x["rule"].startswith("G003") for x in fs5))
    ok("自身不命中 G004", not any(x["rule"] == "G004_NOARG_SUCCESS" for x in fs5))
    ok("自身不命中 G005", not any(x["rule"] == "G005_TWO_STATE_EXIT" for x in fs5))
    ok("自身有自检入口", any(x["rule"] == "G006_CONVENTION" for x in fs5))

    total = passed + len(failed)
    print("\n%d/%d 通过" % (passed, total))
    return 0 if not failed else 2


# ---------------------------------------------------------------- main
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 两套约定都支持：gate_lint.py selftest / gate_lint.py --selftest
    if argv and argv[0] in ("selftest", "--selftest"):
        return selftest()

    ap = argparse.ArgumentParser(
        description="给门禁/自检类 CLI 工具查体（静态 scan + 动态 probe）")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="静态扫描（不执行被检脚本）")
    p_scan.add_argument("targets", nargs="+", help="目录或 .py 文件")
    p_scan.add_argument("--glob", default="*.py")
    p_scan.add_argument("--min", default="info", choices=["info", "medium", "high"])
    p_scan.add_argument("--json", action="store_true")

    p_probe = sub.add_parser("probe", help="动态探针：空目录+无参数真跑一遍（会执行被检脚本）")
    p_probe.add_argument("pkg_dir")
    p_probe.add_argument("--allow-exec", action="store_true",
                         help="确认允许执行被检脚本（不加则拒绝运行）")
    p_probe.add_argument("--python", default=None, help="解释器路径，默认当前解释器")
    p_probe.add_argument("--timeout", type=int, default=20)
    p_probe.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.cmd == "probe":
        if not args.allow_exec:
            print("probe 会执行被检脚本，需显式加 --allow-exec 才运行", file=sys.stderr)
            return 2
        rows = probe(args.pkg_dir, args.python, args.timeout, args.json)
        if not rows:
            print("没有扫到任何脚本：%s" % args.pkg_dir, file=sys.stderr)
            return 3
        bad = [r for r in rows if r["emptydir"]["defect"] or r["noarg"]["defect"]]
        return 2 if bad else 0

    if args.cmd == "scan":
        files, results = scan(args.targets, args.glob, min_sev=args.min)
        if not files:
            print("没有扫到任何文件：targets=%s glob=%s" % (args.targets, args.glob),
                  file=sys.stderr)
            print("（零输入不是「输入干净」，本次不计为通过）", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps({
                "total_files": len(files), "results": results,
                "verdict_counts": {
                    "GATE_FAIL": sum(1 for r in results if r["verdict"] == "GATE_FAIL"),
                    "GATE_WEAK": sum(1 for r in results if r["verdict"] == "GATE_WEAK"),
                    "GATE_CLEAN": sum(1 for r in results if r["verdict"] == "GATE_CLEAN"),
                },
            }, ensure_ascii=False, indent=2))
        else:
            print(render(results, len(files)))
        return 2 if any(r["verdict"] == "GATE_FAIL" for r in results) else 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
