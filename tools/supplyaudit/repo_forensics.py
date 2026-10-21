#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repo_forensics.py — 「这个高星新库是靠不靠得住」的离线取证器。

输入是 GitHub API 的真实回包（离线分析，不联网、可复算）：
    gh api "search/repositories?q=created:>2026-10-11+stars:>50&sort=stars&per_page=30" > repos.json
    gh api repos/<owner>/<repo>/events > events/<owner>__<repo>.json

输出每个仓库的可复核读数（不是判决）：首发延迟、fork/star 比、许可、体积、
WatchEvent 到达形态（最小 5 秒窗口内的占比 = 到达是不是挤在一起），
并给出 FARM_SUSPECT / NEEDS_EYES / ORGANIC 三档与逐条理由。

用法：
    python repo_forensics.py analyze --repos repos.json [--events-dir events/] [--json out.json]
    python repo_forensics.py selftest

退出码：0 无可疑 | 2 有 NEEDS_EYES | 3 有 FARM_SUSPECT | 4 零输入 | 5 selftest 失败

诚实边界（务必连同结论一起读）：
  * /repos/{r}/events 只回最近 90 天、最多 300 条 → watch_count 是下界，
    到达形态只在可见窗口内成立；events 缺失时相关判据置为 UNKNOWN，不假装测过。
  * star:watch 比在本机历史实测中落在 180–380，两种库重叠，**没有区分度**，
    因此本工具不用它做判据（用它等于用噪声）。
  * 阈值是从真实样本上校准出来的启发式，不是硬件事实；FARM_SUSPECT 只是
    「把人叫过来看」的触发器，不是「判定为恶意」。真正的判定要看内容（用 supply_audit.py）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

VERSION = "1.0.0"

# 阈值（基准见 article 的校准表；改这里就要重跑 selftest）
TH = {
    "first_push_fast_s": 900,      # 创建后 15 分钟内就 push 完 → 一次性搬运的形态
    "tiny_size_kb": 60,            # 太小 → 装不下工程
    "low_fork_ratio": 0.03,        # fork/star 过低 → 没有人在二次使用
    "watch_burst_share": 0.5,      # 最小窗口内 >=50% 的 watch 挤在一起
    "watch_min_events": 10,        # 少于 10 个 watch 谈到达形态没意义
}


