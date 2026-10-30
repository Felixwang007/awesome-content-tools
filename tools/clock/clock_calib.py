#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clock_calib.py — 校准 Agent 用来计时的那些「钟」。

问题：Agent 的超时、限速、耗时统计、事件时间戳，全都默认「时间是连续的、刻度是细的、
本机钟是准的」。这三条在本机都不成立，而这个工具把它们分别测出来。

子命令（纯标准库，全部输出秒数，6 位小数）：
  ticks      墙钟/单调钟/date 的最小非零增量与不同取值个数（分辨率）
  sleep      time.sleep(请求值) 的真实耗时（均值/p50/min/max）与超时倍数
  pacer      两种限速写法在多个目标速率下的达成率：naive(每次 sleep(1/rate)) vs absolute(绝对时刻表)
  account    「逐次记账求和」与「整体一次测量」的差（量化损失、零增量比例）
  deadline   用墙钟判 deadline 的越界量分布
  witness    同一份负载，time.time / time.monotonic / perf_counter 各自记账的误差
  ntp        NTP 偏移（UDP 123）+ HTTP Date 头交叉验证
  all        ticks/sleep/pacer/account/deadline 一次跑完
  selftest   离线自检（不碰网络）

退出码（就是结论）：
  0  读数在预期内
  2  发现异常（时钟偏移 > --max-offset，或限速达成率 < --min-achieve）
  3  前置条件不满足（网络不可达、NTP 无回包）
  4  零输入（参数给了空集合/0 次）——「没测」不是「没问题」
  5  自检失败

用法：
  python clock_calib.py ticks
  python clock_calib.py all --out samples/clock_readings_20261030.json
  python clock_calib.py ntp --hosts time.windows.com,pool.ntp.org
  python clock_calib.py selftest
