#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Push awesome-content-tools changes via GitHub Contents API (git protocol blocked)."""
import base64
import json
import os
import subprocess
import sys

REPO = "Felixwang007/awesome-content-tools"
BRANCH = "main"
MSG = "add: AI Agent安全实战指南文章+首页列表+站点地图更新"

# (local_path, remote_path, sha_if_exists_or_None)
FILES = [
    ("docs/articles/ai-agent-security-guide.html", "docs/articles/ai-agent-security-guide.html", None),
    ("docs/index.html", "docs/index.html", "0b9c9ac428584ba74a0393b9759295d7e66dcf72"),
    ("docs/sitemap.xml", "docs/sitemap.xml", "d6ad20d0c72e224c4771c70d3774b30b08b8f010"),
]

def gh_api(method, path, payload=None):
    cmd = ["gh", "api", "-X", method, path]
    if payload is not None:
        tmp = "/tmp/gh_payload.json"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        cmd += ["--input", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"FAIL {method} {path}: {r.stderr.strip()}")
        return None
    return json.loads(r.stdout) if r.stdout.strip() else {}

for local, remote, sha in FILES:
    with open(local, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    payload = {
        "message": MSG,
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    result = gh_api("PUT", f"repos/{REPO}/contents/{remote}", payload)
    if result is None:
        sys.exit(1)
    print(f"OK  {remote} -> {result.get('commit', {}).get('sha', '?')[:8]}")
print("DONE")
