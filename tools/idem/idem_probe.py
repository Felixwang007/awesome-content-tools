#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""idem_probe.py — AI Agent 副作用幂等性探针（纯标准库，零依赖）

真实 HTTP 服务 + 真实 JSONL 账本，测量「重试」在五种故障相位下到底重复了几笔副作用。

子命令
  probe     跑策略矩阵（真实 socket、真实文件），输出表格 + JSON + 对账
  classify  对一组离线读数 {status, exc} 做「该不该重试」的判定复算
  selftest  自检（不联网、不开端口，只测纯逻辑与可注入的调用循环）

退出码
  0  所有场景与声明一致（有重复的场景重复数符合预期，声明无重复的场景为 0）
  2  有场景与声明不符（例如号称幂等却出现了重复扣款）
  3  前置条件不满足（端口/临时目录/socket 不可用）
  4  零输入（probe 没有跑任何场景）
  5  自检失败

设计要点（写在代码里，方便复核）
  1. 幂等键必须由「意图」推导（intent_key），不能由「本次尝试」推导（per_attempt 是 bug）。
  2. 内容哈希去重（content_key）会把两笔真实的同额订单合并成一笔 —— 那是少收钱，不是幂等。
  3. check-then-act（key_racy）在并发下会裂开：读在锁外 + 落账前那段时间就是窗口。
  4. 键存储与副作用必须同一次写入落地：进程重启丢 store 等于丢键。
  5. 对账必须看账本（谁付了钱），不能看客户端自报的 success。
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# 纯逻辑层：规范化 / 键推导 / 错误分类 / 对账
# --------------------------------------------------------------------------- #


