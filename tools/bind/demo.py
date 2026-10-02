#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo.py -- 生成一份可复现的绑定审计样例账本，并跑出审计结论。

样例账本里每个 job 对应一种真实故障形态（形态来自实机观察，数据为构造样例）：
  job-publish-verify   4 次运行：1 次产物核验通过 + 3 次绿灯无产物（连续自报 ok，外部无产物）
  job-artifact-file    3 次运行：产物文件每次都真实存在 → 全绑定
  job-url-declared     2 次运行：产物是 URL，只声明不核验 → DECLARED，不算绑定
  job-mirage           1 次运行：自报 failed 但产物在 → 状态字段失真（反方向）
  job-presence-only    12 次运行：规则被读到 12 次，产物 0 件 → 在场≠绑定
  job-honest-fail      1 次运行：自报 failed 且无产物 → 诚实的失败，成本最低

用法: python demo.py [工作目录]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "bind_audit.py")


def build(workdir):
    os.makedirs(workdir, exist_ok=True)
    art_dir = os.path.join(workdir, "artifacts")
    os.makedirs(art_dir, exist_ok=True)

    # 真实产物：一个 JSON 记录 + 一个 append-only 账本
    published = os.path.join(art_dir, "published.json")
    with open(published, "w", encoding="utf-8") as f:
        json.dump({"gid": "0000000000000000001", "title_sample": "样例产物",
                   "url": "https://example.com/a/1"}, f, ensure_ascii=False)
    ledger = os.path.join(art_dir, "publish_ledger.jsonl")
    with open(ledger, "w", encoding="utf-8") as f:
        for gid in ("0000000000000000001", "0000000000000000002"):
            f.write(json.dumps({"gid": gid, "at": "2026-09-28T08:20:00+08:00"}) + "\n")

    recs = []

    def add(run_id, job, at, self_report, read_side, evidence):
        recs.append({"run_id": run_id, "job": job, "at": at, "self_report": self_report,
                     "read_side": read_side, "evidence": evidence})

    # 1) 产物核验通过的那一次 + 三次绿灯无产物
    add("pv-01", "job-publish-verify", "2026-09-20T08:20:00+08:00", "ok", 1,
        [{"kind": "file", "ref": published}])
    for i, day in enumerate(("2026-09-21", "2026-09-22", "2026-09-23"), start=2):
        add(f"pv-{i:02d}", "job-publish-verify", f"{day}T08:20:00+08:00", "ok", 1, [])

    # 2) 产物文件每次都在
    for i, day in enumerate(("2026-09-27", "2026-09-28", "2026-09-29"), start=1):
        add(f"af-{i:02d}", "job-artifact-file", f"{day}T09:00:00+08:00", "ok", 1,
            [{"kind": "file", "ref": published},
             {"kind": "ledger", "ref": ledger, "match": "0000000000000000001"}])

    # 3) 只声明 URL，不核验
    for i, day in enumerate(("2026-09-26", "2026-09-27"), start=1):
        add(f"ud-{i:02d}", "job-url-declared", f"{day}T10:00:00+08:00", "ok", 1,
            [{"kind": "url", "ref": f"https://example.com/post/{i}"}])

    # 4) 反方向失真
    add("mr-01", "job-mirage", "2026-09-25T11:00:00+08:00", "failed", 1,
        [{"kind": "file", "ref": published}])

    # 5) 在场≠绑定：规则被读到 12 次，产物 0 件
    for i in range(1, 13):
        add(f"po-{i:02d}", "job-presence-only", f"2026-09-{i:02d}T12:00:00+08:00", "ok", 1, [])

    # 6) 诚实的失败
    add("hf-01", "job-honest-fail", "2026-09-24T13:00:00+08:00", "failed", 1, [])

    path = os.path.join(workdir, "runs.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


if __name__ == "__main__":
    workdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_demo")
    ledger_path = build(workdir)
    print(f"样例账本: {ledger_path}  ({sum(1 for _ in open(ledger_path, encoding='utf-8'))} 条记录)")
    print()
    r = subprocess.run([sys.executable, AUDIT, "audit", ledger_path,
                        "--now", "2026-10-02T12:00:00+08:00"],
                       capture_output=True, text=True, encoding="utf-8")
    print(r.stdout)
    print(f"[bind_audit 退出码 = {r.returncode}]")
    sys.exit(0)
