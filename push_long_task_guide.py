#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Push the AI agent long-task resume guide + site updates to GitHub via Contents API."""
import base64, json, os, subprocess, sys, urllib.request, urllib.error

REPO = "Felixwang007/awesome-content-tools"
BRANCH = "main"
TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()

def gh_api(method, path, payload=None):
    url = f"https://api.github.com/repos/{REPO}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "curl/8.0")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"  HTTP {e.code}: {body[:400]}", file=sys.stderr)
        return e.code, {}

def get_sha(path):
    status, data = gh_api("GET", f"/contents/{path}?ref={BRANCH}")
    return data.get("sha") if status == 200 else None

def push_file(path, message):
    with open(path, "rb") as f:
        content = f.read()
    sha = get_sha(path)
    payload = {"message": message, "content": base64.b64encode(content).decode(), "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    status, data = gh_api("PUT", f"/contents/{path}", payload)
    ok = status in (200, 201)
    print(f"{'OK  ' if ok else 'FAIL'} {path} ({len(content)} bytes, HTTP {status})")
    return ok

MSG = ("Add AI agent long-task resume guide (2026-09): resumable execution unit (manifest / ledger / "
       "idempotency key / resume entry), granularity selection table, append-only JSONL ledger field "
       "design, three idempotency patterns with tmp+atomic-rename guard, config fingerprint against stale "
       "state, ~60-line ResumableRunner, token-bucket throttling + circuit breaker, three-way "
       "reconciliation and absence detection, five pitfall table, 15-item checklist")
FILES = [
    ("docs/articles/agent-long-task-resume-guide.md", MSG),
    ("docs/articles/agent-long-task-resume-guide.html", MSG),
    ("docs/index.html", "Add agent-long-task-resume-guide to latest articles list"),
    ("docs/sitemap.xml", "Add agent-long-task-resume-guide.html URL to sitemap"),
    ("convert_articles.py", "Register agent-long-task-resume-guide metadata in article converter"),
    ("README.md", "README: add long-task resume guide to featured articles list"),
]

if __name__ == "__main__":
    if not os.path.isdir("docs"):
        sys.exit("run from repo root")
    all_ok = True
    for p, m in FILES:
        if not push_file(p, m):
            all_ok = False
    print("ALL OK" if all_ok else "SOME FAILED")
    sys.exit(0 if all_ok else 1)
