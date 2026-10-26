#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trunc_probe — 判断「我拿到的是全集，还是前缀，还是根本不知道」的三态探测器。

纯标准库，零依赖。用法：

    python trunc_probe.py judge samples/algolia_hits_cap.json
    python trunc_probe.py judge snap.json --json
    python trunc_probe.py selftest

退出码就是结论（可直接当门禁用）：

    0  COMPLETE   有正面的完整性证据（声明总量==实得，或明确 has_more=false / 结束标记）
    2  TRUNCATED  有截断证据（显式截断标记 / 声明总量>实得 / rel=next / 200+半截正文 / 范围没覆盖完）
    3  UNKNOWN    既不完整也不可证伪——「没有截断标记」被当成全集，是这条流水线最常见的假绿灯
    4  用法错误 / 零输入（「没输入」不是「输入干净」）
    5  自检失败

设计原则（这一条比任何规则都重要）：
    COMPLETE 需要正面证据，不需要「没看到坏消息」。
"""

import json
import os
import sys

EXIT_COMPLETE = 0
EXIT_TRUNCATED = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 4
EXIT_SELFTEST_FAIL = 5

# 常见的「服务端一次最多给这些」数量。命中这些值 + 请求要得更多 + 无任何总量字段
# => 疑似静默封顶。注意：这只是怀疑，不是结论，所以降级为 UNKNOWN 而不是 TRUNCATED。
KNOWN_CAPS = {10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000}

# 显式截断措辞。多语言，宁宽勿窄：漏报比误报贵。
TRUNC_MARKERS = (
    "truncated", "truncat", "cut off", "cut short", "more expected",
    "showing first", "showing lines", "showing 1-", "(showing",
    "已截断", "截断", "省略", "仅显示", "只显示", "输出过长", "超出限制",
    "内容过长", "not shown", "output limit", "max_tokens",
)


def _d(obj, *keys, default=None):
    """安全取嵌套字段。"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def judge(snap):
    """对一份快照做三态判定。

    返回 dict: {"verdict": "COMPLETE|TRUNCATED|UNKNOWN", "findings": [...], "signals": {...}}
    每条 finding: {"rule": "TR00x", "level": "TRUNCATED|UNKNOWN", "evidence": "..."}
    """
    if not isinstance(snap, dict):
        raise ValueError("snapshot 必须是 JSON object")

    req = snap.get("request") or {}
    res = snap.get("response") or {}
    hdr = res.get("headers") or {}
    kind = snap.get("kind") or "list"

    items = _num(res.get("items"))
    total = _num(res.get("total"))
    has_more = res.get("has_more")          # True / False / None
    next_offset = res.get("next_offset")
    status = _num(res.get("status"))
    parse = res.get("parse")                # "ok" / "fail" / None
    bytes_read = _num(res.get("bytes_read"))
    bytes_exp = _num(res.get("bytes_expected"))
    lines_read = _num(res.get("lines_read"))
    lines_total = _num(res.get("lines_total"))
    end_marker = res.get("end_marker")
    first_index = _num(res.get("first_index"))
    tail = res.get("text_tail") or ""
    link = (hdr.get("link") or hdr.get("Link") or "")
    req_limit = _num(req.get("limit"))
    req_offset = _num(req.get("offset")) or 0

    findings = []

    def add(rule, level, evidence):
        findings.append({"rule": rule, "level": level, "evidence": evidence})

    # ---- 截断证据 ----
    # TR001 显式截断标记（正文、或 next_offset 有值）
    hit_words = [w for w in TRUNC_MARKERS if w.lower() in str(res.get("note") or "").lower()
                 or w.lower() in tail.lower()]
    if hit_words:
        add("TR001", "TRUNCATED", "正文/备注命中截断措辞: %s" % "/".join(sorted(set(hit_words))[:4]))
    if next_offset not in (None, "", 0):
        add("TR001", "TRUNCATED", "响应给出 next_offset=%s（服务端明说还有后续）" % next_offset)

    # TR002 声明总量 > 实得
    if total is not None and items is not None and total > items:
        add("TR002", "TRUNCATED", "声明 total=%d，实得 items=%d（缺 %d）" % (total, items, total - items))

    # TR003 has_more 明确为真
    if has_more is True:
        add("TR003", "TRUNCATED", "has_more=true")

    # TR004 分页链接存在但没跟进
    if 'rel="next"' in link or "rel=next" in link:
        add("TR004", "TRUNCATED", '响应头带 rel="next"（服务端自认还有下一页）')

    # TR005 传输层截断：200 + 正文半截
    if parse == "fail" and (status is None or status == 200):
        add("TR005", "TRUNCATED", "status=%s 但正文解析失败 —— 这是半截正文，不是坏数据" % status)
    if bytes_read is not None and bytes_exp is not None and bytes_read < bytes_exp:
        add("TR005", "TRUNCATED", "读到 %d 字节 / 声明 %d 字节（差 %d）" % (bytes_read, bytes_exp, bytes_exp - bytes_read))
    if lines_read is not None and lines_total is not None and lines_read < lines_total:
        add("TR005", "TRUNCATED", "读到 %d 行 / 共 %d 行（差 %d）" % (lines_read, lines_total, lines_total - lines_read))

    # TR006 疑似静默封顶：要得更多、给的是整数帽、且完全没有总量字段
    no_total_evidence = (total is None and has_more is None and next_offset in (None, "", 0)
                         and 'rel=' not in link)
    if (req_limit is not None and items is not None and req_limit > items
            and items in KNOWN_CAPS and no_total_evidence):
        add("TR006", "UNKNOWN",
            "请求 limit=%d 实得 %d（命中常见上限 %d），且无 total/has_more/next/Link 任何总量字段 —— "
            "无法区分「只有这些」和「封顶了」" % (req_limit, items, items))

    # TR007 空集合归因：「0 条」不等于「没有」
    if items == 0:
        if total is not None and total > 0:
            add("TR007", "TRUNCATED", "items=0 但 total=%d" % total)
        elif no_total_evidence and not end_marker:
            add("TR007", "UNKNOWN", "items=0 且无任何总量/结束证据 —— 空结果与「被过滤掉」不可区分")

    # TR008 范围覆盖：offset 起点与覆盖长度
    if first_index is not None and req_offset is not None and first_index != req_offset:
        add("TR008", "UNKNOWN",
            "请求 offset=%s 但响应首条索引=%s —— 对齐关系不成立" % (req_offset, first_index))
    if (total is not None and items is not None and req_offset
            and req_offset + items < total):
        # 只在「已经在翻页中途」时报覆盖不足：第 0 页的缺额由 TR002 负责，避免重复告警
        add("TR008", "TRUNCATED",
            "覆盖区间 [%s, %s) 未到 total=%d" % (req_offset, req_offset + items, total))

    # ---- 完整性正面证据 ----
    complete_signals = []
    if total is not None and items is not None and total == items:
        complete_signals.append("total==items==%d" % items)
    if has_more is False:
        complete_signals.append("has_more=false")
    if end_marker:
        complete_signals.append("结束标记=%r" % str(end_marker)[:24])
    if (lines_read is not None and lines_total is not None and lines_read == lines_total
            and (bytes_exp is None or bytes_read == bytes_exp) and lines_total > 0):
        complete_signals.append("读完 %d/%d 行" % (lines_read, lines_total))
    if link and 'rel="next"' not in link and 'rel="last"' in link and page_is_last(snap):
        complete_signals.append("Link 中 last==当前页")

    # TR009 没有正面证据 —— 这才是默认结局
    if not complete_signals:
        has_any = any(f["level"] == "TRUNCATED" for f in findings)
        if not has_any:
            add("TR009", "UNKNOWN",
                "既无截断证据也无完整性证据 —— 「没看到 truncated」不能当全集证明")

    # ---- 汇总 ----
    if any(f["level"] == "TRUNCATED" for f in findings):
        verdict = "TRUNCATED"
    elif any(f["level"] == "UNKNOWN" for f in findings) or not complete_signals:
        verdict = "UNKNOWN"
    else:
        verdict = "COMPLETE"

    return {
        "verdict": verdict,
        "kind": kind,
        "findings": findings,
        "complete_signals": complete_signals,
        "cursor": {
            "next_offset": next_offset,
            "resume_hint": _resume_hint(verdict, next_offset, req_offset, items, total),
        },
    }