def parse_ts(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def repo_metrics(r, now):
    created = parse_ts(r.get("created_at"))
    pushed = parse_ts(r.get("pushed_at"))
    stars = r.get("stargazers_count") or 0
    forks = r.get("forks_count") or 0
    m = {
        "repo": r.get("full_name"),
        "stars": stars,
        "forks": forks,
        "fork_ratio": round(forks / stars, 4) if stars else None,
        "size_kb": r.get("size"),
        "license": (r.get("license") or {}).get("spdx_id") if r.get("license") else None,
        "created": r.get("created_at"),
        "pushed": r.get("pushed_at"),
        "age_days": round((now - created).total_seconds() / 86400, 2) if created else None,
        "first_push_latency_s": int((pushed - created).total_seconds()) if (created and pushed) else None,
        "desc_len": len(r.get("description") or ""),
        "has_homepage": bool(r.get("homepage")),
        "topics": r.get("topics") or [],
        "homepage": r.get("homepage") or "",
    }
    return m


def watch_shape(events, m):
    """仅用可见事件窗口计算到达形态；events 缺失 → UNKNOWN（不假装测过）"""
    if events is None:
        return {"watch_count": None, "watch_burst_share": None, "window_hours": None,
                "truncated": None, "shape": "UNKNOWN"}
    watch = [parse_ts(e.get("created_at")) for e in events if e.get("type") == "WatchEvent"]
    watch = [w for w in watch if w]
    if not watch:
        return {"watch_count": 0, "watch_burst_share": None, "window_hours": None,
                "truncated": len(events) >= 300, "shape": "NO_WATCH_IN_WINDOW"}
    span = (max(watch) - min(watch)).total_seconds()
    # 最小窗口：把 watch 时间戳排序后，找一段能把最多 watch 装进去的 5 秒窗口
    ws = sorted(watch)
    best, j = 0, 0
    for i in range(len(ws)):
        while j < len(ws) and (ws[j] - ws[i]).total_seconds() <= 5:
            j += 1
        best = max(best, j - i)
    share = round(best / len(ws), 3)
    return {
        "watch_count": len(watch),
        "watch_burst_share": share,
        "max_in_5s": best,
        "span_hours": round(span / 3600, 2),
        "newest_watch": max(ws).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "truncated": len(events) >= 300,
        "shape": "BURST" if share >= TH["watch_burst_share"] and len(ws) >= TH["watch_min_events"] else "SPREAD",
    }


def judge(m, shape):
    reasons, weak = [], 0
    if m["license"] in (None, "NOASSERTION"):
        weak += 1
        reasons.append("无 license")
    if m["fork_ratio"] is not None and m["fork_ratio"] < TH["low_fork_ratio"]:
        weak += 1
        reasons.append("fork/star %.4f < %.2f" % (m["fork_ratio"], TH["low_fork_ratio"]))
    if (m["size_kb"] or 0) < TH["tiny_size_kb"]:
        weak += 1
        reasons.append("体积 %dKB < %dKB" % (m["size_kb"] or 0, TH["tiny_size_kb"]))
    if m["first_push_latency_s"] is not None and m["first_push_latency_s"] < TH["first_push_fast_s"]:
        weak += 1
        reasons.append("首发延迟 %ds < %ds（一次性搬运形态）" % (m["first_push_latency_s"], TH["first_push_fast_s"]))
    burst = shape["shape"] == "BURST"
    if burst:
        reasons.append("WatchEvent 到达挤在一起（%d 个里 %d 个落在同一个 5 秒窗）"
                       % (shape["watch_count"], shape["max_in_5s"]))
    if burst or weak >= 4:
        return "FARM_SUSPECT", reasons
    if weak >= 2:
        return "NEEDS_EYES", reasons
    return "ORGANIC", reasons or ["四条结构判据全部未触发"]


def load_events(events_dir, full_name):
    if not events_dir:
        return None
    slug = full_name.replace("/", "__")
    p = os.path.join(events_dir, slug + ".json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else None


def cross_repo_domains(rows):
    """同一批新库里共用同一个域名/文案 → 一个 campaign 的形态线索"""
    import re as _re
    from collections import defaultdict
    dom = defaultdict(list)
    for r in rows:
        text = " ".join([r.get("homepage") or "", r.get("desc") or ""])
        for url in _re.findall(r"https?://[^\s,)\]\"']+|(?:www\.)[^\s,)\]\"']+", text):
            host = _re.sub(r"^https?://", "", url).split("/")[0].lower()
            host = host.split(":")[0]
            if host and "." in host:
                dom[host].append(r["repo"])
    return {d: v for d, v in dom.items() if len(v) >= 2}


def analyze(repos_json, events_dir, as_json=None, quiet=False):
    with open(repos_json, encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not items:
        print("NO_INPUT: %s 里没有 items（零输入 ≠ 干净，exit 4）" % repos_json)
        return 4
    now = datetime.now(timezone.utc)
    rows = []
    for r in items:
        m = repo_metrics(r, now)
        shape = watch_shape(load_events(events_dir, m["repo"]), m)
        verd, reasons = judge(m, shape)
        m.update(shape)
        m["verdict"] = verd
        m["reasons"] = reasons
        m["desc"] = r.get("description") or ""
        rows.append(m)

    order = {"FARM_SUSPECT": 0, "NEEDS_EYES": 1, "ORGANIC": 2}
    rows.sort(key=lambda x: (order[x["verdict"]], -(x["stars"] or 0)))

    if not quiet:
        print("== repo_forensics v%s | 样本 %d 个 | %s ==" % (VERSION, len(rows), now.strftime("%Y-%m-%d %H:%M UTC")))
        print("%-42s %-13s %6s %6s %9s %8s %-6s %s" % ("repo", "verdict", "★", "fork比", "首发延迟", "体积KB", "watch", "形/理由"))
        for m in rows:
            lat = "-" if m["first_push_latency_s"] is None else ("%ds" % m["first_push_latency_s"])
            wr = "-" if m["watch_count"] is None else ("%d/%s" % (m["watch_count"], m["shape"][:4]))
            print("%-42s %-13s %6d %6s %9s %8s %-6s %s" % (
                m["repo"][:42], m["verdict"], m["stars"], m["fork_ratio"], lat, m["size_kb"], wr,
                "; ".join(m["reasons"])[:80]))
        shared = cross_repo_domains(rows)
        if shared:
            print("\n跨库共用域名（一个 campaign 的形态线索）: %s" % json.dumps(shared, ensure_ascii=False))
        counts = {}
        for m in rows:
            counts[m["verdict"]] = counts.get(m["verdict"], 0) + 1
        print("\n分布: %s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
        print("阈值: %s" % json.dumps(TH, ensure_ascii=False))
        print("提醒: WatchEvent 只覆盖最近 90 天且上限 300 条 → 计数是下界；阈值是启发式不是判决。")

    if as_json:
        with open(as_json, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "generated": now.isoformat(), "thresholds": TH,
                       "rows": rows}, f, ensure_ascii=False, indent=2)
        print("已写出: %s" % as_json)

    if any(m["verdict"] == "FARM_SUSPECT" for m in rows):
        return 3
    if any(m["verdict"] == "NEEDS_EYES" for m in rows):
        return 2
    return 0


# ------------------------------------------------------------------ selftest
def selftest():
    import tempfile
    checks = []
    now = datetime(2026, 10, 14, 6, 0, 0, tzinfo=timezone.utc)

    def mk(name, stars, forks, size, created, pushed, lic, desc="a tool"):
        return {"full_name": name, "stargazers_count": stars, "forks_count": forks, "size": size,
                "created_at": created, "pushed_at": pushed,
                "license": ({"spdx_id": lic} if lic else None), "description": desc,
                "homepage": "", "topics": []}

    farm = mk("farm/payload", 1200, 4, 12, "2026-10-12T08:00:00Z", "2026-10-12T08:03:00Z", None,
              "The best AI agent toolbox you need")
    organic = mk("dev/real-tool", 1295, 458, 6001, "2026-10-12T08:41:19Z", "2026-10-14T05:47:15Z", "MIT")
    mid = mk("dev/half-suspect", 300, 3, 816, "2026-10-13T17:08:53Z", "2026-10-13T17:10:50Z", "MIT")

    mf = repo_metrics(farm, now); sf = watch_shape(None, mf)
    vf, rf = judge(mf, sf)
    checks.append(("农场形态 → FARM_SUSPECT", vf == "FARM_SUSPECT", "; ".join(rf)))

    mo = repo_metrics(organic, now); so = watch_shape(None, mo)
    vo, ro = judge(mo, so)
    checks.append(("真实工程形态 → ORGANIC", vo == "ORGANIC", "; ".join(ro)))

    mm = repo_metrics(mid, now); sm = watch_shape(None, mm)
    vm, rm = judge(mm, sm)
    checks.append(("缺一半信号 → NEEDS_EYES（不是直接判农场）", vm == "NEEDS_EYES", "; ".join(rm)))

    # 到达形态：10 个 watch 全部挤在 4 秒内=BURST；摊在 10 天=SPREAD
    burst = [{"type": "WatchEvent", "created_at": "2026-10-13T00:00:%02dZ" % i} for i in range(10)]
    bshape = watch_shape(burst, mo)
    spread = [{"type": "WatchEvent", "created_at": "2026-10-%02dT00:00:00Z" % (1 + i)} for i in range(10)]
    sshape = watch_shape(spread, mo)
    checks.append(("WatchEvent 挤簇 → BURST", bshape["shape"] == "BURST",
                   "max_in_5s=%s share=%s" % (bshape["max_in_5s"], bshape["watch_burst_share"])))
    checks.append(("WatchEvent 摊开 → SPREAD", sshape["shape"] == "SPREAD", "share=%s" % sshape["watch_burst_share"]))
    checks.append(("events 缺失时到达形态记 UNKNOWN（不假装测过）",
                   sf["shape"] == "UNKNOWN" and sf["watch_count"] is None, str(sf["shape"])))
    checks.append(("watch 数少于阈值不判 BURST（避免小样本噪声）",
                   watch_shape([{"type": "WatchEvent", "created_at": "2026-10-13T00:00:0%dZ" % i} for i in range(3)],
                               mo)["shape"] == "SPREAD", "3 个 watch 不构成到达形态"))

    # 端到端：写真实 API 形状的 JSON，再跑 analyze
    tmp = tempfile.mkdtemp(prefix="repoforensics_")
    rp = os.path.join(tmp, "repos.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({"total_count": 3, "items": [farm, organic, mid]}, f)
    ev = os.path.join(tmp, "events")
    os.makedirs(ev, exist_ok=True)
    with open(os.path.join(ev, "farm__payload.json"), "w", encoding="utf-8") as f:
        json.dump(burst, f)
    code = analyze(rp, ev, quiet=True)
    checks.append(("端到端 exit 3（有 FARM_SUSPECT）", code == 3, "code=%d" % code))

    empty = os.path.join(tmp, "empty.json")
    with open(empty, "w", encoding="utf-8") as f:
        json.dump({"total_count": 0, "items": []}, f)
    checks.append(("零输入 exit 4", analyze(empty, None, quiet=True) == 4, "ok"))

    # 确定性
    a = analyze(rp, ev, as_json=os.path.join(tmp, "a.json"), quiet=True)
    b = analyze(rp, ev, as_json=os.path.join(tmp, "b.json"), quiet=True)
    same = json.load(open(os.path.join(tmp, "a.json"), encoding="utf-8"))["rows"] == \
        json.load(open(os.path.join(tmp, "b.json"), encoding="utf-8"))["rows"]
    checks.append(("同输入两次判定一致", a == b == 3 and same, "code=%d/%d" % (a, b)))

    # 跨库共用域名
    shared = cross_repo_domains([{"repo": "a/x", "homepage": "https://ps-ps.cc/dl", "desc": ""},
                                 {"repo": "b/y", "homepage": "https://ps-ps.cc/other", "desc": ""}])
    checks.append(("跨库共用域名能被识别", "ps-ps.cc" in shared and len(shared["ps-ps.cc"]) == 2,
                   json.dumps(shared, ensure_ascii=False)))

    print("== repo_forensics selftest v%s ==" % VERSION)
    ok = 0
    for name, passed, detail in checks:
        print("  [%s] %s  (%s)" % ("PASS" if passed else "FAIL", name, detail))
        ok += 1 if passed else 0
    print("结果: %d/%d" % (ok, len(checks)))
    return 0 if ok == len(checks) else 5


def main(argv=None):
    ap = argparse.ArgumentParser(description="高星新库的离线取证（不联网，读 GitHub API 回包）")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("analyze")
    a.add_argument("--repos", required=True)
    a.add_argument("--events-dir")
    a.add_argument("--json", dest="json_out")
    a.add_argument("--quiet", action="store_true")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "analyze":
        if not os.path.exists(args.repos):
            print("NO_INPUT: 找不到 %s（exit 4）" % args.repos)
            return 4
        return analyze(args.repos, args.events_dir, args.json_out, args.quiet)
    ap.print_help()
    return 4


if __name__ == "__main__":
    sys.exit(main())
