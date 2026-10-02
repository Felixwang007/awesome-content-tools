#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bind_audit.py -- 规则绑定审计（acceptance binding audit）

问题：一个任务自报成功（last_status=ok / exit 0 / "已完成"），但外部世界没有任何
可核验的产物。这类失败在日志里是绿色的，在产物上是空的——"绿灯无产物"。

本工具做一件事：把**读侧计数器**（规则在上下文里被提到/被读到的次数）和
**写侧计数器**（外部世界可核验的产物件数）摆在一起对账。

三档证据：
  SELF   自报          任务自己的状态字段（last_status / exit code / 文本声称）
  PROC   过程证据      有真进程跑过（脚本 exit 0、有 stdout）——只证明"跑了"
  ARTF   产物证据      外部可核验的产物（文件 / URL / 账本条目）——证明"留下了"

规则：BOUND（绑定）要求 self_report=ok 且至少一条 ARTF 证据被**实际核验**过。
      只声明不核验的产物记为 DECLARED，不计入绑定。

用法：
  python bind_audit.py selftest
  python bind_audit.py audit <ledger.jsonl> [--now ISO8601]
  python bind_audit.py scan-jobs <jobs.json> [--now ISO8601]
  python bind_audit.py explain

退出码：0=全部绑定  2=存在绿灯无产物  3=纯在场（读侧>0 写侧=0）  1=用法/输入错误
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

# ---------------------------------------------------------------- 证据核验

EVIDENCE_KINDS = ("file", "url", "ledger")


def _parse_ts(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _now(now_arg=None):
    t = _parse_ts(now_arg) if now_arg else None
    if t is None:
        t = dt.datetime.now().astimezone()
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return t


def verify_evidence(item, base_dir=".", check_urls=False):
    """把一条声明的证据核验成 VERIFIED / DECLARED / BROKEN。

    - file:   本地路径存在且字节数 > 0
    - ledger: 本地账本文件存在，且含 item['match'] 子串（默认非空即可）
    - url:    默认只记为 DECLARED（离线审计不联网）；--check-urls 时发 HEAD/GET
    """
    kind = item.get("kind")
    ref = str(item.get("ref") or "").strip()
    if not ref:
        return "BROKEN", "ref 为空"
    if kind == "file":
        p = ref if os.path.isabs(ref) else os.path.join(base_dir, ref)
        if not os.path.exists(p):
            return "BROKEN", f"文件不存在: {ref}"
        try:
            size = os.path.getsize(p)
        except OSError as e:
            return "BROKEN", f"读不到大小: {e}"
        if size <= 0:
            return "BROKEN", f"文件 0 字节: {ref}"
        return "VERIFIED", f"{size} bytes"
    if kind == "ledger":
        p = ref if os.path.isabs(ref) else os.path.join(base_dir, ref)
        if not os.path.exists(p):
            return "BROKEN", f"账本不存在: {ref}"
        want = item.get("match")
        if not want:
            return ("VERIFIED", "账本存在") if os.path.getsize(p) > 0 else ("BROKEN", "账本为空")
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if want in line:
                    return "VERIFIED", f"命中 {want[:40]}"
        return "BROKEN", f"账本里没有 {want[:40]}"
    if kind == "url":
        if not check_urls:
            return "DECLARED", "未联网核验（加 --check-urls 才校验）"
        return _http_probe(ref)
    return "BROKEN", f"未知证据类型: {kind}"


def _http_probe(url, timeout=15):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, method="GET")
    # 关键：不带真实 UA 时，很多站点对不存在的内容也返回 200 空壳，会把"没发布"审计成"已发布"
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0 Safari/537.36")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4096)
            return "VERIFIED", f"HTTP {r.status}, {len(body)}+ bytes"
    except urllib.error.HTTPError as e:
        return "BROKEN", f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - 审计工具要吞掉网络异常并如实记录
        return "BROKEN", f"请求失败: {type(e).__name__}"


# ---------------------------------------------------------------- 单条记录判定