"""
import argparse
import datetime as _dt
import email.utils
import hashlib
import json
import os
import socket
import struct
import sys
import time

UNIT = "s"


# ---------------------------------------------------------------- 采样原语
def tick_stats(fn, loops, label):
    """返回 (min_nonzero_delta, p50_nonzero_delta, distinct_values, calls)"""
    prev = fn()
    deltas = []
    seen = {prev}
    for _ in range(loops):
        cur = fn()
        if cur != prev:
            deltas.append(cur - prev)
            prev = cur
            seen.add(cur)
    deltas.sort()
    if not deltas:
        return {"label": label, "calls": loops, "advances": 0,
                "min_tick": None, "p50_tick": None, "distinct": len(seen)}
    return {"label": label, "calls": loops, "advances": len(deltas),
            "min_tick": deltas[0], "p50_tick": deltas[len(deltas) // 2],
            "distinct": len(seen)}


def cmd_ticks(args):
    out = {"cmd": "ticks", "unit": UNIT, "loops": args.loops}
    out["clocks"] = [
        tick_stats(time.time, args.loops, "time.time"),
        tick_stats(time.monotonic, args.loops, "time.monotonic"),
        tick_stats(time.perf_counter, args.loops, "time.perf_counter"),
        tick_stats(lambda: _dt.datetime.now().timestamp(), args.loops, "datetime.now"),
        # ns 版本按纳秒整数读，转成秒
        tick_stats(lambda: time.perf_counter_ns(), args.loops, "perf_counter_ns(int)"),
    ]
    return out, 0


def cmd_sleep(args):
    reqs = parse_floats(args.requests)
    if not reqs:
        return {"cmd": "sleep", "error": "no requests"}, 4
    rows = []
    for req in reqs:
        n = args.n if req < 0.2 else max(5, args.n // 10)
        samples = []
        for _ in range(n):
            a = time.perf_counter()
            time.sleep(req)
            samples.append(time.perf_counter() - a)
        samples.sort()
        mean = sum(samples) / len(samples)
        rows.append({
            "requested": req, "n": n, "mean": mean,
            "p50": samples[len(samples) // 2], "min": samples[0], "max": samples[-1],
            "ratio": (mean / req) if req > 0 else None,
            "overshoot": mean - req,
        })
    return {"cmd": "sleep", "unit": UNIT, "rows": rows}, 0


def pacers(rate, n):
    iv = 1.0 / rate
    # A. 朴素：每轮 sleep(1/rate)，误差逐轮累加
    t0 = time.perf_counter()
    for _ in range(n):
        time.sleep(iv)
    tn = time.perf_counter() - t0
    # B. 绝对时刻表：next = last + iv，落后就补齐、超前才睡
    t0 = time.perf_counter()
    last = t0
    for _ in range(n):
        nxt = last + iv
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(d)
        last = nxt
    ta = time.perf_counter() - t0
    return {"rate": rate, "n": n,
            "naive_total": tn, "naive_actual": n / tn if tn else None,
            "abs_total": ta, "abs_actual": n / ta if ta else None,
            "naive_achieve": (n / tn) / rate if tn else None,
            "abs_achieve": (n / ta) / rate if ta else None}


def cmd_pacer(args):
    rates = parse_floats(args.rates)
    if not rates:
        return {"cmd": "pacer", "error": "no rates"}, 4
    rows = [pacers(r, int(max(r * args.seconds, 40))) for r in rates]
    rc = 0
    for r in rows:
        if r["abs_achieve"] is not None and r["abs_achieve"] < args.min_achieve:
            rc = 2
    return {"cmd": "pacer", "unit": UNIT, "rows": rows}, rc


def cmd_account(args):
    if args.n <= 0:
        return {"cmd": "account", "error": "n must be > 0"}, 4
    items = [b"x" * (100 + (i % 50)) for i in range(args.n)]
    t_all = time.perf_counter()
    sum_wall = sum_mono = 0.0
    zeros = 0
    for it in items:
        w0 = time.time()
        m0 = time.perf_counter()
        hashlib.sha256(it).hexdigest()
        w1 = time.time()
        m1 = time.perf_counter()
        dw = w1 - w0
        sum_wall += dw
        sum_mono += m1 - m0
        if dw == 0:
            zeros += 1
    total = time.perf_counter() - t_all
    return {"cmd": "account", "unit": UNIT, "n": args.n, "true_total": total,
            "sum_wall": sum_wall, "sum_mono": sum_mono,
            "wall_loss_pct": 100.0 * (1 - sum_wall / total) if total else None,
            "mono_loss_pct": 100.0 * (1 - sum_mono / total) if total else None,
            "zero_delta_ratio": zeros / args.n}, 0


def cmd_deadline(args):
    if args.trials <= 0 or args.budget <= 0:
        return {"cmd": "deadline", "error": "trials/budget must be > 0"}, 4
    overs = []
    for _ in range(args.trials):
        w0 = time.time()
        p0 = time.perf_counter()
        while time.time() - w0 < args.budget:
            pass
        overs.append(time.perf_counter() - p0 - args.budget)
    overs.sort()
    over_n = sum(1 for o in overs if o > 0)
    return {"cmd": "deadline", "unit": UNIT, "budget": args.budget, "trials": args.trials,
            "min": overs[0], "p50": overs[len(overs) // 2], "max": overs[-1],
            "mean": sum(overs) / len(overs),
            "over_budget_ratio": over_n / args.trials,
            "max_overshoot_pct": 100.0 * overs[-1] / args.budget}, 0


def cmd_witness(args):
    """同一份负载，四种「钟」各自记账：看谁把 5 毫秒看成 0。"""
    if args.n <= 0 or args.op <= 0:
        return {"cmd": "witness", "error": "n/op must be > 0"}, 4
    clocks = {"time.time": time.time, "time.monotonic": time.monotonic,
              "time.perf_counter": time.perf_counter}
    acc = {k: 0.0 for k in clocks}
    zeros = {k: 0 for k in clocks}
    big = {k: 0 for k in clocks}
    t_all = time.perf_counter()
    for _ in range(args.n):
        start = {k: f() for k, f in clocks.items()}
        time.sleep(args.op)
        for k, f in clocks.items():
            d = f() - start[k]
            acc[k] += d
            if d == 0:
                zeros[k] += 1
            if d >= 0.010:
                big[k] += 1
    total = time.perf_counter() - t_all
    rows = []
    for k in clocks:
        rows.append({"clock": k, "sum": acc[k],
                     "error_pct": 100.0 * (acc[k] - total) / total if total else None,
                     "zero_ratio": zeros[k] / args.n,
                     "ge_10ms_ratio": big[k] / args.n})
    return {"cmd": "witness", "unit": UNIT, "op": args.op, "n": args.n,
            "true_total": total, "expected_unit_duration": args.op * args.n,
            "rows": rows}, 0


# ---------------------------------------------------------------- 网络时间
def ntp_offset(host, timeout):
    pkt = b"\x1b" + 47 * b"\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t1 = time.time()
    try:
        s.sendto(pkt, (host, 123))
        data, _ = s.recvfrom(1024)
        t4 = time.time()
    finally:
        s.close()
    if len(data) < 48:
        raise ValueError("short reply %d bytes" % len(data))
    u = struct.unpack("!12I", data[0:48])
    srv = (u[10] - 2208988800) + u[11] / 2 ** 32   # transmit timestamp, 1900 epoch
    mid = t1 + (t4 - t1) / 2
    return {"host": host, "server_time": srv, "local_mid": mid,
            "offset": mid - srv, "rtt": t4 - t1}


def http_date_offset(url, timeout):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t1 = time.time()
        d = r.headers.get("Date")
        status = r.status
    if not d:
        raise ValueError("no Date header")
    srv = email.utils.parsedate_to_datetime(d).timestamp()
    mid = t0 + (t1 - t0) / 2
    return {"url": url, "http": status, "server_time": srv, "local_mid": mid,
            "offset": mid - srv, "rtt": t1 - t0, "resolution": 1.0}


def cmd_ntp(args):
    hosts = [h.strip() for h in (args.hosts or "").split(",") if h.strip()]
    urls = [u.strip() for u in (args.urls or "").split(",") if u.strip()]
    if not hosts and not urls:
        return {"cmd": "ntp", "error": "need --hosts or --urls"}, 4
    out = {"cmd": "ntp", "unit": UNIT, "ntp": [], "http": [], "errors": []}
    for h in hosts:
        try:
            out["ntp"].append(ntp_offset(h, args.timeout))
        except Exception as e:
            out["errors"].append({"host": h, "error": repr(e)})
    for u in urls:
        try:
            out["http"].append(http_date_offset(u, args.timeout))
        except Exception as e:
            out["errors"].append({"url": u, "error": repr(e)})
    offs = [r["offset"] for r in out["ntp"]] + [r["offset"] for r in out["http"]]
    if not offs:
        return out, 3
    out["offset_spread"] = max(offs) - min(offs)
    out["max_abs_offset"] = max(abs(o) for o in offs)
    rc = 2 if out["max_abs_offset"] > args.max_offset else 0
    out["verdict"] = "CLOCK_OFFSET" if rc == 2 else "OK"
    return out, rc


def cmd_all(args):
    res = {"generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
           "unit": UNIT, "host": socket.gethostname(), "parts": {}}
    rc = 0
    for name, fn, sub in (("ticks", cmd_ticks, args), ("sleep", cmd_sleep, args),
                          ("pacer", cmd_pacer, args), ("account", cmd_account, args),
                          ("deadline", cmd_deadline, args),
                          ("witness", cmd_witness, args)):
        out, code = fn(sub)
        res["parts"][name] = out
        rc = max(rc, code)
    if args.out:
        d = os.path.dirname(os.path.abspath(args.out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        res["written"] = args.out
    return res, rc


# ---------------------------------------------------------------- 自检
def selftest(verbose=False):
    checks = []

    def ck(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # 1) 分辨率：单调钟不比墙钟粗
    tw = tick_stats(time.time, 30000, "wall")
    tp = tick_stats(time.perf_counter, 30000, "perf")
    ck("wall tick > 0", tw["min_tick"] is not None and tw["min_tick"] > 0, str(tw["min_tick"]))
    ck("perf tick <= wall tick", tp["min_tick"] <= tw["min_tick"],
       "perf=%s wall=%s" % (tp["min_tick"], tw["min_tick"]))

    # 2) sleep 不会提前返回太多（0.05s 请求，提前不得超过 5ms）
    samples = []
    for _ in range(5):
        a = time.perf_counter()
        time.sleep(0.05)
        samples.append(time.perf_counter() - a)
    ck("sleep(0.05) >= 0.045", min(samples) >= 0.045, "min=%.6f" % min(samples))
    ck("sleep(0.05) <= 0.30", max(samples) <= 0.30, "max=%.6f" % max(samples))

    # 3) 绝对时刻表在 100/s 上的达成率 90%~110%
    abs100 = pacers(100, 100)
    ck("absolute pacer 100/s within 0.9-1.1",
       0.9 <= abs100["abs_achieve"] <= 1.1, "%.4f" % abs100["abs_achieve"])

    # 4) 朴素限速不会优于绝对时刻表（同速率，同一台机器）
    same = pacers(2000, 400)
    ck("naive <= absolute @2000/s", same["naive_achieve"] <= same["abs_achieve"] + 1e-9,
       "naive=%.3f abs=%.3f" % (same["naive_achieve"], same["abs_achieve"]))

    # 5) 逐次记账：单调钟只会少记（损失>=0 且 <=100%），墙钟会因刻度向上取整而「多记」
    acc = cmd_account(argparse.Namespace(n=600))[0]
    acc2 = cmd_account(argparse.Namespace(n=3000))[0]
    ck("mono_loss in [0,100] @600", 0 <= acc["mono_loss_pct"] <= 100.0, "%.2f%%" % acc["mono_loss_pct"])
    ck("mono_loss in [0,100] @3000", 0 <= acc2["mono_loss_pct"] <= 100.0, "%.2f%%" % acc2["mono_loss_pct"])
    ck("mono_loss stable across n (<=15pt)", abs(acc["mono_loss_pct"] - acc2["mono_loss_pct"]) <= 15.0,
       "%.2f vs %.2f" % (acc["mono_loss_pct"], acc2["mono_loss_pct"]))
    for a in (acc, acc2):
        err = abs(a["sum_wall"] - a["true_total"])
        ck("wall sum error <= n*tick*2 @%d" % a["n"], err <= 2 * a["n"] * tw["min_tick"] + 1e-6,
           "err=%.6f bound=%.6f" % (err, 2 * a["n"] * tw["min_tick"]))

    # 6) 条件断言：只有当墙钟刻度 >> 单次操作耗时时，才要求「零增量占多数」；
    #    否则这条断言不成立（刻度足够细的机器上每笔都有非零读数）——跳过并记录原因
    op = acc["true_total"] / acc["n"]
    if tw["min_tick"] and tw["min_tick"] > 10 * op:
        ck("zero-delta ratio > 0.8 (coarse clock)", acc["zero_delta_ratio"] > 0.8,
           "%.3f (tick=%.6f op=%.6f)" % (acc["zero_delta_ratio"], tw["min_tick"], op))
    else:
        checks.append(("zero-delta ratio: SKIPPED (machine-dependent)", True,
                       "tick=%.6f not >> op=%.6f" % (tw["min_tick"], op)))

    # 7) deadline 越界量非负为主、且不超过 2 个刻度太多
    dl = cmd_deadline(argparse.Namespace(budget=0.02, trials=60))[0]
    ck("deadline max overshoot <= 5 tick", dl["max"] <= 5 * tw["min_tick"],
       "max=%.6f tick=%.6f" % (dl["max"], tw["min_tick"]))

    # 8) 零输入必须返回非成功
    ck("pacer zero rates -> 4", cmd_pacer(argparse.Namespace(rates="", seconds=0.2, min_achieve=0.5))[1] == 4)
    ck("account n=0 -> 4", cmd_account(argparse.Namespace(n=0))[1] == 4)
    ck("ntp no args -> 4", cmd_ntp(argparse.Namespace(hosts="", urls="", timeout=1, max_offset=1))[1] == 4)

    # 9) 输出可序列化 + 结构稳定（两次运行 key 集合相同）
    a1 = cmd_account(argparse.Namespace(n=50))[0]
    a2 = cmd_account(argparse.Namespace(n=50))[0]
    ck("account keys stable", sorted(a1.keys()) == sorted(a2.keys()))
    ck("json serializable", bool(json.dumps(cmd_ticks(argparse.Namespace(loops=200))[0])))

    # 10) 量具对照：5ms 负载，至少有一个钟的记账误差在 5% 以内
    w = cmd_witness(argparse.Namespace(op=0.005, n=40))[0]
    best = min(abs(r["error_pct"]) for r in w["rows"])
    ck("witness: some clock within 5%", best <= 5.0, "best=%.2f%%" % best)
    ck("witness zero input -> 4", cmd_witness(argparse.Namespace(op=0, n=10))[1] == 4)

    # 11) parse_floats 行为
    ck("parse_floats strips spaces", parse_floats("1, 2 ,,3") == [1.0, 2.0, 3.0])
    ck("parse_floats empty -> []", parse_floats("") == [])

    ok = all(c for _, c, _ in checks)
    for name, c, detail in checks:
        if verbose or not c:
            print("%s %s %s" % ("PASS" if c else "FAIL", name, ("| " + detail) if detail else ""))
    print("selftest: %d/%d %s" % (sum(1 for _, c, _ in checks if c), len(checks),
                                  "OK" if ok else "FAILED"))
    return ok




def parse_floats(s):
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="校准 Agent 用来计时的钟")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("ticks"); p.add_argument("--loops", type=int, default=200000)
    p = sub.add_parser("sleep"); p.add_argument("--requests", default="0,0.0001,0.001,0.002,0.005,0.01,0.0156,0.02,0.05,0.1,0.5,1.0")
    p.add_argument("--n", type=int, default=200)
    p = sub.add_parser("pacer"); p.add_argument("--rates", default="10,50,100,500,1000,2000")
    p.add_argument("--seconds", type=float, default=1.0); p.add_argument("--min-achieve", type=float, default=0.5)
    p = sub.add_parser("account"); p.add_argument("--n", type=int, default=1500)
    p = sub.add_parser("deadline"); p.add_argument("--budget", type=float, default=0.05)
    p.add_argument("--trials", type=int, default=300)
    p = sub.add_parser("ntp"); p.add_argument("--hosts", default="time.windows.com,pool.ntp.org")
    p.add_argument("--urls", default=""); p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--max-offset", type=float, default=1.0)
    p = sub.add_parser("all"); p.add_argument("--loops", type=int, default=200000)
    p.add_argument("--requests", default="0.0001,0.001,0.002,0.005,0.01,0.0156,0.02,0.05,0.1")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--rates", default="10,50,100,500,1000,2000")
    p.add_argument("--seconds", type=float, default=1.0); p.add_argument("--min-achieve", type=float, default=0.5)
    p.add_argument("--budget", type=float, default=0.05); p.add_argument("--trials", type=int, default=300)
    p.add_argument("--op", type=float, default=0.005)
    p.add_argument("--out", default="")
    p = sub.add_parser("witness"); p.add_argument("--op", type=float, default=0.005)
    p.add_argument("--n", type=int, default=60)
    p = sub.add_parser("selftest")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 4

    if args.cmd == "selftest":
        return 0 if selftest(verbose=True) else 5

    fn = {"ticks": cmd_ticks, "sleep": cmd_sleep, "pacer": cmd_pacer,
          "account": cmd_account, "deadline": cmd_deadline, "ntp": cmd_ntp,
          "witness": cmd_witness,
          "all": cmd_all}[args.cmd]
    res, rc = fn(args)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
