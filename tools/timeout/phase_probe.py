#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phase_probe.py — 把「超时」拆成五个阶段：DNS / TCP / TLS / 首字节 / 整包。

纯标准库，不装任何东西。存在的理由：一个 timeout=30 同时代表五种完全不同的
故障，而修复动作、重试策略、告警等级各不相同。

子命令
    probe URL [--timeout S] [--read-cap N] [--json]
        逐阶段计时，给六态判定。
    classify readings.json
        采样与判定分离：从已有读数离线复判（可复算，不重新发请求）。
    selftest
        起本地服务端复现七种故障形态，断言判定结果。

退出码
    0  UP / SLOW_UP            （有进展）
    2  STALLED_* / TRUNCATED   （连上了，但没有进展完）
    3  DEAD_*                  （根本没连上）
    4  零输入                  （「没输入」不是「输入干净」）
    5  自检失败
"""

import argparse
import json
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UP = "UP"
SLOW_UP = "SLOW_UP"
STALLED_NO_BYTES = "STALLED_NO_BYTES"
STALLED_PARTIAL = "STALLED_PARTIAL"
TRUNCATED = "TRUNCATED"
TLS_FAIL = "TLS_FAIL"
CONNECT_REFUSED = "CONNECT_REFUSED"
CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
DNS_FAIL = "DNS_FAIL"

PROGRESS_STATES = {UP, SLOW_UP}
CONNECTED_STATES = {UP, SLOW_UP, STALLED_NO_BYTES, STALLED_PARTIAL, TRUNCATED, TLS_FAIL}
DEAD_STATES = {CONNECT_REFUSED, CONNECT_TIMEOUT, DNS_FAIL}


def exit_code_for(state):
    if state in PROGRESS_STATES:
        return 0
    if state in DEAD_STATES:
        return 3
    return 2


# ---------------------------------------------------------------- 探测

def _now():
    return time.perf_counter()


def _build_request(parts):
    host = parts.hostname or ""
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    host_hdr = host if port in (None, default) else "%s:%d" % (host, port)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "User-Agent: phase-probe/1.0\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n\r\n" % (path, host_hdr)
    )
    return req.encode("ascii", "replace")


def _parse_head(buf):
    """从已收到的字节里解析状态行与头部；不完整返回 None。"""
    idx = buf.find(b"\r\n\r\n")
    if idx < 0:
        return None
    head = buf[:idx].decode("iso-8859-1", "replace")
    lines = head.split("\r\n")
    status = None
    proto = lines[0].split(" ")[0] if lines else ""
    try:
        status = int(lines[0].split(" ")[1])
    except (IndexError, ValueError):
        status = None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    body_have = len(buf) - (idx + 4)
    return {"proto": proto, "status": status, "headers": headers, "body_have": body_have}


def probe(url, timeout=10.0, read_cap=65536, slow_threshold=1.0):
    """返回 dict：每阶段秒数 + state + 证据字段。

    elapsed_s 是探测自己真实等了多久（与相位无关）。零字节故障时各相位都可能是
    None，若没有 elapsed_s，预算核算就只能靠猜——第一版漏了这个字段。
    """
    t_start = _now()
    out = _probe_impl(url, timeout, read_cap, slow_threshold)
    out["elapsed_s"] = round(_now() - t_start, 4)
    return out


def _probe_impl(url, timeout=10.0, read_cap=65536, slow_threshold=1.0):
    """返回 dict：每阶段秒数 + state + 证据字段。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http/https: %r" % url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    out = {
        "url": url,
        "host": host,
        "port": port,
        "timeout": timeout,
        "dns_s": None,
        "tcp_s": None,
        "tls_s": None,
        "send_s": None,
        "ttfb_s": None,
        "body_s": None,
        "bytes_recv": 0,
        "status": None,
        "declared_len": None,
        "deepest_phase": None,
        "state": None,
        "note": "",
    }

    # 1) DNS
    t0 = _now()
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        out["dns_s"] = round(_now() - t0, 4)
        out["state"] = DNS_FAIL
        out["deepest_phase"] = "dns"
        out["note"] = "getaddrinfo: %s" % e
        return out
    out["dns_s"] = round(_now() - t0, 4)
    family, socktype, proto, _, addr = infos[0]

    # 2) TCP
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    t0 = _now()
    try:
        sock.connect(addr)
    except socket.timeout:
        out["tcp_s"] = round(_now() - t0, 4)
        out["state"] = CONNECT_TIMEOUT
        out["deepest_phase"] = "tcp"
        out["note"] = "SYN 未被应答，%.2fs 到探测上限（内核仍在重试）" % timeout
        sock.close()
        return out
    except ConnectionRefusedError as e:
        out["tcp_s"] = round(_now() - t0, 4)
        out["state"] = CONNECT_REFUSED
        out["deepest_phase"] = "tcp"
        out["note"] = "RST: %s" % e
        sock.close()
        return out
    except OSError as e:
        out["tcp_s"] = round(_now() - t0, 4)
        out["state"] = CONNECT_TIMEOUT
        out["deepest_phase"] = "tcp"
        out["note"] = "OSError: %s" % e
        sock.close()
        return out
    out["tcp_s"] = round(_now() - t0, 4)

    # 3) TLS
    if parts.scheme == "https":
        ctx = ssl.create_default_context()
        t0 = _now()
        try:
            sock = ctx.wrap_socket(sock, server_hostname=host)
        except (ssl.SSLError, socket.timeout, OSError) as e:
            out["tls_s"] = round(_now() - t0, 4)
            out["state"] = TLS_FAIL
            out["deepest_phase"] = "tls"
            out["note"] = "%s: %s" % (type(e).__name__, e)
            try:
                sock.close()
            except OSError:
                pass
            return out
        out["tls_s"] = round(_now() - t0, 4)
        sock.settimeout(timeout)

    # 4) 发送请求
    req = _build_request(parts)
    t0 = _now()
    try:
        sock.sendall(req)
    except (socket.timeout, OSError) as e:
        out["send_s"] = round(_now() - t0, 4)
        out["state"] = STALLED_NO_BYTES
        out["deepest_phase"] = "send"
        out["note"] = "sendall 失败: %s" % e
        sock.close()
        return out
    out["send_s"] = round(_now() - t0, 4)

    # 5) 首字节 + 6) 整包
    buf = b""
    t_first = None
    t0 = _now()
    head = None
    body_done = False
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        except OSError as e:
            out["note"] = "recv OSError: %s" % e
            break
        if not chunk:
            body_done = True
            break
        if t_first is None:
            t_first = _now() - t0
        buf += chunk
        if head is None:
            head = _parse_head(buf)
        if head and len(buf) > read_cap:
            break
        # 内容长度足够就当完事
        if head:
            cl = head["headers"].get("content-length")
            if cl and cl.isdigit() and head["body_have"] >= int(cl):
                body_done = True
                break
    t_end = _now() - t0
    # 结束时用完整 buf 重新解析头部：中途解析的那份 body_have 是过期的，
    # 用它比 content-length 会把正常响应误判成 TRUNCATED（第一版真实 bug）。
    head_fresh = _parse_head(buf)
    if head_fresh:
        head = head_fresh
    out["bytes_recv"] = len(buf)
    out["ttfb_s"] = round(t_first, 4) if t_first is not None else None
    out["body_s"] = round(t_end, 4) if t_first is not None else None

    head = head or _parse_head(buf)
    if head:
        out["status"] = head["status"]
        cl = head["headers"].get("content-length")
        if cl and cl.isdigit():
            out["declared_len"] = int(cl)

    # 判定
    if t_first is None:
        out["state"] = STALLED_NO_BYTES
        out["deepest_phase"] = "ttfb"
        out["note"] = "TCP%s已建立但 %.2fs 内零字节到达" % (
            "/TLS" if parts.scheme == "https" else "", timeout)
    elif head is None:
        out["state"] = STALLED_PARTIAL
        out["deepest_phase"] = "head"
        out["note"] = "收到 %d 字节但响应头不完整" % len(buf)
    elif out["declared_len"] is not None and head["body_have"] < out["declared_len"]:
        out["state"] = STALLED_PARTIAL if not body_done else TRUNCATED
        out["deepest_phase"] = "body"
        out["note"] = "声明 %d 字节，实收 %d 字节%s" % (
            out["declared_len"], head["body_have"],
            "，连接先关" if body_done else "，此后无新字节")
    elif not body_done and out["bytes_recv"] >= read_cap:
        out["state"] = SLOW_UP
        out["deepest_phase"] = "body"
        out["note"] = "读到 read_cap=%d 主动停（未被截断，是探测自己设的上限）" % read_cap
    elif not body_done:
        out["state"] = STALLED_PARTIAL
        out["deepest_phase"] = "body"
        out["note"] = "头部完整、body 无结束标记（chunked 终止块缺失）"
    else:
        out["state"] = SLOW_UP if (out["ttfb_s"] or 0) > slow_threshold else UP
        out["deepest_phase"] = "body"
        out["note"] = "读到 EOF"
    try:
        sock.close()
    except OSError:
        pass
    return out