def canon(obj):
    """规范化：键排序、字符串去首尾空白、列表保序。幂等键的前置条件。"""
    if isinstance(obj, dict):
        return {k: canon(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [canon(v) for v in obj]
    if isinstance(obj, str):
        return obj.strip()
    return obj


def _digest(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def intent_key(intent, salt=""):
    """由「意图」推导的幂等键：同一意图的任何一次重试都得到同一个键。"""
    payload = json.dumps(canon(intent), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest((salt + "|" if salt else "") + payload)


def content_key(intent):
    """内容去重键 —— 故意只看金额，用来演示「同额不同单」被误合并。"""
    return _digest(json.dumps(canon(intent.get("amount")), sort_keys=True))


def attempt_key(intent, attempt, salt=""):
    """每次尝试一个新键 —— 用来演示「有键等于没键」。"""
    return _digest("%s|%s|%d" % (salt, intent_key(intent, salt), attempt))


def should_retry(status, exc=None):
    """返回 (kind, retry, why)。只有「结果未知」的错误才值得重试，且必须携带同一幂等键。"""
    if exc is not None:
        name = type(exc).__name__
        if name in ("HTTPException",) or isinstance(exc, socket.timeout):
            return ("transport-timeout", True, "exc:" + name)
        return ("transport", True, "exc:" + name)
    if status is None:
        return ("unknown", True, "no-status")
    if 200 <= status < 300:
        return ("ok", False, "2xx")
    if 400 <= status < 500:
        if status in (408, 409, 425, 429):
            return ("retryable-4xx", True, "status:%d" % status)
        return ("client-error", False, "status:%d" % status)
    return ("server-error", True, "status:%d" % status)


class Ledger:
    """副作用账本：一行一笔已提交的扣款。真实文件，追加写。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        open(path, "w", encoding="utf-8").close()

    def append(self, rec):
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with self.lock:
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")

    def entries(self):
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def reconcile(entries, expected_intents):
    """对账：重复（多收）与缺失（少收）各算一次。三个数缺一不可。"""
    per = Counter(e.get("intent") for e in entries)
    duplicates = sum(max(0, c - 1) for c in per.values())
    distinct = len(per)
    lost = sum(1 for i in expected_intents if per.get(i, 0) == 0)
    return {
        "committed": len(entries),
        "distinct_intents": distinct,
        "duplicates": duplicates,
        "lost": lost,
    }


# --------------------------------------------------------------------------- #
# 服务端：一个会「提交后不吭声」的扣款服务
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        srv.request_count += 1
        intent = body.get("intent") or {}
        iid = intent.get("id")
        key = body.get("key") or ""

        with srv.counter_lock:
            srv.attempts[iid] = srv.attempts.get(iid, 0) + 1
            attempt_no = srv.attempts[iid]

        # 故障相位一：提交之前失败（真实世界里最常见的 503 / 连接被拒）
        if srv.fault == "fail_first" and attempt_no == 1:
            self._send(503, {"error": "upstream unavailable"})
            return

        result = _apply_policy(srv, intent, key)

        # 故障相位二：提交之后丢回包（网关超时 / 连接被切断 / 客户端超时）
        drop = srv.fault == "drop_always" or (srv.fault == "drop_first" and attempt_no == 1)
        if drop:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if srv.ack_delay:
            time.sleep(srv.ack_delay)
        self._send(200, result)

    def _send(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass


def _apply_policy(srv, intent, key):
    """四种去重策略。返回值同时告诉调用方这次是「新提交」还是「重放」。"""
    policy = srv.policy
    if policy == "naive":
        rec = srv.commit(intent, key)
        return {"replay": False, "receipt": rec}

    if policy == "key_atomic":
        with srv.store_lock:
            if key in srv.store:
                return {"replay": True, "receipt": srv.store[key]}
            rec = srv.commit(intent, key)
            srv.store[key] = rec
            return {"replay": False, "receipt": rec}

    if policy == "key_racy":
        if key in srv.store:  # 读在锁外，且落账前还睡一会 = 典型 check-then-act 窗口
            return {"replay": True, "receipt": srv.store[key]}
        time.sleep(srv.precheck_delay)
        rec = srv.commit(intent, key)
        srv.store[key] = rec
        return {"replay": False, "receipt": rec}

    if policy == "key_ttl":
        # 幂等窗口比重试间隔还短 —— 键还在，但已经过期
        with srv.store_lock:
            hit = srv.store.get(key)
            if hit and (time.time() - hit["at"]) <= srv.ttl:
                return {"replay": True, "receipt": hit["rec"]}
            rec = srv.commit(intent, key)
            srv.store[key] = {"rec": rec, "at": time.time()}
            return {"replay": False, "receipt": rec}

    if policy == "content_hash":
        ck = content_key(intent)
        if ck in srv.store:
            return {"replay": True, "receipt": srv.store[ck]}
        rec = srv.commit(intent, ck)
        srv.store[ck] = rec
        return {"replay": False, "receipt": rec}

    raise ValueError("unknown policy: %r" % policy)


def make_server(ledger, policy="naive", fault="none", ack_delay=0.0, precheck_delay=0.0, ttl=1.0):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.ledger = ledger
    srv.policy = policy
    srv.fault = fault
    srv.ack_delay = ack_delay
    srv.precheck_delay = precheck_delay
    srv.ttl = ttl
    srv.store = {}
    srv.store_lock = threading.Lock()
    srv.counter_lock = threading.Lock()
    srv.attempts = {}
    srv.request_count = 0

    def commit(intent, key):
        rec = {
            "intent": intent.get("id"),
            "amount": intent.get("amount"),
            "key": key,
            "receipt": "rcpt_" + os.urandom(4).hex(),
            "t": round(time.time(), 6),
        }
        ledger.append(rec)
        return rec

    srv.commit = commit
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return srv


# --------------------------------------------------------------------------- #
# 客户端：会重试的 Agent 工具调用
# --------------------------------------------------------------------------- #


def post_once(port, body, timeout):
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/charge", body=raw, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    finally:
        conn.close()


def run_intent(port, intent, key_mode="stable", attempts=3, timeout=0.3,
               salt="", backoff=0.02, poster=None):
    """一个意图走完重试循环。返回每次尝试的读数（key 是否稳定都在里面）。"""
    poster = poster or post_once
    rows = []
    for i in range(1, attempts + 1):
        if key_mode == "stable":
            key = intent_key(intent, salt)
        elif key_mode == "per_attempt":
            key = attempt_key(intent, i, salt)
        elif key_mode == "content":
            key = content_key(intent)
        elif key_mode == "none":
            key = ""
        else:
            raise ValueError("unknown key_mode: %r" % key_mode)

        body = {"intent": intent, "key": key, "attempt": i}
        try:
            status, data = poster(port, body, timeout)
        except Exception as exc:  # 连接被切/超时/对端关闭
            status, data = None, b""
            kind, retry, why = should_retry(None, exc)
        else:
            kind, retry, why = should_retry(status, None)

        row = {"attempt": i, "key": key, "status": status,
               "kind": kind, "retry": retry, "why": why}
        try:
            row["replay"] = json.loads(data.decode("utf-8")).get("replay") if data else None
        except Exception:
            row["replay"] = None
        rows.append(row)
        if not retry:
            break
        time.sleep(backoff)
    return rows


# --------------------------------------------------------------------------- #
# 场景矩阵
# --------------------------------------------------------------------------- #


def scenario(name, policy, key_mode, intents, fault="none", attempts=3,
             timeout=0.3, ack_delay=0.0, precheck_delay=0.0, backoff=0.02,
             concurrency=1, expect_dup=None, expect_lost=None, workdir=None, ttl=1.0):
    ledger = Ledger(os.path.join(workdir, "ledger_%s.jsonl" % name))
    srv = make_server(ledger, policy=policy, fault=fault,
                      ack_delay=ack_delay, precheck_delay=precheck_delay, ttl=ttl)
    port = srv.server_address[1]
    barrier = threading.Barrier(concurrency) if concurrency > 1 else None

    def worker(intent):
        if barrier is not None:
            barrier.wait()
        return run_intent(port, intent, key_mode=key_mode, attempts=attempts,
                          timeout=timeout, salt="s1", backoff=backoff)

    threads, results = [], []
    try:
        if concurrency == 1:
            for intent in intents:
                results.append(worker(intent))
        else:
            for intent in intents:
                ts = [threading.Thread(target=lambda it=intent: results.append(worker(it)))
                      for _ in range(concurrency)]
                threads = ts
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
        time.sleep(0.15)  # 等服务端把最后一行落盘
    finally:
        srv.shutdown()
        srv.server_close()

    entries = ledger.entries()
    rec = reconcile(entries, [i["id"] for i in intents])
    attempts_total = sum(len(r) for r in results)
    rec.update({
        "name": name,
        "policy": policy,
        "key_mode": key_mode,
        "fault": fault,
        "intents": len(intents),
        "attempts": attempts_total,
        "server_requests": srv.request_count,
        "concurrency": concurrency,
        "declared_dup": expect_dup,
        "declared_lost": expect_lost,
        "ok": (expect_dup is None or rec["duplicates"] == expect_dup)
              and (expect_lost is None or rec["lost"] == expect_lost)
              and rec["committed"] == len(entries),
    })
    return rec


def scenario_restart(workdir):
    """键存储随进程重启丢失：同一意图、同一键，第二次仍然再扣一笔。"""
    ledger = Ledger(os.path.join(workdir, "ledger_restart.jsonl"))
    intents = build_intents(1, prefix="rb")
    srv1 = make_server(ledger, policy="key_atomic", fault="drop_always")
    run_intent(srv1.server_address[1], intents[0], key_mode="stable", attempts=1, salt="s1")
    srv1.shutdown()
    srv1.server_close()
    # 新进程：内存里的键全没了，账本还在
    srv2 = make_server(ledger, policy="key_atomic", fault="none")
    run_intent(srv2.server_address[1], intents[0], key_mode="stable", attempts=1, salt="s1")
    srv2.shutdown()
    srv2.server_close()
    time.sleep(0.1)
    entries = ledger.entries()
    rec = reconcile(entries, [i["id"] for i in intents])
    rec.update({"name": "restart_store_lost", "policy": "key_atomic", "key_mode": "stable",
                "fault": "drop_always+restart", "intents": 1, "attempts": 2,
                "server_requests": 2, "concurrency": 1,
                "declared_dup": 1, "declared_lost": 0})
    rec["ok"] = rec["duplicates"] == 1 and rec["lost"] == 0
    return rec


def build_intents(n, amount=1000, prefix="ord"):
    return [{"id": "%s-%03d" % (prefix, i), "amount": amount, "item": "pro-plan"} for i in range(1, n + 1)]


def probe(workdir):
    cases = [
        # 1) 提交前失败 + 朴素重试：安全（错的那次没落账）
        dict(name="pre_fail_naive", policy="naive", key_mode="none",
             intents=build_intents(3), fault="fail_first", attempts=3,
             expect_dup=0, expect_lost=0),
        # 2) 提交后丢回包 + 朴素重试：每次重试都多扣一笔
        dict(name="post_drop_naive", policy="naive", key_mode="none",
             intents=build_intents(3), fault="drop_first", attempts=3,
             expect_dup=3, expect_lost=0),
        # 3) 提交后丢回包 + 意图键 + 服务端原子去重：一笔
        dict(name="post_drop_key_atomic", policy="key_atomic", key_mode="stable",
             intents=build_intents(3), fault="drop_first", attempts=3,
             expect_dup=0, expect_lost=0),
        # 4) 有键但每次尝试换一个键 = 没键
        dict(name="post_drop_key_per_attempt", policy="key_atomic", key_mode="per_attempt",
             intents=build_intents(3), fault="drop_first", attempts=3,
             expect_dup=3, expect_lost=0),
        # 5) 内容哈希去重：两笔同额不同单被并成一笔（少收钱）
        dict(name="content_hash_two_orders", policy="content_hash", key_mode="stable",
             intents=build_intents(2), fault="none", attempts=2,
             expect_dup=0, expect_lost=1),
        # 6) check-then-act：并发同键，10 轮
        dict(name="race_racy", policy="key_racy", key_mode="stable",
             intents=build_intents(10, prefix="race"), fault="none", attempts=1,
             concurrency=2, precheck_delay=0.05, expect_dup=None, expect_lost=0),
        # 7) 原子去重：同样并发，必须 0
        dict(name="race_atomic", policy="key_atomic", key_mode="stable",
             intents=build_intents(10, prefix="race2"), fault="none", attempts=1,
             concurrency=2, precheck_delay=0.05, expect_dup=0, expect_lost=0),
        # 8) 只发一次不重试（at-most-once）：提交前失败就是真丢单
        dict(name="at_most_once", policy="naive", key_mode="stable",
             intents=build_intents(3), fault="fail_first", attempts=1,
             expect_dup=0, expect_lost=3),
        # 9) 幂等窗口（0.1s）比重试退避（0.35s）短：键过期 = 没键
        dict(name="ttl_shorter_than_backoff", policy="key_ttl", key_mode="stable",
             intents=build_intents(3), fault="drop_first", attempts=2,
             backoff=0.35, ttl=0.1, expect_dup=3, expect_lost=0),
    ]

    rows = []
    t0 = time.time()
    for case in cases:
        rows.append(scenario(workdir=workdir, **case))
    rows.append(scenario_restart(workdir))
    elapsed = time.time() - t0

    print("%-26s %-10s %-9s %5s %6s %5s %5s %5s" %
          ("scenario", "policy", "key", "req", "charg", "uniq", "dup", "lost"))
    print("-" * 82)
    for r in rows:
        print("%-26s %-10s %-9s %5d %6d %5d %5d %5d  %s" %
              (r["name"], r["policy"], r["key_mode"], r["attempts"],
               r["committed"], r["distinct_intents"], r["duplicates"],
               r["lost"], "OK" if r["ok"] else "MISMATCH"))
    print("-" * 82)
    print("wall=%.3fs  ledgers=%s" % (elapsed, workdir))

    mismatched = [r["name"] for r in rows if not r["ok"]]
    if mismatched:
        print("MISMATCH: " + ", ".join(mismatched))
        return 2, rows
    return 0, rows


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def selftest():
    checks = []

    def ck(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # 键推导
    a = {"id": "o-1", "amount": 1000, "item": " pro-plan "}
    b = {"item": "pro-plan", "amount": 1000, "id": "o-1"}
    ck("intent_key 与键顺序无关", intent_key(a) == intent_key(b))
    ck("intent_key 去首尾空白", intent_key({"id": " x "}) == intent_key({"id": "x"}))
    ck("intent_key 稳定", intent_key(a) == intent_key(a))
    ck("改金额则键变", intent_key(a) != intent_key({"id": "o-1", "amount": 1001, "item": "pro-plan"}))
    ck("salt 参与推导", intent_key(a) != intent_key(a, "s2"))
    ck("per_attempt 每次不同", attempt_key(a, 1) != attempt_key(a, 2))
    ck("per_attempt 与 stable 不同", attempt_key(a, 1) != intent_key(a))
    ck("content_key 同额相等（这是缺陷）", content_key(a) == content_key({"id": "o-2", "amount": 1000}))
    ck("content_key 异额不等", content_key(a) != content_key({"id": "o-1", "amount": 999}))

    # 错误分类
    ck("200 不重试", should_retry(200)[1] is False)
    ck("400 不重试", should_retry(400)[1] is False)
    ck("404 不重试", should_retry(404)[1] is False)
    ck("408 重试", should_retry(408)[1] is True)
    ck("429 重试", should_retry(429)[1] is True)
    ck("503 重试", should_retry(503)[1] is True)
    ck("超时重试", should_retry(None, socket.timeout("t"))[1] is True)
    ck("连接被切重试", should_retry(None, ConnectionResetError("r"))[1] is True)
    ck("无状态重试", should_retry(None)[1] is True)

    # 对账
    ent = [{"intent": "a"}, {"intent": "a"}, {"intent": "b"}]
    r = reconcile(ent, ["a", "b", "c"])
    ck("重复=总条数-意图数", r["duplicates"] == 1)
    ck("缺失=期望里没出现的", r["lost"] == 1)
    ck("已提交条数", r["committed"] == 3)
    ck("空账本全零", reconcile([], [])["duplicates"] == 0 and reconcile([], [])["lost"] == 0)
    ck("全丢=lost 等于意图数", reconcile([], ["a", "b"])["lost"] == 2)

    # 账本 I/O
    tmp = tempfile.mkdtemp(prefix="idem_selftest_")
    lg = Ledger(os.path.join(tmp, "l.jsonl"))
    lg.append({"intent": "订单-甲", "amount": 1})
    lg.append({"intent": "订单-乙", "amount": 2})
    ents = lg.entries()
    ck("账本往返中文不丢", ents[0]["intent"] == "订单-甲")
    ck("账本行数一致", len(ents) == 2)

    # 重试循环（注入 poster，不开端口）
    seen = []

    def stub_400(port, body, timeout):
        seen.append(body["key"])
        return 400, b'{"error":"bad"}'

    rows = run_intent(0, {"id": "o-9"}, key_mode="stable", attempts=3, poster=stub_400)
    ck("4xx 一次就停", len(rows) == 1)
    ck("4xx 不重试标记", rows[0]["retry"] is False)

    def stub_timeout(port, body, timeout):
        seen.append(body["key"])
        raise socket.timeout("read timed out")

    rows = run_intent(0, {"id": "o-9"}, key_mode="stable", attempts=3,
                      timeout=0.01, backoff=0.0, poster=stub_timeout)
    ck("超时重试到上限", len(rows) == 3)
    ck("重试沿用同一幂等键", len(set(r["key"] for r in rows)) == 1)
    ck("超时被归类", rows[0]["kind"] == "transport-timeout")

    rows = run_intent(0, {"id": "o-9"}, key_mode="per_attempt", attempts=3,
                      timeout=0.01, backoff=0.0, poster=stub_timeout)
    ck("per_attempt 三次三个键", len(set(r["key"] for r in rows)) == 3)

    failed = [n for n, ok, _ in checks if not ok]
    for name, ok, detail in checks:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    print("-" * 60)
    print("selftest: %d/%d" % (len(checks) - len(failed), len(checks)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 5
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI Agent 副作用幂等性探针")
    ap.add_argument("cmd", choices=["probe", "classify", "selftest"])
    ap.add_argument("--workdir", default=None, help="账本落地目录（默认临时目录）")
    ap.add_argument("--input", default=None, help="classify 的读数 JSON 文件")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "classify":
        if not args.input:
            print("classify 需要 --input <readings.json>", file=sys.stderr)
            return 3
        with open(args.input, encoding="utf-8") as f:
            readings = json.load(f)
        if not readings:
            print("零输入：没有读数可判", file=sys.stderr)
            return 4
        for r in readings:
            kind, retry, why = should_retry(r.get("status"),
                                            socket.timeout() if r.get("exc") == "timeout" else None)
            print("%-24s status=%-4s -> %-18s retry=%-5s %s" %
                  (r.get("name", "-"), r.get("status"), kind, retry, why))
        return 0

    workdir = args.workdir or tempfile.mkdtemp(prefix="idem_probe_")
    os.makedirs(workdir, exist_ok=True)
    try:
        code, rows = probe(workdir)
    except OSError as exc:
        print("前置条件不满足：%s" % exc, file=sys.stderr)
        return 3
    if not rows:
        return 4
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