def judge_record(rec, base_dir=".", check_urls=False):
    """返回 (verdict, detail_dict)。verdict ∈ BOUND / UNBOUND_GREEN / HONEST_FAIL / MIRAGE"""
    self_report = str(rec.get("self_report", "")).lower()
    ok = self_report in ("ok", "success", "green", "true", "0")
    reads = int(rec.get("read_side") or 0)
    ev_in = rec.get("evidence") or []
    if isinstance(ev_in, str):
        ev_in = [{"kind": "file", "ref": ev_in}]
    states = []
    for it in ev_in:
        st, why = verify_evidence(it, base_dir=base_dir, check_urls=check_urls)
        states.append({"kind": it.get("kind"), "ref": it.get("ref"), "state": st, "why": why})
    verified = [s for s in states if s["state"] == "VERIFIED"]
    declared = [s for s in states if s["state"] == "DECLARED"]
    broken = [s for s in states if s["state"] == "BROKEN"]
    if ok and verified:
        verdict = "BOUND"
    elif ok and not verified:
        verdict = "UNBOUND_GREEN"
    elif not ok and verified:
        verdict = "MIRAGE"          # 产物在，自报失败：状态字段失真（方向相反）
    else:
        verdict = "HONEST_FAIL"
    detail = {
        "run_id": rec.get("run_id"),
        "job": rec.get("job"),
        "at": rec.get("at"),
        "self_report": self_report or "(空)",
        "read_side": reads,
        "verified": len(verified),
        "declared": len(declared),
        "broken": len(broken),
        "states": states,
        "verdict": verdict,
    }
    return detail


def summarize(details, now):
    """按 job 聚合：绑定率、读/写计数、绿灯无产物的最长静默天数。"""
    jobs = {}
    for d in details:
        j = jobs.setdefault(d["job"], {
            "job": d["job"], "runs": 0, "bound": 0, "unbound_green": 0,
            "honest_fail": 0, "mirage": 0, "read_side": 0, "write_side": 0,
            "last_artifact_at": None,
        })
        j["runs"] += 1
        j["read_side"] += d["read_side"]
        j["write_side"] += d["verified"]
        j[{"BOUND": "bound", "UNBOUND_GREEN": "unbound_green",
           "HONEST_FAIL": "honest_fail", "MIRAGE": "mirage"}[d["verdict"]]] += 1
        if d["verified"]:
            ts = _parse_ts(d.get("at"))
            if ts and (j["last_artifact_at"] is None or ts > j["last_artifact_at"]):
                j["last_artifact_at"] = ts
    for j in jobs.values():
        j["binding_ratio"] = round(j["bound"] / j["runs"], 3) if j["runs"] else 0.0
        if j["binding_ratio"] == 1.0:
            j["status"] = "BOUND"
        elif j["bound"] > 0:
            j["status"] = "PARTIAL"
        elif j["unbound_green"] == 0 and j["mirage"] > 0:
            j["status"] = "MIRAGE"
        elif j["unbound_green"] == 0:
            j["status"] = "HONEST_FAIL"
        elif j["read_side"] > 0:
            j["status"] = "PRESENCE_ONLY"
        else:
            j["status"] = "NO_EVIDENCE"
        if j["last_artifact_at"] is None:
            j["silent_days"] = None
        else:
            j["silent_days"] = round((now - j["last_artifact_at"]).total_seconds() / 86400.0, 1)
    return jobs


def exit_code(details):
    if any(d["verdict"] == "UNBOUND_GREEN" for d in details):
        return 2
    if details and all(d["verdict"] in ("UNBOUND_GREEN", "HONEST_FAIL") for d in details) \
            and sum(d["read_side"] for d in details) > 0 and sum(d["verified"] for d in details) == 0:
        return 3
    return 0


# ---------------------------------------------------------------- 渲染