def page_is_last(snap):
    res = snap.get("response") or {}
    return bool(res.get("is_last_page"))


def _resume_hint(verdict, next_offset, req_offset, items, total):
    if verdict == "COMPLETE":
        return None
    if next_offset not in (None, "", 0):
        return "从 next_offset=%s 续读" % next_offset
    if items is not None and (req_offset is not None):
        return ("从 offset=%d 续读（假定该 API 用 offset/limit 语义且顺序稳定；"
                "用 page/per_page 的 API 需换算，且部分 API 有硬顶翻不过去）" % (req_offset + items))
    return "无法给出续读点：先拿到总量字段再续读"


# ----------------------------------------------------------------------------
# selftest：断言对准真实契约，不是对准「看起来很酷」
# ----------------------------------------------------------------------------
def _case(**kw):
    snap = {"kind": kw.pop("kind", "list"), "request": {}, "response": {}}
    for k, v in kw.items():
        if k in ("limit", "offset", "url", "path"):
            snap["request"][k] = v
        elif k in ("link",):
            snap["response"].setdefault("headers", {})["link"] = v
        else:
            snap["response"][k] = v
    return snap


def selftest():
    checks = []

    def eq(name, actual, expected):
        checks.append((name, actual, expected, actual == expected))

    # 1. 干净集合：total==items + has_more=false => COMPLETE
    v = judge(_case(items=100, total=100, has_more=False))["verdict"]
    eq("clean list => COMPLETE", v, "COMPLETE")

    # 2. 声明总量大于实得 => TRUNCATED
    v = judge(_case(items=100, total=250))["verdict"]
    eq("total>items => TRUNCATED", v, "TRUNCATED")

    # 3. 只有 Link rel=next => TRUNCATED
    v = judge(_case(items=100, link='<https://x/y?page=2>; rel="next"'))["verdict"]
    eq("rel=next => TRUNCATED", v, "TRUNCATED")

    # 4. 200 + 半截正文 => TRUNCATED（不是坏数据）
    v = judge(_case(status=200, parse="fail", bytes_read=592772, bytes_expected=609000))["verdict"]
    eq("200 + parse fail => TRUNCATED", v, "TRUNCATED")

    # 5. 静默封顶：请求 5000 给 1000，零总量字段 => UNKNOWN（不是 COMPLETE！）
    v = judge(_case(limit=5000, items=1000))["verdict"]
    eq("silent cap => UNKNOWN", v, "UNKNOWN")

    # 6. 同样的封顶，但服务端给了 nbHits => TRUNCATED
    v = judge(_case(limit=5000, items=1000, total=533688))["verdict"]
    eq("cap + declared total => TRUNCATED", v, "TRUNCATED")

    # 7. 空集合无任何总量证据 => UNKNOWN
    v = judge(_case(items=0))["verdict"]
    eq("empty, no evidence => UNKNOWN", v, "UNKNOWN")

    # 8. 空集合但 total>0 => TRUNCATED
    v = judge(_case(items=0, total=7))["verdict"]
    eq("empty but total>0 => TRUNCATED", v, "TRUNCATED")

    # 9. 文件读完（行数相等）=> COMPLETE
    v = judge(_case(kind="file", items=5000, lines_read=5000, lines_total=5000))["verdict"]
    eq("file fully read => COMPLETE", v, "COMPLETE")

    # 10. 文件半读 => TRUNCATED
    v = judge(_case(kind="file", lines_read=1042, lines_total=5000,
                    bytes_read=100000, bytes_expected=465000))["verdict"]
    eq("file partial => TRUNCATED", v, "TRUNCATED")

    # 11. next_offset 有值 => TRUNCATED
    v = judge(_case(kind="file", lines_read=1042, lines_total=5000, next_offset=1043))["verdict"]
    eq("next_offset present => TRUNCATED", v, "TRUNCATED")

    # 12. 显式结束标记 => COMPLETE
    v = judge(_case(items=42, end_marker="EOF"))["verdict"]
    eq("end_marker => COMPLETE", v, "COMPLETE")

    # 13. 正文命中中文截断措辞 => TRUNCATED
    v = judge(_case(items=10, text_tail="...已截断，共 900 行"))["verdict"]
    eq("cn truncation marker => TRUNCATED", v, "TRUNCATED")

    # 14. 完全空白快照 => UNKNOWN（不是 COMPLETE）
    v = judge({})["verdict"]
    eq("empty snapshot => UNKNOWN", v, "UNKNOWN")

    # 15. 非 dict 输入抛异常（不静默放行）
    try:
        judge([])
        raised = False
    except ValueError:
        raised = True
    eq("non-dict raises", raised, True)

    # 16. 续读点：有 next_offset 用它
    r = judge(_case(kind="file", lines_read=10, lines_total=99, next_offset=11))
    eq("resume uses next_offset", r["cursor"]["resume_hint"], "从 next_offset=11 续读")

    # 17. 续读点：无 next_offset 但有 items/offset => offset+items
    r = judge(_case(items=100, total=250, offset=0))
    eq("resume falls back to offset+items", r["cursor"]["resume_hint"].startswith("从 offset=100 续读"), True)

    # 18. COMPLETE 时没有续读点
    r = judge(_case(items=5, total=5, has_more=False))
    eq("complete has no resume", r["cursor"]["resume_hint"], None)

    # 19. 规则 id 稳定可审计
    ids = {f["rule"] for f in judge(_case(items=100, total=250, link='<a>; rel="next"'))["findings"]}
    eq("rule ids audited", sorted(ids), ["TR002", "TR004"])

    # 20. 每条 finding 都带证据文本（不许空断言）
    fs = judge(_case(items=100, total=250))["findings"]
    eq("findings carry evidence", all(f["evidence"].strip() for f in fs), True)

    # 21. has_more=false 单独就能成立 COMPLETE（服务端明确说没有下一页）
    v = judge(_case(items=3, has_more=False))["verdict"]
    eq("has_more=false alone => COMPLETE", v, "COMPLETE")

    # 22. has_more=false 与 total>items 冲突时，按证据更硬的一方判 TRUNCATED
    v = judge(_case(items=3, total=9, has_more=False))["verdict"]
    eq("conflict => TRUNCATED", v, "TRUNCATED")

    # 23. 200 但 parse 成功且无任何总量字段 => UNKNOWN（最常见的假绿灯现场）
    v = judge(_case(status=200, parse="ok", items=37))["verdict"]
    eq("200 + parse ok + no total => UNKNOWN", v, "UNKNOWN")

    # 24. offset 越界（首条索引与请求不一致）=> UNKNOWN
    v = judge(_case(items=10, offset=100, first_index=0, has_more=False))["verdict"]
    eq("offset mismatch => UNKNOWN", v, "UNKNOWN")

    # 25. 翻页中途覆盖不足 => TR008 单独报出
    fs = judge(_case(items=100, total=533688, offset=100))["findings"]
    eq("mid-range shortfall => TR008", [f["rule"] for f in fs], ["TR002", "TR008"])

    failed = [c for c in checks if not c[3]]
    for name, actual, expected, ok in checks:
        print("%-46s %s   got=%r want=%r" % (name, "PASS" if ok else "FAIL", actual, expected))
    print("-" * 78)
    print("selftest: %d/%d passed" % (len(checks) - len(failed), len(checks)))
    return EXIT_COMPLETE if not failed else EXIT_SELFTEST_FAIL