def classify(readings):
    """离线复判：只吃已经采到的读数，不重新发请求。

    判定必须由 deepest_phase（走到了哪一层）驱动，不能由「哪个字段是 None」驱动：
    某相位字段为 None 既可能是「没走到那一层」，也可能是「走到了但那层失败」，
    两者动作完全不同。第一版就是拿 `https 且 tls_s 为空` 当 TLS 失败，把两个
    「TCP 从未建立」的样本判成了 TLS_FAIL / STALLED_NO_BYTES。
    """
    if not readings:
        return "UNKNOWN"
    phase = readings.get("deepest_phase")
    note = (readings.get("note") or "").lower()
    state = readings.get("state")
    if phase is None:
        # 没有相位证据：只能用已记录的 state，没有就 UNKNOWN，不许猜
        return state if state else "UNKNOWN"
    if phase == "dns":
        return DNS_FAIL
    if phase == "tcp":
        if "rst" in note or "refused" in note or "10061" in note or "拒绝" in note:
            return CONNECT_REFUSED
        return CONNECT_TIMEOUT
    if phase == "tls":
        return TLS_FAIL
    if phase == "send":
        return STALLED_NO_BYTES
    if phase in ("ttfb", "head"):
        return STALLED_NO_BYTES if (readings.get("bytes_recv") or 0) == 0 else STALLED_PARTIAL
    if phase == "body":
        declared = readings.get("declared_len")
        got = readings.get("bytes_recv") or 0
        if got <= 0:
            return STALLED_NO_BYTES
        if declared and got < int(declared):
            return TRUNCATED
        if state in (UP, SLOW_UP, TRUNCATED, STALLED_PARTIAL):
            return state
        return UP
    return state if state else "UNKNOWN"