def render_ledger(details, jobs, now):
    out = []
    out.append(f"审计时间: {now.isoformat(timespec='seconds')}")
    out.append(f"记录数: {len(details)}  任务数: {len(jobs)}")
    out.append("")
    out.append(f"{'run_id':<14}{'job':<22}{'自报':<8}{'读':>4}{'证据V/D/B':>12}  {'判定':<16}")
    out.append("-" * 82)
    for d in details:
        ev = f"{d['verified']}/{d['declared']}/{d['broken']}"
        out.append(f"{str(d['run_id']):<14}{str(d['job']):<22}{d['self_report']:<8}"
                   f"{d['read_side']:>4}{ev:>12}  {d['verdict']:<16}")
    out.append("")
    out.append(f"{'job':<22}{'runs':>5}{'BOUND':>7}{'绿灯无产物':>11}{'读侧':>6}{'写侧':>6}"
               f"{'绑定率':>8}{'静默天数':>9}  状态")
    out.append("-" * 100)
    for j in sorted(jobs.values(), key=lambda x: (x["binding_ratio"], x["job"])):
        sd = "-" if j["silent_days"] is None else f"{j['silent_days']}"
        out.append(f"{j['job']:<22}{j['runs']:>5}{j['bound']:>7}{j['unbound_green']:>11}"
                   f"{j['read_side']:>6}{j['write_side']:>6}{j['binding_ratio']:>8}"
                   f"{sd:>9}  {j['status']}")
    verdict = "ALL BOUND" if exit_code(details) == 0 else "UNBOUND FOUND"
    out.append("")
    out.append(f"门禁判定: {verdict}（退出码 {exit_code(details)}）")
    return "\n".join(out)


def render_scan(rows, now):
    """rows: [{'slot','category','enabled','state','last_status','evidence_class','days_since_run'}]"""
    out = []
    out.append(f"扫描时间: {now.isoformat(timespec='seconds')}")
    out.append(f"任务总数: {len(rows)}")
    out.append("")
    out.append(f"{'槽位':<8}{'类别':<10}{'启用':<6}{'状态':<12}{'自报':<8}{'证据档':<16}{'停摆天数':>9}")
    out.append("-" * 78)
    for r in rows:
        d = "-" if r["days_since_run"] is None else f"{r['days_since_run']}"
        out.append(f"{r['slot']:<8}{r['category']:<10}{('是' if r['enabled'] else '否'):<6}"
                   f"{r['state']:<12}{r['last_status']:<8}{r['evidence_class']:<16}{d:>9}")
    n = len(rows)
    en = sum(1 for r in rows if r["enabled"])
    artf = sum(1 for r in rows if r["evidence_class"] == "ARTIFACT")
    weak = sum(1 for r in rows if r["evidence_class"] == "PROCESS")
    self_only = sum(1 for r in rows if r["evidence_class"] == "SELF")
    stale = [r for r in rows if not r["enabled"] and r["last_status"] in ("ok", "success")]
    out.append("")
    out.append(f"启用 {en}/{n} | 产物证据 {artf} | 过程证据 {weak} | 仅自报 {self_only}")
    out.append(f"绿灯残留在未启用任务上: {len(stale)} 个"
               + (f"，最长 {max((r['days_since_run'] or 0) for r in stale)} 天" if stale else ""))
    out.append(f"绑定率(产物证据/总任务): {round(artf / n, 3) if n else 0.0}")
    out.append("")
    out.append("判定: 仅自报的任务在失败时不会报警——它们的信号源与执行者是同一方。")
    return "\n".join(out)


# ---------------------------------------------------------------- 真实 cron jobs.json 扫描

CATEGORY_RULES = [
    ("发布", ("发布", "发文", "publish", "影评", "文章", "头条")),
    ("复盘", ("复盘", "对账", "跟踪", "track", "review")),
    ("巡检", ("巡检", "安全", "扫描", "security", "scan")),
    ("心跳", ("心跳", "heartbeat", "keepalive")),
    ("学习", ("学习", "情报", "发现", "社区", "research")),
]


def categorize(name):
    low = (name or "").lower()
    for cat, keys in CATEGORY_RULES:
        if any(k.lower() in low for k in keys):
            return cat
    return "其他"


def evidence_class(job):
    """从任务声明本身推断它的成功证据属于哪一档。"""
    if job.get("monitor_script") or job.get("monitor_url"):
        return "ARTIFACT"
    if job.get("no_agent") and job.get("script"):
        return "PROCESS"
    return "SELF"


def scan_jobs(path, now):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    rows = []
    for i, j in enumerate(jobs, 1):
        last_run = _parse_ts(j.get("last_run_at"))
        days = None
        if last_run is not None:
            days = round((now - last_run).total_seconds() / 86400.0, 1)
        rows.append({
            "slot": f"job#{i:02d}",
            "category": categorize(j.get("name")),
            "enabled": bool(j.get("enabled")),
            "state": str(j.get("state") or ("scheduled" if j.get("enabled") else "disabled")),
            "last_status": str(j.get("last_status") or "never"),
            "evidence_class": evidence_class(j),
            "days_since_run": days,
        })
    return rows