# ----------------------------------------------------------------------------
def load_snapshot(argv):
    if not argv:
        return None, "缺少快照文件参数"
    path = argv[0]
    if path == "-":
        raw = sys.stdin.read()
    else:
        if not os.path.isfile(path):
            return None, "快照文件不存在: %s" % path
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    if not raw.strip():
        return None, "快照为空"
    try:
        return json.loads(raw), None
    except Exception as exc:
        return None, "快照不是合法 JSON: %s" % exc


VERDICT_EXIT = {"COMPLETE": EXIT_COMPLETE, "TRUNCATED": EXIT_TRUNCATED, "UNKNOWN": EXIT_UNKNOWN}


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return EXIT_USAGE
    cmd = argv[1]
    if cmd == "selftest":
        return selftest()
    if cmd != "judge":
        print("未知子命令: %s" % cmd)
        return EXIT_USAGE

    rest = [a for a in argv[2:] if not a.startswith("--")]
    as_json = "--json" in argv
    snap, err = load_snapshot(rest)
    if err:
        print("trunc_probe: %s" % err)
        return EXIT_USAGE

    result = judge(snap)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("判定: %s   (kind=%s)" % (result["verdict"], result["kind"]))
        if result["complete_signals"]:
            print("完整性证据: " + "; ".join(result["complete_signals"]))
        for f in result["findings"]:
            print("  [%s] %s: %s" % (f["rule"], f["level"], f["evidence"]))
        if result["cursor"]["resume_hint"]:
            print("续读: " + result["cursor"]["resume_hint"])
    return VERDICT_EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
