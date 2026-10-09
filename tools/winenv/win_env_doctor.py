#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""win_env_doctor —— Windows 上跑 AI Agent 自动化的环境自检工具（纯标准库，零依赖）

为什么需要它：Windows 上大量"Agent 跑了但什么都没发生 / 文件写了但找不到 / 端口起不来"
不是模型问题，是环境问题。三类高频：

  1. 路径分歧 —— bash(MSYS) 里的 /tmp 与原生程序眼里的 /tmp 指向两个不同目录；
     给原生程序传 /c/... 路径会被当相对路径写到别处。
  2. 端口被保留 —— Windows 把大段端口留给 Hyper-V/WSL/Docker，绑定直接 WinError 10013。
  3. 环境变量继承 —— 父进程的 PYTHONPATH 会把子进程的 import 劫持到别的项目的模块上，
     表现为 DLL 加载失败或"模块串味"，日志里看不到任何显式报错。

用法：
    python win_env_doctor.py selftest                 # 自带样例自检，退出码即结论
    python win_env_doctor.py scan                     # 采集本机环境并出体检报告
    python win_env_doctor.py scan --json --out r.json
    python win_env_doctor.py scan --input snap.json   # 从快照文件体检（可离线复算）
    python win_env_doctor.py explain TMP_DIVERGENCE   # 看某条规则的解释与修法

退出码：0 = 全部通过；2 = 有警告；3 = 有失败 / 输入不完整（缺数据不等于干净）
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------
# 规则实现：纯函数，输入是快照 dict，输出是结论列表。便于用合成样例做自检。
# --------------------------------------------------------------------------

RULES = {
    "TMP_DIVERGENCE": "bash 的 /tmp 与原生程序的 /tmp 不是同一个目录",
    "MSYS_PATH_IN_ENV": "环境变量里带 MSYS 风格路径（/c/... 或 /x/...），原生工具读不了",
    "PYTHONPATH_POLLUTION": "PYTHONPATH 非空，子进程的 import 会被劫持",
    "PYTHONHOME_SET": "PYTHONHOME 被设置，解释器标准库定位可能串味",
    "SYS_PATH_FOREIGN": "sys.path 里混入了别的应用/venv 的 site-packages",
    "PORT_RESERVED": "端口落在 Windows 保留段内，绑定必失败（WinError 10013）",
    "PORT_EPHEMERAL_TRAP": "端口落在 50000+ 管理排除段附近，可能被系统动态占用",
    "CONSOLE_ENCODING": "原生命令输出是 GBK，按 UTF-8 解码会炸",
    "SCRATCH_NOT_WRITABLE": "临时/暂存目录不可写",
    "SCRATCH_PATH_DIRTY": "暂存目录路径含空格或非 ASCII 字符，原生工具易踩",
    "LONG_PATHS_DISABLED": "长路径支持未开启，深层目录会莫名失败",
}


def _ok(rid, msg, fix=""):
    return {"id": rid, "status": "ok", "message": msg, "fix": fix}


def _warn(rid, msg, fix=""):
    return {"id": rid, "status": "warn", "message": msg, "fix": fix}


def _fail(rid, msg, fix=""):
    return {"id": rid, "status": "fail", "message": msg, "fix": fix}


def _norm(p):
    return (p or "").replace("\\", "/").rstrip("/").lower()


def check_tmp_divergence(snap):
    t = snap.get("temp", {}) or {}
    native = t.get("native_slash_tmp")
    shell = t.get("shell_slash_tmp")
    if not native or not shell:
        return [_warn("TMP_DIVERGENCE", "只采到一半（原生=%r，shell=%r），无法比对——不是通过"
                                        % (native, shell),
                      "在 shell 里执行 cd /tmp && pwd -W 与原生 python -c \"import os;print(os.path.abspath('/tmp'))\" 各查一次")]
    if _norm(native) != _norm(shell):
        return [_fail(
            "TMP_DIVERGENCE",
            "同一个 /tmp 在两个世界里指向不同目录：原生程序=%s，shell=%s" % (native, shell),
            "两边都别用 /tmp：显式写绝对路径，或统一用 TMPDIR 指向的暂存目录",
        )]
    return [_ok("TMP_DIVERGENCE", "/tmp 解析一致：%s" % native)]