# ---------------------------------------------------------------- 自检用服务端

class _Scenario:
    def __init__(self, mode, delay=0.0, body=b'{"ok":true}', declared=None):
        self.mode = mode
        self.delay = delay
        self.body = body
        self.declared = declared


def _make_handler(scn):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if scn.mode == "slow_headers":
                time.sleep(scn.delay)
                self.send_response(200)
                self.send_header("Content-Length", str(len(scn.body)))
                self.end_headers()
                self.wfile.write(scn.body)
            elif scn.mode == "never_send":
                time.sleep(30)
                try:
                    self.close_connection = True
                except Exception:
                    pass
            elif scn.mode == "partial_body":
                self.send_response(200)
                self.send_header("Content-Length", str(scn.declared or 4096))
                self.end_headers()
                self.wfile.write(scn.body[: len(scn.body) // 2])
                self.wfile.flush()
                self.close_connection = True
            elif scn.mode == "stall_body":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"%x\r\n" % len(scn.body) + scn.body + b"\r\n")
                self.wfile.flush()
                time.sleep(30)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(scn.body)))
                self.end_headers()
                self.wfile.write(scn.body)

    return H


def _serve(scn):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(scn))
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _free_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def selftest():
    checks = []

    def chk(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # 1 正常
    srv = _serve(_Scenario("ok"))
    r = probe("http://127.0.0.1:%d/x" % srv.server_address[1], timeout=3)
    chk("正常响应→UP", r["state"] == UP, r["state"])
    chk("正常响应状态 200", r["status"] == 200, str(r["status"]))
    chk("正常响应字节数>0", r["bytes_recv"] > 0, str(r["bytes_recv"]))
    chk("正常响应 tcp_s 有值", r["tcp_s"] is not None, str(r["tcp_s"]))
    srv.shutdown()

    # 2 慢响应头（连得上、有进展）
    srv = _serve(_Scenario("slow_headers", delay=0.9))
    r = probe("http://127.0.0.1:%d/x" % srv.server_address[1], timeout=3, slow_threshold=0.3)
    chk("慢响应→SLOW_UP", r["state"] == SLOW_UP, r["state"])
    chk("慢响应 ttfb≈0.9s", (r["ttfb_s"] or 0) >= 0.85, str(r["ttfb_s"]))
    chk("慢响应 tcp 快于 ttfb", r["tcp_s"] < r["ttfb_s"], "%s vs %s" % (r["tcp_s"], r["ttfb_s"]))
    srv.shutdown()

    # 3 连上但不发字节
    srv = _serve(_Scenario("never_send"))
    r = probe("http://127.0.0.1:%d/x" % srv.server_address[1], timeout=0.7)
    chk("连上零字节→STALLED_NO_BYTES", r["state"] == STALLED_NO_BYTES, r["state"])
    chk("连上零字节但 tcp_s 很小", r["tcp_s"] is not None and r["tcp_s"] < 0.5, str(r["tcp_s"]))
    chk("连上零字节 bytes_recv=0", r["bytes_recv"] == 0, str(r["bytes_recv"]))
    chk("连上零字节的退出码不是 0", exit_code_for(r["state"]) != 0, str(exit_code_for(r["state"])))
    chk("零字节时 elapsed_s 记录了真实等待",
        (r.get("elapsed_s") or 0) >= 0.6, str(r.get("elapsed_s")))
    srv.shutdown()

    # 4 声明长度未给够
    srv = _serve(_Scenario("partial_body", body=b"0123456789", declared=4096))
    r = probe("http://127.0.0.1:%d/x" % srv.server_address[1], timeout=2)
    chk("半截 body→STALLED_PARTIAL/TRUNCATED",
        r["state"] in (STALLED_PARTIAL, TRUNCATED), r["state"])
    chk("半截 body declared_len=4096", r["declared_len"] == 4096, str(r["declared_len"]))
    srv.shutdown()

    # 5 chunked 后停住
    srv = _serve(_Scenario("stall_body"))
    r = probe("http://127.0.0.1:%d/x" % srv.server_address[1], timeout=0.7)
    chk("chunked 停住→STALLED_PARTIAL", r["state"] == STALLED_PARTIAL, r["state"])
    srv.shutdown()

    # 6 关闭端口：两种合法形态——RST 快速拒绝，或被过滤驱动静默丢弃到超时。
    # 本机实测是后者（Windows + 网络过滤驱动），断言只锁不变量：判死、且退出码 3。
    port = _free_closed_port()
    r = probe("http://127.0.0.1:%d/x" % port, timeout=2)
    chk("关闭端口判死(REFUSED 或 CONNECT_TIMEOUT)",
        r["state"] in (CONNECT_REFUSED, CONNECT_TIMEOUT), r["state"])
    chk("关闭端口退出码=3", exit_code_for(r["state"]) == 3, str(exit_code_for(r["state"])))
    chk("关闭端口零字节", r["bytes_recv"] == 0, str(r["bytes_recv"]))
    print("      [实测] 关闭端口形态=%s tcp_s=%s（RST 会 <0.05s，丢包则是整个超时）"
          % (r["state"], r["tcp_s"]))

    # 7 DNS 失败
    r = probe("http://no-such-host.invalid/x", timeout=2)
    chk("DNS 失败→DNS_FAIL", r["state"] == DNS_FAIL, r["state"])
    chk("DNS 失败 deepest=dns", r["deepest_phase"] == "dns", str(r["deepest_phase"]))

    # 8 离线复判与在线一致（采样与判定分离）
    r = {"url": "http://x/", "dns_s": 0.01, "tcp_s": 0.02, "tls_s": None,
         "ttfb_s": None, "bytes_recv": 0, "declared_len": None, "note": "",
         "deepest_phase": "ttfb"}
    chk("离线复判：零字节→STALLED_NO_BYTES", classify(r) == STALLED_NO_BYTES, classify(r))
    r2 = dict(r, ttfb_s=0.3, bytes_recv=10, declared_len=100, deepest_phase="body")
    chk("离线复判：长度不足→TRUNCATED", classify(r2) == TRUNCATED, classify(r2))
    r3 = dict(r, ttfb_s=0.3, bytes_recv=100, declared_len=100,
              deepest_phase="body", state=UP)
    chk("离线复判：够数→UP", classify(r3) == UP, classify(r3))
    chk("离线复判：无读数→UNKNOWN", classify({}) == "UNKNOWN", classify({}))
    # 回归：第一版把「TCP 从未建立」判成 TLS 失败（相位缺失 ≠ 相位失败）
    r4 = {"url": "https://x/", "dns_s": 0.006, "tcp_s": 6.0, "tls_s": None,
          "ttfb_s": None, "bytes_recv": 0, "declared_len": None,
          "note": "SYN 未被应答", "deepest_phase": "tcp", "state": CONNECT_TIMEOUT}
    chk("回归：https 但 TCP 未建立→CONNECT_TIMEOUT 而非 TLS_FAIL",
        classify(r4) == CONNECT_TIMEOUT, classify(r4))
    r5 = dict(r4, note="RST: [WinError 10061] 目标计算机积极拒绝")
    chk("回归：RST→CONNECT_REFUSED", classify(r5) == CONNECT_REFUSED, classify(r5))
    r6 = dict(r4, tls_s=0.2, deepest_phase="tls", note="SSLError: cert",
              state=TLS_FAIL)
    chk("回归：真 TLS 失败→TLS_FAIL", classify(r6) == TLS_FAIL, classify(r6))
    r7 = {"url": "https://x/", "state": STALLED_NO_BYTES}
    chk("无 deepest_phase 时不猜：沿用已记录 state",
        classify(r7) == STALLED_NO_BYTES, classify(r7))
    chk("无 deepest_phase 且无 state→UNKNOWN", classify({"url": "x"}) == "UNKNOWN",
        classify({"url": "x"}))

    # 9 退出码映射
    chk("UP→0", exit_code_for(UP) == 0, str(exit_code_for(UP)))
    chk("STALLED→2", exit_code_for(STALLED_NO_BYTES) == 2, str(exit_code_for(STALLED_NO_BYTES)))
    chk("DNS_FAIL→3", exit_code_for(DNS_FAIL) == 3, str(exit_code_for(DNS_FAIL)))

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("\n%d/%d 项通过" % (len(checks) - len(failed), len(checks)))
    return 0 if not failed else 5


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="把超时拆成五个阶段")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("probe")
    p.add_argument("url")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--read-cap", type=int, default=65536)
    p.add_argument("--slow-threshold", type=float, default=1.0)
    p.add_argument("--json", action="store_true")

    c = sub.add_parser("classify")
    c.add_argument("file")

    sub.add_parser("selftest")

    args = ap.parse_args(argv)
    if not args.cmd:
        print(__doc__)
        return 4

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "classify":
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print("读不到/读不懂读数文件: %s" % e)
            return 4
        items = data if isinstance(data, list) else [data]
        if not items:
            print("零输入：文件里没有任何读数")
            return 4
        rc = 0
        for it in items:
            st = classify(it)
            print("%-22s %-28s %s" % (st, it.get("url", "?"), it.get("note", "")))
            rc = max(rc, exit_code_for(st) if st != "UNKNOWN" else 2)
        return rc

    # probe
    try:
        r = probe(args.url, timeout=args.timeout, read_cap=args.read_cap,
                  slow_threshold=args.slow_threshold)
    except ValueError as e:
        print(str(e))
        return 4
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("URL      %s" % r["url"])
        print("DNS      %s s" % r["dns_s"])
        print("TCP      %s s" % r["tcp_s"])
        print("TLS      %s s" % r["tls_s"])
        print("send     %s s" % r["send_s"])
        print("TTFB     %s s" % r["ttfb_s"])
        print("body/全  %s s" % r["body_s"])
        print("bytes    %d (declared %s) status %s" % (
            r["bytes_recv"], r["declared_len"], r["status"]))
        print("最深阶段 %s" % r["deepest_phase"])
        print("判定     %s" % r["state"])
        print("实等     %s s（相位全为 None 时这是唯一能进预算的数）" % r["elapsed_s"])
        print("说明     %s" % r["note"])
    return exit_code_for(r["state"])


if __name__ == "__main__":
    sys.exit(main())
