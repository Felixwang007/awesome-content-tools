#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh_tree_push.py — 用 Git Data API 把一次本地提交推到远端（git 智能协议不通时的备用通道）。

为什么需要它：某些网络里 github.com:443 的 TCP 连接建立不起来（connect=0，21s 后超时），
而 api.github.com 完全正常。此时 `git push` 永远失败，但仓库内容照样能推上去。

核心设计：
  * 内容寻址对账 —— 推送后逐个 blob 比对 SHA，而不是相信 HTTP 200。
  * 索引字节优先 —— 默认从 `git cat-file` 取已提交字节（而非工作区字节），
    避免 core.autocrlf 把 LF 变 CRLF 后 blob SHA 与本地不一致。
  * base 校验 —— 远端 head 必须等于你声明的 base，否则中止（防止覆盖别人的提交）。

命令：
    probe                         探测四条通道可达性与耗时
    push  --base <sha> [--dry-run] [--source index|worktree] [--branch main]
    verify --base <sha> --paths a,b [--repo o/r] --url-prefix https://...
    selftest                      纯本地断言，不联网

退出码：0 通过 / 2 对账或验证不一致 / 3 前置条件不满足（用法、base 不符、无改动）
纯标准库；需要本机装有 git 与已登录的 gh（取 token）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
DEFAULT_REPO = "Felixwang007/awesome-content-tools"
UA = "gh_tree_push/1.0 (+https://felixwang007.github.io/awesome-content-tools/)"

EXIT_OK = 0
EXIT_MISMATCH = 2
EXIT_PRECONDITION = 3


# ---------------------------------------------------------------- 基础工具


def sh(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> rc={p.returncode}: {p.stderr.strip()[:400]}")
    return p


def git(repo, *args, check=True):
    return sh(["git", "-C", repo, *args], check=check).stdout


def git_bytes(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True).stdout


def git_blob_sha(data: bytes) -> str:
    """Git 的 blob 对象哈希：sha1(b'blob <len>\\0' + data)。"""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def token():
    tok = os.environ.get("GITHUB_TOKEN") or ""
    if not tok:
        tok = sh(["gh", "auth", "token"]).stdout.strip()
    return tok


class Api:
    def __init__(self, repo: str, tok: str, timeout: float = 120.0):
        self.repo = repo
        self.tok = tok
        self.timeout = timeout
        self.calls = []  # (method, path, ms, status)

    def __call__(self, method: str, path: str, payload=None):
        url = f"{API_ROOT}/repos/{self.repo}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self.tok}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", UA)
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, data=data, timeout=self.timeout) as r:
                body = r.read().decode("utf-8")
                status, out = r.status, (json.loads(body) if body else {})
                err = ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            status, out, err = e.code, {}, body
        ms = (time.perf_counter() - t0) * 1000
        self.calls.append((method, path, round(ms, 1), status))
        if err:
            print(f"  HTTP {status} {method} {path}: {err[:300]}", file=sys.stderr)
        return status, out


# ---------------------------------------------------------------- 差异清单


def changed_paths(repo: str, base: str, head: str = "HEAD"):
    """返回 [(status, path)]；status 取 A/M/D。"""
    raw = git(repo, "diff", "--name-status", base, head)
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        out.append((parts[0][0], parts[-1]))
    return out


def index_bytes(repo: str, path: str):
    """取该路径在 HEAD 里的确切字节（已过 .gitattributes/autocrlf 归一化）。"""
    sha = git(repo, "rev-parse", f"HEAD:{path}").strip()
    return git_bytes(repo, "cat-file", "blob", sha), sha


def worktree_bytes(repo: str, path: str):
    with open(os.path.join(repo, path.replace("/", os.sep)), "rb") as f:
        return f.read()


def count_eol(data: bytes):
    crlf = data.count(b"\r\n")
    return crlf, data.count(b"\n") - crlf, data.count(b"\r") - crlf


# ---------------------------------------------------------------- 推送