def check_msys_path_in_env(snap):
    cand = (snap.get("paths_in_env") or {}).get("candidates") or {}
    bad = []
    for k, v in cand.items():
        if not v:
            continue
        for part in str(v).replace(";", os.pathsep).split(os.pathsep):
            part = part.strip()
            if len(part) > 2 and part[0] == "/" and part[1].isalpha() and part[2] == "/":
                bad.append("%s=%s" % (k, part))
                break
    if bad:
        return [_warn(
            "MSYS_PATH_IN_ENV",
            "环境变量里是 MSYS 路径（原生程序会当相对路径）：" + "; ".join(bad[:4]),
            "传给 git/curl/node/python 的路径改成 C:/... 正斜杠原生形式",
        )]
    return [_ok("MSYS_PATH_IN_ENV", "未发现 MSYS 风格路径")]


def check_pythonpath(snap):
    env = (snap.get("python") or {}).get("env") or {}
    pp = (env.get("PYTHONPATH") or "").strip()
    out = []
    if pp:
        out.append(_fail(
            "PYTHONPATH_POLLUTION",
            "PYTHONPATH=%s（子进程 import 会优先命中这里，可能引入别的项目版本）" % pp,
            "启动子进程前 env -u PYTHONPATH；启动脚本里用 set PYTHONPATH= 删除该键（设成空串仍会泄漏）",
        ))
    else:
        out.append(_ok("PYTHONPATH_POLLUTION", "PYTHONPATH 未被设置"))
    ph = (env.get("PYTHONHOME") or "").strip()
    if ph:
        out.append(_fail("PYTHONHOME_SET", "PYTHONHOME=%s" % ph, "同样用 env -u PYTHONHOME 清掉"))
    else:
        out.append(_ok("PYTHONHOME_SET", "PYTHONHOME 未被设置"))
    return out


def check_sys_path_foreign(snap):
    py = snap.get("python") or {}
    sp = py.get("sys_path") or []
    foreign = [p for p in sp if "hermes-agent" in p.replace("\\", "/").lower()]
    if foreign:
        return [_warn(
            "SYS_PATH_FOREIGN",
            "sys.path 混入了其它应用的 site-packages（%d 条）" % len(foreign),
            "在入口文件第一行 import 之前过滤 sys.path，删掉不属于本应用的路径",
        )]
    return [_ok("SYS_PATH_FOREIGN", "sys.path 未见其它应用目录")]


def check_ports(snap):
    ports = snap.get("ports") or {}
    excluded = ports.get("excluded") or []
    probed = ports.get("probed") or {}
    out = []
    # 采集不到排除段时不能判过——没数据不等于没风险
    if not excluded:
        out.append(_warn("PORT_RESERVED", "未采集到端口排除段，无法判断（不是通过）"))
    else:
        hit = []
        for p, res in probed.items():
            try:
                pi = int(p)
            except (TypeError, ValueError):
                continue
            if res != "BLOCKED":
                continue
            for rng in excluded:
                if len(rng) == 2 and rng[0] <= pi <= rng[1]:
                    hit.append("%s∈[%s,%s]" % (p, rng[0], rng[1]))
                    break
        if hit:
            out.append(_fail(
                "PORT_RESERVED",
                "这些端口绑不了：" + ", ".join(hit),
                "换到排除段外的端口（netsh int ipv4 show excludedportrange protocol=tcp 查全量）",
            ))
        else:
            out.append(_ok("PORT_RESERVED", "已探测端口均不在排除段内"))
        dyn = []
        for p, res in probed.items():
            try:
                pi = int(p)
            except (TypeError, ValueError):
                continue
            if pi >= 49152:
                dyn.append(p)
        if dyn:
            out.append(_warn("PORT_EPHEMERAL_TRAP",
                             "探测端口落在动态端口段（49152+）：%s" % ",".join(dyn),
                             "服务端口尽量选 1024-49151 内的固定值，动态段会被系统随机占用"))
        else:
            out.append(_ok("PORT_EPHEMERAL_TRAP", "探测端口均在固定端口段内"))
    return out


def check_encoding(snap):
    enc = snap.get("encoding") or {}
    cmd_enc = enc.get("cmd_echo")
    if cmd_enc == "gbk":
        return [_warn(
            "CONSOLE_ENCODING",
            "原生命令输出是 GBK（stdout=%s）" % enc.get("stdout"),
            "读子进程输出统一 encoding='gbk', errors='replace'；写文件统一 utf-8",
        )]
    if not cmd_enc:
        return [_warn("CONSOLE_ENCODING", "未采集到原生命令编码，无法判断")]
    return [_ok("CONSOLE_ENCODING", "原生命令输出可用 %s 解码" % cmd_enc)]