# ---------------------------------------------------------------- 自检

def _tmpdir():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp")
    os.makedirs(d, exist_ok=True)
    return d


def selftest():
    """自带样例 + 断言 + 退出码即结论。"""
    tmp = _tmpdir()
    results = []

    def check(name, got, want):
        ok = got == want
        results.append((name, ok, got, want))

    # 样例 1：产物证据（文件真的存在且非空）→ BOUND
    art = os.path.join(tmp, "artifact.txt")
    with open(art, "w", encoding="utf-8") as f:
        f.write("published: gid=123\n")
    d = judge_record({"run_id": "r1", "job": "j-artifact", "self_report": "ok",
                         "read_side": 1, "evidence": [{"kind": "file", "ref": art}]})
    check("产物存在且非空 → BOUND", d["verdict"], "BOUND")
    check("  核验计数=1", d["verified"], 1)

    # 样例 2：0 字节文件 → 不算证据
    empty = os.path.join(tmp, "empty.txt")
    open(empty, "w").close()
    d = judge_record({"run_id": "r2", "job": "j-empty", "self_report": "ok",
                         "read_side": 1, "evidence": [{"kind": "file", "ref": empty}]})
    check("0 字节文件 → UNBOUND_GREEN", d["verdict"], "UNBOUND_GREEN")
    check("  BROKEN 计数=1", d["broken"], 1)

    # 样例 3：路径不存在 → 记为 BROKEN 而不是异常
    d = judge_record({"run_id": "r3", "job": "j-missing", "self_report": "ok",
                         "read_side": 2, "evidence": [{"kind": "file", "ref": os.path.join(tmp, "nope.txt")}]})
    check("产物不存在 → UNBOUND_GREEN", d["verdict"], "UNBOUND_GREEN")

    # 样例 4：URL 默认不联网 → DECLARED，不计入绑定
    d = judge_record({"run_id": "r4", "job": "j-url", "self_report": "ok",
                         "read_side": 1, "evidence": [{"kind": "url", "ref": "https://example.com/x"}]})
    check("URL 未核验 → DECLARED 不算绑定", (d["verdict"], d["declared"]), ("UNBOUND_GREEN", 1))

    # 样例 5：账本命中/未命中
    led = os.path.join(tmp, "ledger.jsonl")
    with open(led, "w", encoding="utf-8") as f:
        f.write('{"gid": "7681570773751939584"}\n')
    d = judge_record({"run_id": "r5", "job": "j-led", "self_report": "ok", "read_side": 1,
                         "evidence": [{"kind": "ledger", "ref": led, "match": "7681570773751939584"}]})
    check("账本命中 → BOUND", d["verdict"], "BOUND")
    d = judge_record({"run_id": "r6", "job": "j-led", "self_report": "ok", "read_side": 1,
                         "evidence": [{"kind": "ledger", "ref": led, "match": "9999999999999999999"}]})
    check("账本未命中 → UNBOUND_GREEN", d["verdict"], "UNBOUND_GREEN")

    # 样例 6：产物在但自报失败 → MIRAGE（反方向失真）
    d = judge_record({"run_id": "r7", "job": "j-mirage", "self_report": "failed",
                         "read_side": 0, "evidence": [{"kind": "file", "ref": art}]})
    check("自报失败但产物在 → MIRAGE", d["verdict"], "MIRAGE")

    # 样例 7：在场≠绑定（12 次读到规则，0 件产物）
    recs = [{"run_id": f"p{i:02d}", "job": "j-presence", "self_report": "ok",
             "read_side": 1, "at": f"2026-09-{i:02d}T08:00:00+08:00", "evidence": []}
            for i in range(1, 13)]
    details = [judge_record(r, base_dir=tmp) for r in recs]
    jobs = summarize(details, _now("2026-10-02T12:00:00+08:00"))
    j = jobs["j-presence"]
    check("12 次读 / 0 件产物 → PRESENCE_ONLY", j["status"], "PRESENCE_ONLY")
    check("  读侧计数=12", j["read_side"], 12)
    check("  写侧计数=0", j["write_side"], 0)
    check("  静默天数=None（从未有过产物）", j["silent_days"], None)
    check("门禁退出码=2", exit_code(details), 2)

    # 样例 8：混合（1 绑定 1 绿灯）→ PARTIAL
    mixed = [judge_record({"run_id": "m1", "job": "j-mixed", "self_report": "ok", "read_side": 1,
                           "evidence": [{"kind": "file", "ref": art}]}, base_dir=tmp),
             judge_record({"run_id": "m2", "job": "j-mixed", "self_report": "ok", "read_side": 1,
                           "evidence": []}, base_dir=tmp)]
    jm = summarize(mixed, _now("2026-10-02T12:00:00+08:00"))["j-mixed"]
    check("1 绑定 1 绿灯 → PARTIAL + 0.5", (jm["status"], jm["binding_ratio"]), ("PARTIAL", 0.5))

    # 样例 9：无自报（空字段）且无证据 → HONEST_FAIL，不是成功
    d = judge_record({"run_id": "r8", "job": "j-noself", "read_side": 0, "evidence": []})
    check("缺字段 → HONEST_FAIL", d["verdict"], "HONEST_FAIL")

    # 样例 10：证据类别推断
    check("no_agent+script → PROCESS",
          evidence_class({"no_agent": True, "script": "x.py"}), "PROCESS")
    check("monitor_url → ARTIFACT",
          evidence_class({"monitor_url": "https://x/y"}), "ARTIFACT")
    check("纯 prompt → SELF", evidence_class({"prompt": "..."}), "SELF")

    print("=" * 72)
    print("bind_audit selftest")
    print("=" * 72)
    bad = 0
    for name, ok, got, want in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
        if not ok:
            bad += 1
    print("-" * 72)
    print(f"{len(results) - bad}/{len(results)} 通过")
    return 0 if bad == 0 else 1