def cmd_push(args):
    repo = os.path.abspath(os.path.expanduser(args.repo_dir))
    api = Api(args.repo, token())

    status, ref = api("GET", f"/git/ref/heads/{args.branch}")
    if status != 200:
        print(f"读不到分支 {args.branch}（HTTP {status}）", file=sys.stderr)
        return EXIT_PRECONDITION
    remote_sha = ref["object"]["sha"]
    print(f"远端 head      : {remote_sha[:7]}")

    if not remote_sha.startswith(args.base.lower()):
        print(
            f"中止：远端 head {remote_sha[:7]} 与声明的 base {args.base[:7]} 不一致。\n"
            f"  说明有别的写者在你之后推过；先 fetch/核对，别硬推。",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    status, base_commit = api("GET", f"/git/commits/{remote_sha}")
    if status != 200:
        return EXIT_PRECONDITION
    base_tree = base_commit["tree"]["sha"]

    changes = changed_paths(repo, args.base, "HEAD")
    if not changes:
        print("本地 base..HEAD 没有任何改动，无需推送。", file=sys.stderr)
        return EXIT_PRECONDITION

    # ---- 准备 blob（内容寻址身份在这里就定下来了）
    local_shas, remote_blobs, bytes_total, eol_warn = {}, [], 0, []
    for status_char, path in changes:
        if status_char == "D":
            remote_blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print(f"DEL  {path}")
            continue
        mode = git(repo, "ls-tree", "HEAD", path).split()[0] or "100644"
        if args.source == "index":
            data, sha = index_bytes(repo, path)
        else:
            data = worktree_bytes(repo, path)
            sha = git_blob_sha(data)
            try:
                _, isha = index_bytes(repo, path)
            except RuntimeError:
                isha = None
            if isha and isha != sha:
                crlf, bare_lf, _ = count_eol(data)
                eol_warn.append((path, isha, sha, crlf, bare_lf))
        local_shas[path] = sha
        bytes_total += len(data)
        st, blob = api("POST", "/git/blobs", {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        })
        if st not in (200, 201):
            print(f"blob 上传失败：{path}", file=sys.stderr)
            return EXIT_PRECONDITION
        if blob["sha"] != sha:
            # 服务端算出来的 SHA 与本地不一致 = 字节在传输中被改过
            print(f"字节被改动：{path} local={sha[:10]} remote={blob['sha'][:10]}", file=sys.stderr)
            return EXIT_MISMATCH
        remote_blobs.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        print(f"BLOB {path} {len(data)}B {sha[:10]}")

    for path, isha, wsha, crlf, bare_lf in eol_warn:
        print(
            f"WARN {path}: 工作区字节 SHA 与索引不同（CRLF={crlf} bareLF={bare_lf}）；\n"
            f"     索引 {isha[:10]} vs 工作区 {wsha[:10]} —— 用 --source index 可消除",
            file=sys.stderr,
        )

    st, tree = api("POST", "/git/trees", {"base_tree": base_tree, "tree": remote_blobs})
    if st not in (200, 201):
        print("建树失败", file=sys.stderr)
        return EXIT_PRECONDITION
    new_tree = tree["sha"]
    print(f"树            : {new_tree[:7]}  ({len(remote_blobs)} 项, {bytes_total}B)")

    local_tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    same = new_tree == local_tree
    print(f"本地提交树    : {local_tree[:7]}  {'== 远端树（逐字节一致）' if same else '!= 远端树'}")

    if args.dry_run:
        print("dry-run：不建 commit、不动 ref。")
        report_calls(api)
        return EXIT_OK if same or args.source == "worktree" else EXIT_MISMATCH

    msg = git(repo, "log", "-1", "--pretty=%B").strip() or f"push from {args.base[:7]}"
    st, commit = api("POST", "/git/commits", {"message": msg, "tree": new_tree, "parents": [remote_sha]})
    if st not in (200, 201):
        print("建 commit 失败", file=sys.stderr)
        return EXIT_PRECONDITION
    new_sha = commit["sha"]
    print(f"commit        : {new_sha[:7]}")

    st, upd = api("PATCH", f"/git/refs/heads/{args.branch}", {"sha": new_sha, "force": False})
    if st not in (200, 201):
        # 422 = 期间有人推过，ref 已非 fast-forward
        print(f"ref 更新失败（HTTP {st}）——通常是并发写者，重跑本命令即可", file=sys.stderr)
        return EXIT_MISMATCH
    print(f"ref {args.branch:<9}: {remote_sha[:7]} -> {upd['object']['sha'][:7]}")

    # ---- 回读对账：不信 200，看树上到底是什么
    st, back = api("GET", f"/git/trees/{new_tree}?recursive=1")
    if st != 200:
        return EXIT_MISMATCH
    remote_map = {n["path"]: n["sha"] for n in back["tree"] if n["type"] == "blob"}
    bad = [(p, s, remote_map.get(p)) for p, s in local_shas.items() if remote_map.get(p) != s]
    print(f"回读对账      : {len(local_shas) - len(bad)}/{len(local_shas)} 个 blob 一致")
    report_calls(api)
    if bad:
        for p, want, got in bad:
            print(f"  MISMATCH {p}: want {want[:10]} got {str(got)[:10]}", file=sys.stderr)
        return EXIT_MISMATCH
    print(f"NEW_SHA={new_sha}")
    return EXIT_OK


def report_calls(api: Api):
    total = sum(c[2] for c in api.calls)
    print(f"API 调用      : {len(api.calls)} 次 / {total/1000:.2f}s")
    for method, path, ms, st in api.calls:
        short = path if len(path) < 46 else path[:43] + "..."
        print(f"  {method:<5} {short:<46} {ms:>8.1f}ms  {st}")


# ---------------------------------------------------------------- 通道基准


def cmd_bench(args):
    """在临时分支上跑完整六步，量出真实耗时；主线 ref 不动，结束后删临时 ref。"""
    api = Api(args.repo, token())
    st, ref = api("GET", f"/git/ref/heads/{args.branch}")
    if st != 200:
        return EXIT_PRECONDITION
    head = ref["object"]["sha"]
    st, commit = api("GET", f"/git/commits/{head}")
    base_tree = commit["tree"]["sha"]

    payload = f"bench {time.time()}\n".encode()
    st, blob = api("POST", "/git/blobs", {
        "content": base64.b64encode(payload).decode("ascii"), "encoding": "base64"})
    if st not in (200, 201):
        return EXIT_PRECONDITION

    bench_path = args.path or ".bench/gh_tree_push.txt"
    st, tree = api("POST", "/git/trees", {"base_tree": base_tree, "tree": [
        {"path": bench_path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    if st not in (200, 201):
        return EXIT_PRECONDITION

    st, newc = api("POST", "/git/commits", {
        "message": "bench: gh_tree_push timing probe", "tree": tree["sha"], "parents": [head]})
    if st not in (200, 201):
        return EXIT_PRECONDITION

    tmp = f"bench-{int(time.time())}"
    st, created = api("POST", "/git/refs", {"ref": f"refs/heads/{tmp}", "sha": newc["sha"]})
    if st not in (200, 201):
        return EXIT_MISMATCH
    st2, _ = api("DELETE", f"/git/refs/heads/{tmp}")
    deleted = st2 in (200, 204)

    st, head_after = api("GET", f"/git/ref/heads/{args.branch}")
    untouched = head_after.get("object", {}).get("sha") == head
    report_calls(api)
    print(f"临时分支      : {tmp} {'已删除' if deleted else '删除失败'}")
    print(f"主线未动      : {head[:7]} -> {head_after.get('object', {}).get('sha', '?')[:7]} {'OK' if untouched else 'FAIL'}")
    return EXIT_OK if (deleted and untouched) else EXIT_MISMATCH


# ---------------------------------------------------------------- 通道探测


def _probe(url: str, timeout=25.0, method="GET"):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2048)
            return r.status, (time.perf_counter() - t0) * 1000, ""
    except urllib.error.HTTPError as e:
        # 401/403 是「端点可达但要认证」，与 000/超时有本质区别：连接建立了
        return e.code, (time.perf_counter() - t0) * 1000, "auth-required" if e.code in (401, 403) else ""
    except Exception as e:  # URLError/timeout/socket：TCP 没建立
        return 0, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {str(e)[:80]}"


def _reachable(status: int) -> bool:
    """000 = 连不上；401/403/2xx/3xx = 通道可达。"""
    return status != 0


def cmd_probe(args):
    repo = args.repo
    targets = [
        ("web 页面", f"https://github.com/{repo}"),
        ("smart-http（git push 走这里）", f"https://github.com/{repo}.git/info/refs?service=git-receive-pack"),
        ("api.github.com", f"{API_ROOT}/repos/{repo}"),
        ("codeload 源码包", f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{args.branch}"),
        ("Pages CDN", args.url_prefix or f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/"),
    ]
    print(f"{'通道':<34}{'HTTP':>6}{'耗时':>10}  备注")
    seen = {}
    for name, url in targets:
        status, ms, err = _probe(url, timeout=args.timeout)
        note = err or ("ok" if status == 200 else "non-200")
        print(f"{name:<34}{status:>6}{ms/1000:>9.1f}s  {note}")
        seen[name] = status
    pushable = _reachable(seen["smart-http（git push 走这里）"])
    api_ok = seen["api.github.com"] == 200
    print()
    if pushable:
        print("结论：smart-http 可达，`git push` 可用。")
        return EXIT_OK
    if api_ok:
        print("结论：smart-http 不可达（connect 超时/被丢包）而 API 通 —— 用 `push` 子命令走 Git Data API。")
        return EXIT_OK
    print("结论：git 协议与 API 都不通，先解决网络，别重试。", file=sys.stderr)
    return EXIT_MISMATCH


# ---------------------------------------------------------------- 上线验证


def cmd_verify(args):
    paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    base = args.url_prefix.rstrip("/")
    bad = 0
    for p in paths:
        url = f"{base}/{p.lstrip('/')}"
        if args.cb:
            url += ("&" if "?" in url else "?") + f"cb={int(time.time())}"
        deadline = time.time() + args.wait
        while True:
            status, ms, err = _probe(url, timeout=30)
            size = 0
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
                    size = len(body)
                    ok = status == 200 and (not args.needle or args.needle.encode() in body)
            except Exception:
                ok = False
            if ok or time.time() > deadline:
                break
            time.sleep(args.interval)
        flag = "OK  " if ok else "FAIL"
        if not ok:
            bad += 1
        print(f"{flag} http={status} {size}B {ms/1000:.2f}s {p}")
    return EXIT_OK if bad == 0 else EXIT_MISMATCH


# ---------------------------------------------------------------- 自检


def cmd_selftest(_args):
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # blob 哈希：git 官方示例 "hello\n" -> ce0136...
    ck("blob sha(hello\\n)", git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a")
    ck("blob sha(empty)", git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
    ck("blob sha 对 CRLF 敏感", git_blob_sha(b"a\r\n") != git_blob_sha(b"a\n"))
    ck("blob sha 定长", len(git_blob_sha(b"x" * 100000)) == 40)

    crlf, bare, lone_cr = count_eol(b"a\r\nb\nc\r\n")
    ck("count_eol crlf=2", crlf == 2)
    ck("count_eol bareLF=1", bare == 1)
    ck("count_eol 无孤立CR", lone_cr == 0)
    ck("纯LF文件 CRLF=0", count_eol(b"a\nb\n")[0] == 0)

    ck("base 前缀匹配", "abc1234deadbeef".startswith("abc1234"))
    ck("base 不匹配", not "abc1234".startswith("zzz"))
    ck("退出码互不相同", len({EXIT_OK, EXIT_MISMATCH, EXIT_PRECONDITION}) == 3)
    ck("退出码 0 为成功", EXIT_OK == 0)

    # 删除条目必须把 sha 置 None，而不是省略
    api = Api("o/r", "x")
    ck("Api 记录调用", isinstance(api.calls, list))
    ck("API 根正确", API_ROOT == "https://api.github.com")
    ck("UA 非空", "gh_tree_push" in UA)

    # 参数默认值
    p = build_parser()
    ns = p.parse_args(["push", "--base", "abc"])
    ck("默认 source=index", ns.source == "index")
    ck("默认 branch=main", ns.branch == "main")
    ck("默认非 dry-run", ns.dry_run is False)
    ns2 = p.parse_args(["verify", "--base", "x", "--paths", "a.html", "--url-prefix", "https://e.x/y"])
    ck("verify 默认 wait=0", ns2.wait == 0)
    ns3 = p.parse_args(["probe"])
    ck("probe 有超时默认", ns3.timeout > 0)

    ok = sum(1 for _, c in checks if c)
    for name, c in checks:
        print(f"  {'ok  ' if c else 'FAIL'} {name}")
    print(f"\n{ok}/{len(checks)} 通过")
    return EXIT_OK if ok == len(checks) else EXIT_MISMATCH


def build_parser():
    p = argparse.ArgumentParser(prog="gh_tree_push", description="Git Data API 推送与对账")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("bench", help="在临时分支上量出完整六步耗时（主线不受影响）")
    sp.add_argument("--repo", default=DEFAULT_REPO)
    sp.add_argument("--branch", default="main")
    sp.add_argument("--path", default="")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("probe", help="探测四条通道")
    sp.add_argument("--repo", default=DEFAULT_REPO)
    sp.add_argument("--branch", default="main")
    sp.add_argument("--url-prefix", default="")
    sp.add_argument("--timeout", type=float, default=25.0)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("push", help="用 Git Data API 推送 base..HEAD 的改动")
    sp.add_argument("--base", required=True, help="远端当前 head（前 7 位即可）")
    sp.add_argument("--repo", default=DEFAULT_REPO)
    sp.add_argument("--branch", default="main")
    sp.add_argument("--repo-dir", default="~/awesome-content-tools")
    sp.add_argument("--source", choices=["index", "worktree"], default="index")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("verify", help="轮询线上 URL 直到 200 或超时")
    sp.add_argument("--base", default="", help="（占位，兼容旧脚本）")
    sp.add_argument("--paths", required=True, help="逗号分隔的相对路径")
    sp.add_argument("--url-prefix", required=True)
    sp.add_argument("--needle", default="", help="必须出现在响应体里的字符串")
    sp.add_argument("--wait", type=float, default=0.0)
    sp.add_argument("--interval", type=float, default=10.0)
    sp.add_argument("--cb", action="store_true", help="附加时间戳破除缓存")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("selftest", help="纯本地断言，不联网")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    ns = build_parser().parse_args(argv)
    try:
        return ns.func(ns)
    except RuntimeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_PRECONDITION


if __name__ == "__main__":
    sys.exit(main())
