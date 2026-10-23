#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""par_batch.py — 并行工具调用的安全分批器 / 竞态探测器（纯标准库，无第三方依赖）

三个问题分开回答，不混在一起：

  1. plan   —— 我看到的是「声明」的效果（哪个 job 读什么、写什么）。
               它算的是「哪些调用可以同批、哪些必须排队」，并把冲突原因列出来。
  2. ledger —— 我看到的是「实际发生的」效果（每次调用真实的 start/end 时间）。
               它算的是「已经发生的这批里，有没有同一路径上的写-写/写-读重叠」。
  3. probe  —— 这两者之间的缺口，有相当一部分不取决于我的代码，而取决于
               当前 OS/文件系统上 append/open 的原子性语义。probe 在本机实测。

为什么必须分成三层：plan 通过只说明「我以为安全」；ledger 通过只说明
「这次恰好没撞上」；probe 说「这台的底层语义允许/不允许撞上」。三者任一单独
使用都会给出看起来正确的错误结论。

退出码：0 干净 / 2 检出冲突或竞态（需要串行化或人看）/ 3 用法错误或零输入
（「没输入」不等于「输入干净」）/ 5 自检失败。
"""
import os
import sys
import json
import time
import shutil
import argparse
import subprocess
import tempfile

OK, CONFLICT, BAD_INPUT, SELFTEST_FAIL = 0, 2, 3, 5

# ---------------------------------------------------------------- 路径归一化

def norm_path(p):
    """把 ./x、x、x/、a\\b 归一成同一个键。大小写保持原样（不假装不区分大小写）。"""
    if p is None:
        return None
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return os.path.normpath(p) if p else p


# ---------------------------------------------------------------- 冲突判定

def conflict_reason(a, b):
    """返回 a、b 两个 job 不能同批的原因字符串；可以同批则返回 None。

    规则按代价从高到低：
      WW  同一路径被两个 job 写        —— 丢失更新（后写覆盖先写）
      RW  一个写、另一个读同一路径      —— 读到的是「之前」还是「之后」不确定
      EX  两个 job 抢占同一全局资源     —— cwd / env / 端口 / 单一锁文件等
    """
    wa, wb = set(a["writes"]), set(b["writes"])
    ra, rb = set(a["reads"]), set(b["reads"])
    for p in sorted(wa & wb):
        return "WW %s" % p
    for p in sorted(wa & rb):
        return "RW %s（%s 写 / %s 读）" % (p, a["id"], b["id"])
    for p in sorted(wb & ra):
        return "RW %s（%s 写 / %s 读）" % (p, b["id"], a["id"])
    ea, eb = set(a["exclusive"]), set(b["exclusive"])
    for k in sorted(ea & eb):
        return "EX %s" % k
    return None


def normalize_jobs(spec):
    """校验 + 归一化。返回 (jobs, errors)。任何 error 都算 BAD_INPUT。"""
    errors = []
    raw = spec.get("jobs")
    if not isinstance(raw, list):
        return [], ["spec.jobs 必须是数组"]
    if not raw:
        return [], ["spec.jobs 为空（零输入的判定是拒绝，不是通过）"]
    jobs, seen = [], set()
    for i, j in enumerate(raw):
        if not isinstance(j, dict):
            errors.append("jobs[%d] 不是对象" % i)
            continue
        jid = str(j.get("id") or "").strip()
        if not jid:
            errors.append("jobs[%d] 缺 id" % i)
            continue
        if jid in seen:
            errors.append("重复 id: %s" % jid)
            continue
        seen.add(jid)
        reads = j.get("reads") or []
        writes = j.get("writes") or []
        excl = j.get("exclusive") or []
        if not isinstance(reads, list) or not isinstance(writes, list) or not isinstance(excl, list):
            errors.append("%s: reads/writes/exclusive 必须是数组" % jid)
            continue
        est = j.get("est_s")
        if est is not None and not isinstance(est, (int, float)):
            errors.append("%s: est_s 必须是数字" % jid)
            continue
        jobs.append({
            "id": jid,
            "reads": sorted({norm_path(x) for x in reads if x}),
            "writes": sorted({norm_path(x) for x in writes if x}),
            "exclusive": sorted({str(x) for x in excl if x}),
            "est_s": float(est) if est is not None else None,
            "index": len(jobs),
        })
    return jobs, errors


def build_plan(jobs, max_parallel=8):
    """贪心分批：按声明顺序，把 job 放进第一个「不早于其依赖、与批内成员均无冲突、未满」的批。

    贪心不是最优着色，但它有三个我们真正需要的性质：确定性（同输入同输出）、
    保序（同批内按声明顺序）、可解释（每个冲突都能指向具体的一对 job）。

    关键约束（第一版漏了这条，把 render_sitemap 排到了它读的 index.html 生产者之前）：
    批是按序号顺序执行的，所以一个 job 只能落在「比它冲突过的所有前置 job 的批」更晚的批里。
    否则依赖方向被反转——被读者先跑，读到的还是旧文件，而计划看上去完全合法。
    """
    batches, pairs = [], []
    batch_of = {}
    for job in jobs:
        # 声明顺序在它之前、且与它冲突的 job 已经把最低允许批号顶起来了
        min_batch = 0
        for prev_id, bi in batch_of.items():
            prev = next(j for j in jobs if j["id"] == prev_id)
            if conflict_reason(job, prev):
                min_batch = max(min_batch, bi + 1)
        placed = False
        bi = min_batch
        while not placed:
            while len(batches) <= bi:
                batches.append([])
            batch = batches[bi]
            if len(batch) < max_parallel and not any(conflict_reason(job, m) for m in batch):
                batch.append(job)
                batch_of[job["id"]] = bi
                placed = True
            else:
                bi += 1
    # 清掉可能出现的空批（min_batch 顶出来的占位）
    batches = [b for b in batches if b]

    # 冲突对（用于报告与 exit code），不参与分批决策
    for i in range(len(jobs)):
        for k in range(i + 1, len(jobs)):
            r = conflict_reason(jobs[i], jobs[k])
            if r:
                pairs.append({"a": jobs[i]["id"], "b": jobs[k]["id"], "reason": r})
    return batches, pairs


def plan_report(jobs, batches, pairs, max_parallel, claims_parallel):
    ids = [j["id"] for b in batches for j in b]
    accounting_ok = sorted(ids) == sorted(j["id"] for j in jobs)
    est = [j["est_s"] for j in jobs]
    has_est = all(e is not None for e in est)
    serial_s = round(sum(est), 3) if has_est else None
    batched_s = round(sum(max(j["est_s"] for j in b) for b in batches), 3) if has_est else None
    verdict = "SAFE"
    if claims_parallel and len(batches) > 1:
        verdict = "UNSAFE_AS_CLAIMED"
    elif pairs:
        verdict = "NEEDS_SERIALIZATION"
    return {
        "jobs": len(jobs),
        "batches": [{"n": i + 1, "jobs": [j["id"] for j in b], "width": len(b)} for i, b in enumerate(batches)],
        "batch_count": len(batches),
        "max_width": max((len(b) for b in batches), default=0),
        "max_parallel_cap": max_parallel,
        "conflict_pairs": pairs,
        "conflict_count": len(pairs),
        "accounting_ok": accounting_ok,
        "est_serial_s": serial_s,
        "est_batched_s": batched_s,
        "est_speedup": (round(serial_s / batched_s, 2) if serial_s and batched_s else None),
        "claims_parallel": bool(claims_parallel),
        "verdict": verdict,
    }


# ---------------------------------------------------------------- ledger 对账

def load_ledger(path):
    rows, errors = [], []
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception as e:
                errors.append("第 %d 行不是 JSON: %s" % (n, e))
                continue
            for k in ("job", "path", "mode", "start", "end"):
                if k not in r:
                    errors.append("第 %d 行缺字段 %s" % (n, k))
                    break
            else:
                if r["mode"] not in ("read", "write"):
                    errors.append("第 %d 行 mode 必须是 read/write" % n)
                    continue
                if float(r["end"]) < float(r["start"]):
                    errors.append("第 %d 行 end < start" % n)
                    continue
                rows.append({"job": str(r["job"]), "path": norm_path(r["path"]),
                             "mode": r["mode"], "start": float(r["start"]), "end": float(r["end"])})
    return rows, errors


def find_races(rows):
    """同一路径上，至少一方是 write 且时间区间真重叠 → 竞态。

    边界相接（a.end == b.start）不算重叠：先结束再开始是顺序执行，不是并发。
    """
    races, checked = [], 0
    by_path = {}
    for r in rows:
        # 防御性归一化：find_races 可能被直接调用（测试、别的脚本），
        # 不能假设调用方已经过 load_ledger 的归一化。
        by_path.setdefault(norm_path(r["path"]), []).append(r)
    for path, group in by_path.items():
        for i in range(len(group)):
            for k in range(i + 1, len(group)):
                a, b = group[i], group[k]
                if a["mode"] == "read" and b["mode"] == "read":
                    continue
                checked += 1
                if a["start"] < b["end"] and b["start"] < a["end"]:
                    races.append({"path": path, "a": a["job"], "b": b["job"],
                                  "modes": "%s/%s" % (a["mode"], b["mode"]),
                                  "overlap_s": round(min(a["end"], b["end"]) - max(a["start"], b["start"]), 6)})
    return races, checked


# ---------------------------------------------------------------- probe 实测

_WORKER = r'''
import os, sys
path, rounds, reopen, start_at = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1", float(sys.argv[4])
while time.time() < start_at:
    pass
if reopen:
    for _ in range(rounds):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(fd, b"1\n"); os.close(fd)
else:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    for _ in range(rounds):
        os.write(fd, b"1\n")
    os.close(fd)
'''

import time as _t  # noqa: E402  (worker 代码里用到 time，导入放在这里更贴近使用点)


def _run_cell(workdir, workers, rounds, synchronize, reopen):
    path = os.path.join(workdir, "cell_%d%d_%d.log" % (int(synchronize), int(reopen), _t.time_ns() % 10 ** 9))
    with open(path, "w"):
        pass
    start_at = _t.time() + 0.35 if synchronize else 0.0
    wf = os.path.join(workdir, "_worker.py")
    with open(wf, "w", encoding="utf-8") as f:
        f.write("import time\n" + _WORKER)
    ps = [subprocess.Popen([sys.executable, wf, path, str(rounds), "1" if reopen else "0", str(start_at)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(workers)]
    for p in ps:
        p.wait()
    with open(path, "rb") as f:
        got = f.read().count(b"\n")
    os.remove(path)
    return {"sync": synchronize, "reopen_each": reopen, "expect": workers * rounds,
            "got": got, "lost": workers * rounds - got}


def probe(workers=8, rounds=10, trials=3):
    workdir = tempfile.mkdtemp(prefix="parbatch_probe_")
    try:
        cells = []
        for sync in (True, False):
            for reopen in (True, False):
                runs = [_run_cell(workdir, workers, rounds, sync, reopen) for _ in range(trials)]
                cells.append({"sync": sync, "reopen_each": reopen,
                              "trials": [r["got"] for r in runs],
                              "lost": [r["lost"] for r in runs],
                              "lost_total": sum(r["lost"] for r in runs),
                              "expect_total": workers * rounds * trials})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    burst = [c for c in cells if c["sync"]]
    any_lost = sum(c["lost_total"] for c in cells)
    burst_lost = sum(c["lost_total"] for c in burst)
    if burst_lost > 0:
        verdict = "NOT_ATOMIC_UNDER_BURST"
    elif any_lost > 0:
        verdict = "FLAKY"
    else:
        verdict = "ATOMIC"
    return {"os": sys.platform, "workers": workers, "rounds": rounds, "trials": trials,
            "cells": cells, "lost_total": any_lost,
            "burst_lost": burst_lost, "verdict": verdict}


# ---------------------------------------------------------------- selftest

def selftest():
    checks, failed = [], []

    def ck(name, cond):
        checks.append(name)
        if not cond:
            failed.append(name)

    # --- 归一化
    ck("norm ./a == a", norm_path("./a.txt") == norm_path("a.txt"))
    ck("norm a\\b == a/b", norm_path("a\\b.txt") == norm_path("a/b.txt"))
    ck("norm x/ == x", norm_path("x/") == "x")

    def jobs(spec):
        j, e = normalize_jobs({"jobs": spec})
        return j, e

    jj, err = jobs([
        {"id": "r1", "reads": ["x"]},
        {"id": "r2", "reads": ["./x"]},
        {"id": "w1", "writes": ["x"]},
        {"id": "w2", "writes": ["y"]},
        {"id": "e1", "writes": ["m"], "exclusive": ["cwd"]},
        {"id": "e2", "writes": ["n"], "exclusive": ["cwd"]},
    ])
    ck("spec 合法", not err and len(jj) == 6)
    by = {j["id"]: j for j in jj}

    ck("RR 无冲突", conflict_reason(by["r1"], by["r2"]) is None)
    ck("WW 冲突", conflict_reason(by["w1"], by["w1"]).startswith("WW"))
    ck("RW 冲突（读到写的顺序不确定）", "RW" in (conflict_reason(by["w1"], by["r1"]) or ""))
    ck("RW 与参数顺序无关", conflict_reason(by["r1"], by["w1"]) == conflict_reason(by["w1"], by["r1"]).replace("w1 写 / r1 读", "w1 写 / r1 读"))
    ck("不同路径无冲突", conflict_reason(by["w2"], by["r1"]) is None)
    ck("EX 冲突覆盖不同路径", conflict_reason(by["e1"], by["e2"]).startswith("EX"))

    batches, pairs = build_plan(jj, max_parallel=8)
    ids_in_plan = [j["id"] for b in batches for j in b]
    ck("每个 job 恰好出现一次", sorted(ids_in_plan) == sorted(by))
    ck("分批数 > 1（存在冲突）", len(batches) > 1)
    ck("冲突对数量守恒", len(pairs) == sum(1 for i in range(6) for k in range(i + 1, 6) if conflict_reason(jj[i], jj[k])))

    # 互不相干：全部一批
    clean, _ = jobs([{"id": "a", "reads": ["a"]}, {"id": "b", "reads": ["b"]}, {"id": "c", "writes": ["c"]}])
    cb, cp = build_plan(clean, max_parallel=8)
    ck("无冲突全部同批", len(cb) == 1 and len(cb[0]) == 3)
    ck("无冲突对", cp == [])

    # 全写同一文件：必须逐个串行
    same, _ = jobs([{"id": "w%d" % i, "writes": ["same"]} for i in range(5)])
    sb, _sp = build_plan(same, max_parallel=8)
    ck("同文件 5 写 → 5 批", len(sb) == 5)
    ck("同文件每批宽度 1", all(len(b) == 1 for b in sb))

    # 确定性 + 保序
    sb2, _ = build_plan(same, max_parallel=8)
    ck("分批确定", [[j["id"] for j in b] for b in sb] == [[j["id"] for j in b] for b in sb2])
    ck("同批保序", [j["index"] for j in sb[0]] == sorted(j["index"] for j in sb[0]))

    # 依赖方向：链式 a→b→c，批号必须严格递增（第一版曾把消费者排到生产者之前）
    chain, _ = jobs([
        {"id": "produce", "writes": ["mid"], "est_s": 1.0},
        {"id": "consume", "reads": ["mid"], "writes": ["out"], "est_s": 1.0},
        {"id": "publish", "reads": ["out"], "writes": ["final"], "est_s": 1.0},
    ])
    chb, _ = build_plan(chain, max_parallel=8)
    pos = {j["id"]: bi for bi, b in enumerate(chb) for j in b}
    ck("链式依赖严格递增", pos["produce"] < pos["consume"] < pos["publish"])
    ck("链式依赖 = 3 批", len(chb) == 3)
    # 前置 job 混在宽批里时也不能被反序：读取者必须晚于同批其他写者之后的批
    mixed, _ = jobs([
        {"id": "w1", "writes": ["f1"]}, {"id": "w2", "writes": ["f2"]}, {"id": "w3", "writes": ["f3"]},
        {"id": "r1", "reads": ["f2"], "writes": ["g1"]},
    ])
    mb, _ = build_plan(mixed, max_parallel=8)
    mpos = {j["id"]: bi for bi, b in enumerate(mb) for j in b}
    ck("读者晚于其写者", mpos["r1"] > mpos["w2"])
    ck("无关写者和读者同批", mpos["w1"] == mpos["w3"] == 0 and mpos["w2"] == 0)
    ck("宽批未被依赖约束拆碎", len(mb) == 2)

    # max_parallel 上限
    wide, _ = jobs([{"id": "r%d" % i, "reads": ["f%d" % i]} for i in range(7)])
    wb, _ = build_plan(wide, max_parallel=3)
    ck("max_parallel 生效", max(len(b) for b in wb) <= 3 and len(wb) == 3)

    # 报告与裁决
    rep = plan_report(jj, batches, pairs, 8, claims_parallel=False)
    ck("报告 accounting_ok", rep["accounting_ok"] is True)
    ck("NEEDS_SERIALIZATION", rep["verdict"] == "NEEDS_SERIALIZATION")
    rep2 = plan_report(jj, batches, pairs, 8, claims_parallel=True)
    ck("声明一次并行 → UNSAFE", rep2["verdict"] == "UNSAFE_AS_CLAIMED")
    rep3 = plan_report(clean, cb, cp, 8, claims_parallel=True)
    ck("真并行安全 → SAFE", rep3["verdict"] == "SAFE")
    with_est, _ = jobs([{"id": "a", "reads": ["a"], "est_s": 2.0},
                        {"id": "b", "reads": ["b"], "est_s": 3.0},
                        {"id": "c", "writes": ["c"], "est_s": 1.0}])
    eb, ep = build_plan(with_est, max_parallel=8)
    rep4 = plan_report(with_est, eb, ep, 8, claims_parallel=False)
    ck("串行估算 = 求和", rep4["est_serial_s"] == 6.0)
    ck("分批估算 = 各批最大值之和", rep4["est_batched_s"] == 3.0)
    ck("加速比", rep4["est_speedup"] == 2.0)

    # 零输入 / 非法输入
    _, e0 = normalize_jobs({"jobs": []})
    ck("零输入报错", len(e0) == 1)
    _, e1 = normalize_jobs({"jobs": [{"reads": ["a"]}]})
    ck("缺 id 报错", any("缺 id" in x for x in e1))
    _, e2 = normalize_jobs({"jobs": [{"id": "a"}, {"id": "a"}]})
    ck("重复 id 报错", any("重复 id" in x for x in e2))
    _, e3 = normalize_jobs({"jobs": [{"id": "a", "est_s": "slow"}]})
    ck("est_s 类型错报错", any("est_s" in x for x in e3))

    # ledger
    rows_ok = [
        {"job": "a", "path": "x", "mode": "read", "start": 0, "end": 1},
        {"job": "b", "path": "x", "mode": "read", "start": 0.5, "end": 1.5},
    ]
    races, _ = find_races(rows_ok)
    ck("读读重叠不算竞态", races == [])
    rows_adj = [
        {"job": "a", "path": "x", "mode": "write", "start": 0, "end": 1},
        {"job": "b", "path": "x", "mode": "write", "start": 1.0, "end": 2},
    ]
    races2, _ = find_races(rows_adj)
    ck("边界相接不算重叠", races2 == [])
    rows_race = [
        {"job": "a", "path": "./x", "mode": "write", "start": 0, "end": 2},
        {"job": "b", "path": "x", "mode": "write", "start": 1, "end": 3},
    ]
    races3, _ = find_races(rows_race)
    ck("写写重叠是竞态（路径归一后同键）", len(races3) == 1 and races3[0]["overlap_s"] == 1.0)
    rows_rw = [
        {"job": "a", "path": "x", "mode": "write", "start": 0, "end": 2},
        {"job": "b", "path": "x", "mode": "read", "start": 1, "end": 1.5},
    ]
    races4, _ = find_races(rows_rw)
    ck("读落在写区间内 → 竞态", len(races4) == 1)
    rows_disjoint = [
        {"job": "a", "path": "x", "mode": "write", "start": 0, "end": 1},
        {"job": "b", "path": "y", "mode": "write", "start": 0, "end": 1},
    ]
    races5, _ = find_races(rows_disjoint)
    ck("不同路径不报", races5 == [])

    # probe 的判定函数（不真跑，验证分档逻辑）
    def verdict_of(burst_lost, total):
        if burst_lost > 0:
            return "NOT_ATOMIC_UNDER_BURST"
        return "FLAKY" if total > 0 else "ATOMIC"
    ck("probe 分档：突发丢→NOT_ATOMIC", verdict_of(67, 67) == "NOT_ATOMIC_UNDER_BURST")
    ck("probe 分档：非突发丢→FLAKY", verdict_of(0, 5) == "FLAKY")
    ck("probe 分档：全零→ATOMIC", verdict_of(0, 0) == "ATOMIC")

    total = len(checks)
    print("selftest: %d/%d passed" % (total - len(failed), total))
    for f in failed:
        print("  FAIL", f)
    return SELFTEST_FAIL if failed else OK


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="并行工具调用安全分批器 / 竞态探测器")
    sub = ap.add_subparsers(dest="cmd")

    p_plan = sub.add_parser("plan", help="按声明的 reads/writes 分批")
    p_plan.add_argument("--spec", required=True)
    p_plan.add_argument("--max-parallel", type=int, default=8)
    p_plan.add_argument("--claims-parallel", action="store_true",
                        help="spec 声称这些 job 会一次并行提交；若有冲突则 exit 2")
    p_plan.add_argument("--json", action="store_true")

    p_led = sub.add_parser("ledger", help="对账实际运行记录里的重叠写")
    p_led.add_argument("--file", required=True)
    p_led.add_argument("--json", action="store_true")

    p_probe = sub.add_parser("probe", help="实测本机 append 原子性")
    p_probe.add_argument("--workers", type=int, default=8)
    p_probe.add_argument("--rounds", type=int, default=10)
    p_probe.add_argument("--trials", type=int, default=3)

    sub.add_parser("selftest", help="离线断言")

    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "plan":
        if not os.path.exists(args.spec):
            print("错误：spec 文件不存在：%s" % args.spec)
            return BAD_INPUT
        with open(args.spec, "r", encoding="utf-8") as f:
            try:
                spec = json.load(f)
            except Exception as e:
                print("错误：spec 不是合法 JSON：%s" % e)
                return BAD_INPUT
        jobs, errors = normalize_jobs(spec)
        if errors:
            for e in errors:
                print("错误：" + e)
            return BAD_INPUT
        claims = bool(args.claims_parallel or spec.get("claims_parallel"))
        batches, pairs = build_plan(jobs, args.max_parallel)
        rep = plan_report(jobs, batches, pairs, args.max_parallel, claims)
        if not rep["accounting_ok"]:
            print("内部错误：jobs 在分批中丢失或重复")
            return SELFTEST_FAIL
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print("jobs=%d  批数=%d  最大宽度=%d  cap=%d  冲突对=%d"
                  % (rep["jobs"], rep["batch_count"], rep["max_width"],
                     rep["max_parallel_cap"], rep["conflict_count"]))
            for b in rep["batches"]:
                print("  批%d (宽度%d): %s" % (b["n"], b["width"], " ".join(b["jobs"])))
            for p in rep["conflict_pairs"]:
                print("  冲突 %s × %s : %s" % (p["a"], p["b"], p["reason"]))
            if rep["est_serial_s"] is not None:
                print("  估算：串行 %.2fs → 分批 %.2fs（加速 %.2fx）"
                      % (rep["est_serial_s"], rep["est_batched_s"], rep["est_speedup"]))
            print("  裁决：%s" % rep["verdict"])
        return CONFLICT if rep["verdict"] in ("UNSAFE_AS_CLAIMED", "NEEDS_SERIALIZATION") else OK

    if args.cmd == "ledger":
        if not os.path.exists(args.file):
            print("错误：ledger 文件不存在：%s" % args.file)
            return BAD_INPUT
        rows, errors = load_ledger(args.file)
        if errors:
            for e in errors:
                print("错误：" + e)
            return BAD_INPUT
        if not rows:
            print("错误：ledger 里没有任何有效记录（零输入不算通过）")
            return BAD_INPUT
        races, checked = find_races(rows)
        out = {"records": len(rows), "write_pairs_checked": checked,
               "races": races, "race_count": len(races),
               "verdict": "RACE" if races else "NO_OVERLAP"}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print("记录=%d  检查的写相关对=%d  竞态=%d  → %s"
                  % (out["records"], checked, len(races), out["verdict"]))
            for r in races[:20]:
                print("  %s: %s × %s (%s) 重叠 %.4fs"
                      % (r["path"], r["a"], r["b"], r["modes"], r["overlap_s"]))
        return CONFLICT if races else OK

    if args.cmd == "probe":
        rep = probe(args.workers, args.rounds, args.trials)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return CONFLICT if rep["verdict"] == "NOT_ATOMIC_UNDER_BURST" else OK

    ap.print_help()
    return BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