def check_scratch(snap):
    fs = snap.get("filesystem") or {}
    out = []
    path = fs.get("scratch")
    if fs.get("writable") is False:
        out.append(_fail("SCRATCH_NOT_WRITABLE", "暂存目录不可写：%s" % path, "换一个可写目录，或修权限"))
    elif fs.get("writable") is None:
        out.append(_warn("SCRATCH_NOT_WRITABLE", "未做写入探测，无法判断"))
    else:
        out.append(_ok("SCRATCH_NOT_WRITABLE", "暂存目录可写：%s" % path))
    if path and (" " in path or any(ord(c) > 127 for c in path)):
        out.append(_warn(
            "SCRATCH_PATH_DIRTY",
            "暂存路径含空格/中文：%s" % path,
            "临时文件路径尽量用 ASCII 且无空格，避免原生命令的参数解析问题",
        ))
    else:
        out.append(_ok("SCRATCH_PATH_DIRTY", "暂存路径为 ASCII 且无空格"))
    return out


def check_long_paths(snap):
    lp = snap.get("long_paths")
    if lp is True:
        return [_ok("LONG_PATHS_DISABLED", "长路径支持已开启")]
    if lp is False:
        return [_warn("LONG_PATHS_DISABLED", "LongPathsEnabled=0", "深层目录/长文件名在 260 字符处会失败，路径尽量短")]
    return [_warn("LONG_PATHS_DISABLED", "未读到 LongPathsEnabled（无管理员权限时读不到，属正常）")]


ALL_CHECKS = [
    check_tmp_divergence, check_msys_path_in_env, check_pythonpath, check_sys_path_foreign,
    check_ports, check_encoding, check_scratch, check_long_paths,
]

REQUIRED_KEYS = ("os", "python", "temp", "ports", "encoding", "filesystem")


def evaluate(snap):
    results = []
    for fn in ALL_CHECKS:
        results.extend(fn(snap))
    return results


def verdict(results):
    if any(r["status"] == "fail" for r in results):
        return 3
    if any(r["status"] == "warn" for r in results):
        return 2
    return 0


def coverage_missing(snap):
    return [k for k in REQUIRED_KEYS if k not in snap]


# --------------------------------------------------------------------------
# 采集（仅 Windows 有意义；其它平台返回不完整快照，由 coverage 判定为缺数据）
# --------------------------------------------------------------------------

def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=False)
        return p.stdout or b""
    except Exception:
        return b""


def collect_live(probe_ports=(1241, 1300, 7860, 8300, 50010)):
    snap = {"os": os.name}
    env = os.environ
    snap["python"] = {
        "executable": sys.executable,
        "version": "%d.%d.%d" % sys.version_info[:3],
        "sys_path": list(sys.path),
        "env": {"PYTHONPATH": env.get("PYTHONPATH", ""), "PYTHONHOME": env.get("PYTHONHOME", "")},
    }

    native_tmp = os.path.abspath("/tmp") if os.name == "nt" else None
    shell_tmp = None
    if os.name == "nt":
        for cmd in (["bash", "-c", "cd /tmp && pwd -W"],
                    ["bash", "--noprofile", "--norc", "-c", "cd /tmp && pwd -W"],
                    ["sh", "-c", "cd /tmp && pwd -W"]):
            raw = _run(cmd).decode("utf-8", "replace").strip().replace("\r", "")
            if raw and ":" in raw:
                shell_tmp = raw.splitlines()[-1].strip()
                break
    snap["temp"] = {
        "native_slash_tmp": native_tmp,
        "shell_slash_tmp": shell_tmp,
        "TEMP": env.get("TEMP", ""),
        "TMPDIR": env.get("TMPDIR", ""),
    }

    excluded = []
    if os.name == "nt":
        out = _run(["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"])
        text = None
        for enc in ("utf-8", "gbk"):
            try:
                text = out.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text:
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    excluded.append([int(parts[0]), int(parts[1])])
    probed = {}
    for p in probe_ports:
        probed[str(p)] = "OK" if _can_bind(p) else "BLOCKED"
    snap["ports"] = {"excluded": excluded, "probed": probed}

    cmd_enc = None
    if os.name == "nt":
        raw = _run(["cmd", "/c", "echo", "中文测试"])
        try:
            raw.decode("utf-8")
            cmd_enc = "utf-8" if "中文测试" in raw.decode("utf-8") else "gbk"
        except UnicodeDecodeError:
            cmd_enc = "gbk"
    snap["encoding"] = {"stdout": sys.stdout.encoding, "cmd_echo": cmd_enc}

    scratch = env.get("TMPDIR") or tempfile.gettempdir()
    writable = None
    try:
        fd, path = tempfile.mkstemp(dir=scratch, prefix="wed_probe_")
        os.close(fd)
        os.remove(path)
        writable = True
    except Exception:
        writable = False
    snap["filesystem"] = {"scratch": scratch, "writable": writable}

    snap["paths_in_env"] = {"candidates": {k: env.get(k, "") for k in ("TMPDIR", "PATH", "HOME", "PYTHONPATH") if env.get(k)}}
    snap["long_paths"] = _long_paths()
    return snap