def explain():
    print(__doc__)
    print("三档证据:")
    print("  SELF   自报       last_status / exit code / 文本声称 —— 信号源与执行者是同一方")
    print("  PROC   过程证据   有真进程跑过 —— 证明'跑了'，不证明'留下了'")
    print("  ARTF   产物证据   外部可核验的产物 —— 唯一能支撑 BOUND 的一档")
    print()
    print("判定表:")
    print("  自报 ok  + 产物核验通过   → BOUND          规则被绑定")
    print("  自报 ok  + 无产物核验     → UNBOUND_GREEN  绿灯无产物（最贵的一种失败）")
    print("  自报 fail + 产物在        → MIRAGE         状态字段失真（反方向）")
    print("  自报 fail + 无产物        → HONEST_FAIL    诚实的失败，成本最低")
    print()
    print("账本字段（JSONL 一行一条 run）:")
    print('  {"run_id": "...", "job": "...", "at": "2026-10-02T08:00:00+08:00",')
    print('   "self_report": "ok", "read_side": 3,')
    print('   "evidence": [{"kind": "file|url|ledger", "ref": "...", "match": "..."}]}')
    return 0


# ---------------------------------------------------------------- CLI

def load_ledger(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"账本第 {i} 行不是合法 JSON: {e}")
    return recs


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bind_audit.py", description="规则绑定审计")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    sub.add_parser("explain")
    p1 = sub.add_parser("audit")
    p1.add_argument("ledger")
    p1.add_argument("--now")
    p1.add_argument("--check-urls", action="store_true")
    p2 = sub.add_parser("scan-jobs")
    p2.add_argument("jobs_json")
    p2.add_argument("--now")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "explain" or a.cmd is None:
        return explain()
    now = _now(getattr(a, "now", None))
    if a.cmd == "audit":
        recs = load_ledger(a.ledger)
        if not recs:
            print("账本为空：没有任何可审计的 run（这不是'干净'，是'没输入'）", file=sys.stderr)
            return 1
        details = [judge_record(r, base_dir=os.path.dirname(os.path.abspath(a.ledger)),
                                check_urls=a.check_urls) for r in recs]
        print(render_ledger(details, summarize(details, now), now))
        return exit_code(details)
    if a.cmd == "scan-jobs":
        rows = scan_jobs(a.jobs_json, now)
        if not rows:
            print("jobs 文件里没有任务", file=sys.stderr)
            return 1
        print(render_scan(rows, now))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