def _can_bind(port, host="127.0.0.1"):
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _long_paths():
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except Exception:
        return None


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

MARK = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}


def render(results, snap=None):
    lines = []
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for r in results:
        counts[r["status"]] += 1
    lines.append("win_env_doctor 体检报告  ——  PASS %d / WARN %d / FAIL %d"
                 % (counts["ok"], counts["warn"], counts["fail"]))
    lines.append("-" * 72)
    for r in results:
        lines.append("[%s] %-20s %s" % (MARK[r["status"]], r["id"], r["message"]))
        if r["status"] != "ok" and r.get("fix"):
            lines.append("       修法: %s" % r["fix"])
    if snap:
        lines.append("-" * 72)
        p = snap.get("ports", {})
        lines.append("探测端口: %s" % json.dumps(p.get("probed", {}), ensure_ascii=False))
        lines.append("排除段:   %s" % (p.get("excluded") or "未采集"))
        t = snap.get("temp", {})
        lines.append("原生 /tmp=%s | shell /tmp=%s" % (t.get("native_slash_tmp"), t.get("shell_slash_tmp")))
    code = verdict(results)
    lines.append("-" * 72)
    lines.append({0: "结论: 环境干净，可以跑自动化。",
                  2: "结论: 有警告项，能跑但会在特定场景翻车。",
                  3: "结论: 有失败项，先修再跑。"}[code])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检：合成快照 + 断言。退出码即结论，不依赖使用者的眼睛。
# --------------------------------------------------------------------------

def _base_snapshot():
    return {
        "os": "nt",
        "python": {"executable": r"C:\app\python.exe", "version": "3.11.9",
                   "sys_path": [r"C:\app", r"C:\app\Lib"],
                   "env": {"PYTHONPATH": "", "PYTHONHOME": ""}},
        "temp": {"native_slash_tmp": r"C:\tmp", "shell_slash_tmp": r"C:\Users\u\AppData\Local\Temp",
                 "TEMP": r"C:\Users\u\AppData\Local\Temp", "TMPDIR": r"C:\scratch"},
        "ports": {"excluded": [[1066, 1365], [50000, 50059]], "probed": {"1241": "BLOCKED", "8300": "OK"}},
        "encoding": {"stdout": "utf-8", "cmd_echo": "utf-8"},
        "filesystem": {"scratch": r"C:\scratch", "writable": True},
        "paths_in_env": {"candidates": {"TMPDIR": r"C:\scratch"}},
        "long_paths": True,
    }


def selftest():
    checks = []

    def expect(name, cond):
        checks.append((name, bool(cond)))

    # 1. 基线：干净快照里，除了已知的两条（/tmp 分歧、保留端口），其它必须 ok
    snap = _base_snapshot()
    res = evaluate(snap)
    byid = {r["id"]: r["status"] for r in res}
    expect("基线/tmp 分歧被标失败", byid["TMP_DIVERGENCE"] == "fail")
    expect("基线/1241 保留端口被标失败", byid["PORT_RESERVED"] == "fail")
    expect("基线/PYTHONPATH 空判过", byid["PYTHONPATH_POLLUTION"] == "ok")
    expect("基线/编码 utf-8 判过", byid["CONSOLE_ENCODING"] == "ok")
    expect("基线/暂存可写判过", byid["SCRATCH_NOT_WRITABLE"] == "ok")
    expect("基线/退出码=3", verdict(res) == 3)

    # 2. /tmp 一致 → 该条判过
    snap = _base_snapshot()
    snap["temp"]["shell_slash_tmp"] = snap["temp"]["native_slash_tmp"]
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("tmp 一致判过", byid["TMP_DIVERGENCE"] == "ok")

    # 3. 缺 /tmp 数据 → 警告而非通过（没数据不等于没问题）
    snap = _base_snapshot()
    snap["temp"] = {}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("tmp 缺数据判警告", byid["TMP_DIVERGENCE"] == "warn")
    snap = _base_snapshot()
    snap["temp"]["shell_slash_tmp"] = None
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("只采到一半判警告（不判过）", byid["TMP_DIVERGENCE"] == "warn")

    # 4. PYTHONPATH 污染 → 失败 + 有修法
    snap = _base_snapshot()
    snap["python"]["env"]["PYTHONPATH"] = r"C:\other\venv\Lib\site-packages"
    res = evaluate(snap)
    r = [x for x in res if x["id"] == "PYTHONPATH_POLLUTION"][0]
    expect("PYTHONPATH 污染判失败", r["status"] == "fail")
    expect("PYTHONPATH 失败带修法", bool(r["fix"]))

    # 5. PYTHONHOME → 独立一条失败
    snap = _base_snapshot()
    snap["python"]["env"]["PYTHONHOME"] = r"C:\other\python"
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("PYTHONHOME 判失败", byid["PYTHONHOME_SET"] == "fail")

    # 6. 混合环境变量里的 MSYS 路径
    snap = _base_snapshot()
    snap["paths_in_env"]["candidates"]["TMPDIR"] = "/c/Users/u/AppData/Local/Temp"
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("MSYS 路径判警告", byid["MSYS_PATH_IN_ENV"] == "warn")

    # 7. 端口：落在排除段内判失败；不在段内的 BLOCKED 不背锅
    snap = _base_snapshot()
    snap["ports"] = {"excluded": [[1066, 1365]], "probed": {"8300": "BLOCKED"}}
    res = evaluate(snap)
    r = [x for x in res if x["id"] == "PORT_RESERVED"][0]
    expect("段外 BLOCKED 不判失败", r["status"] == "ok")

    # 8. 排除段采集不到 → 警告（不是通过）；动态端口段单独一条
    snap = _base_snapshot()
    snap["ports"] = {"excluded": [], "probed": {}}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("排除段缺数据判警告", byid["PORT_RESERVED"] == "warn")
    snap = _base_snapshot()
    snap["ports"]["probed"] = {"50010": "OK"}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("动态端口段判警告", byid["PORT_EPHEMERAL_TRAP"] == "warn")
    snap = _base_snapshot()
    snap["ports"]["probed"] = {"8300": "OK"}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("固定端口段不告警", byid["PORT_EPHEMERAL_TRAP"] == "ok")

    # 9. 编码 GBK → 警告；采集不到 → 警告
    snap = _base_snapshot()
    snap["encoding"] = {"stdout": "utf-8", "cmd_echo": "gbk"}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("GBK 输出判警告", byid["CONSOLE_ENCODING"] == "warn")
    snap = _base_snapshot()
    snap["encoding"] = {}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("编码缺数据判警告", byid["CONSOLE_ENCODING"] == "warn")

    # 10. 暂存不可写 → 失败
    snap = _base_snapshot()
    snap["filesystem"] = {"scratch": r"C:\scratch", "writable": False}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("暂存不可写判失败", byid["SCRATCH_NOT_WRITABLE"] == "fail")

    # 11. 路径含中文 → 警告
    snap = _base_snapshot()
    snap["filesystem"] = {"scratch": r"C:\用户 目录\暂存", "writable": True}
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("路径脏判警告", byid["SCRATCH_PATH_DIRTY"] == "warn")

    # 12. 长路径关闭 → 警告；读不到 → 警告
    snap = _base_snapshot()
    snap["long_paths"] = False
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("长路径关闭判警告", byid["LONG_PATHS_DISABLED"] == "warn")
    snap = _base_snapshot()
    snap["long_paths"] = None
    byid = {r["id"]: r["status"] for r in evaluate(snap)}
    expect("长路径读不到判警告", byid["LONG_PATHS_DISABLED"] == "warn")

    # 13. 覆盖度：空快照必须被认出「缺数据」，不能当成「全通过」
    expect("空快照缺 6 个必需键", len(coverage_missing({})) == len(REQUIRED_KEYS))
    expect("完整快照不缺键", coverage_missing(_base_snapshot()) == [])

    # 14. 全 ok 快照 → 退出码 0，且不会出现 fail
    snap = _base_snapshot()
    snap["temp"]["shell_slash_tmp"] = snap["temp"]["native_slash_tmp"]
    snap["ports"] = {"excluded": [[1066, 1365]], "probed": {"8300": "OK"}}
    snap["long_paths"] = True
    res = evaluate(snap)
    expect("干净快照退出码 0", verdict(res) == 0)
    expect("干净快照无 fail", all(r["status"] != "fail" for r in res))

    # 15. 报告渲染包含计数与结论行
    txt = render(evaluate(_base_snapshot()), _base_snapshot())
    expect("报告含计数", "PASS" in txt and "FAIL" in txt)
    expect("报告含结论", "结论:" in txt)

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
    print("-" * 72)
    print("selftest: %d/%d" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


def explain(rule):
    rid = rule.upper()
    if rid not in RULES:
        print("未知规则：%s\n可用：%s" % (rule, ", ".join(sorted(RULES))))
        return 1
    print("%s —— %s" % (rid, RULES[rid]))
    snap = _base_snapshot()
    if rid == "TMP_DIVERGENCE":
        snap["temp"]["shell_slash_tmp"] = r"C:\Users\u\AppData\Local\Temp"
    elif rid == "PYTHONPATH_POLLUTION":
        snap["python"]["env"]["PYTHONPATH"] = r"C:\other\venv\Lib\site-packages"
    elif rid == "MSYS_PATH_IN_ENV":
        snap["paths_in_env"]["candidates"]["TMPDIR"] = "/c/Users/u/AppData/Local/Temp"
    elif rid == "CONSOLE_ENCODING":
        snap["encoding"]["cmd_echo"] = "gbk"
    elif rid == "SCRATCH_NOT_WRITABLE":
        snap["filesystem"]["writable"] = False
    elif rid == "SCRATCH_PATH_DIRTY":
        snap["filesystem"]["scratch"] = r"C:\用户 目录\暂存"
    elif rid == "LONG_PATHS_DISABLED":
        snap["long_paths"] = False
    for r in evaluate(snap):
        if r["id"] == rid:
            print("[%s] %s" % (MARK[r["status"]], r["message"]))
            if r.get("fix"):
                print("修法: %s" % r["fix"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="win_env_doctor", description="Windows Agent 环境自检")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="跑自带样例自检")
    sc = sub.add_parser("scan", help="采集并体检")
    sc.add_argument("--json", action="store_true", help="输出 JSON")
    sc.add_argument("--out", help="把 JSON 报告写到文件")
    sc.add_argument("--input", help="从快照文件体检（离线复算）")
    sc.add_argument("--ports", help="额外探测端口，逗号分隔")
    ex = sub.add_parser("explain", help="解释某条规则")
    ex.add_argument("rule")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()

    if a.cmd == "explain":
        return explain(a.rule)

    if a.cmd != "scan":
        ap.print_help()
        return 1

    if a.input:
        if not os.path.exists(a.input):
            print("输入快照不存在：%s —— 什么也没检查，退出码 3（缺数据不等于环境干净）" % a.input)
            return 3
        try:
            with open(a.input, encoding="utf-8") as f:
                snap = json.load(f)
        except Exception as e:
            print("快照解析失败：%s" % e)
            return 3
    else:
        ports = tuple(int(x) for x in a.ports.split(",")) if a.ports else (1241, 1300, 7860, 8300, 50010)
        snap = collect_live(probe_ports=ports)

    missing = coverage_missing(snap)
    results = evaluate(snap)
    code = verdict(results)
    if missing:
        code = 3

    if a.json or a.out:
        report = {"snapshot": snap, "results": results, "exit_code": code,
                  "missing_keys": missing,
                  "counts": {s: sum(1 for r in results if r["status"] == s) for s in ("ok", "warn", "fail")}}
        blob = json.dumps(report, ensure_ascii=False, indent=2)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(blob)
            print("JSON 报告已写入 %s" % a.out)
        if a.json:
            print(blob)
            return code

    if missing:
        print("快照缺少必需段落：%s —— 本次体检不完整，按失败处理（缺数据不等于干净）" % ", ".join(missing))
    print(render(results, snap))
    print("exit=%d" % code)
    return code


if __name__ == "__main__":
    sys.exit(main())
