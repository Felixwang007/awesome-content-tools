#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo.py — 用一个 4 步工具链演示：录制磁带、离线回放、漂移检出、落空补录、注入式回放。"""
import json, os, time
from tape import Tape, TapeMiss, TapeDrift

HERE = os.path.dirname(os.path.abspath(__file__))
TAPE = os.path.join(HERE, "tape.jsonl")
OUT = os.path.join(HERE, "out")
REPORT = os.path.join(OUT, "report.md")

COST = {"summarize": 0.012}          # 只有 LLM 这一步花钱，其余是本地/免费调用

def fetch_doc(url):
    time.sleep(0.06)                                    # 模拟真实网络
    return {"url": url, "status": 200, "text": "agent 长任务需要账本，" * 40}

def search_index(query, top_k=3):
    time.sleep(0.04)
    return [{"title": f"doc-{i}", "score": round(0.9 - i * 0.1, 2)} for i in range(top_k)]

def summarize(text, max_words):
    time.sleep(0.15)                                    # 模拟 LLM 调用（唯一花钱的一步）
    return {"words": max_words, "text": text[:20] + f"…[截断到{max_words}词]"}

def write_report(path, body):
    os.makedirs(OUT, exist_ok=True)                     # 真实副作用：写盘
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return {"bytes": len(body.encode("utf-8")), "file": os.path.basename(path)}

TOOLS = {"fetch_doc": (fetch_doc, False), "search_index": (search_index, False),
         "summarize": (summarize, False), "write_report": (write_report, True)}

def run(tape, max_words=120, extra_step=False):
    """一次 agent 会话。v2 把 max_words 120→80；v3 在末尾多一步检索。"""
    def step(tool, **args):
        fn, side = TOOLS[tool]
        return tape.call(tool, args, fn, side_effect=side)

    doc = step("fetch_doc", url="https://example.com/agent-long-task")
    if doc["status"] != 200:
        return {"mode": "degraded", "reason": f"上游 {doc['status']}"}
    hits = step("search_index", query="agent 断点续跑", top_k=3)
    s = step("summarize", text=doc["text"], max_words=max_words)
    res = step("write_report", path=REPORT, body=s["text"] + f"\n参考 {len(hits)} 条\n")
    if extra_step:
        step("search_index", query="Tape 漂移检测", top_k=2)   # v3 新增的一步
    return res

def banner(t):
    print("\n" + "=" * 66 + f"\n{t}\n" + "=" * 66)

if os.path.exists(TAPE):
    os.remove(TAPE)

banner("PASS 1  录制 mode=record（工具真的会跑）")
t = Tape(TAPE, mode="record")
t0 = time.perf_counter(); r1 = run(t); wall1 = round((time.perf_counter() - t0) * 1000, 2)
print("结果:", json.dumps(r1, ensure_ascii=False))
print("统计:", json.dumps(t.stats(), ensure_ascii=False))
print(f"墙钟 {wall1} ms   超参数花费 ${sum(COST.values())}   磁带行数 {sum(1 for _ in open(TAPE, encoding='utf-8'))}")
print(f"真实副作用: report.md {os.path.getsize(REPORT)} bytes")

banner("PASS 2  回放 mode=replay（工具一次都不跑）")
t = Tape(TAPE, mode="replay"); os.remove(REPORT)
t0 = time.perf_counter(); r2 = run(t); wall2 = round((time.perf_counter() - t0) * 1000, 2)
print("结果与录制逐字节一致:", r2 == r1)
print("统计:", json.dumps(t.stats(), ensure_ascii=False))
print(f"墙钟 {wall2} ms   超参数花费 $0    report.md 被重新写了吗: {os.path.exists(REPORT)}")

banner("PASS 3  漂移检出（v2: max_words 120 → 80，同一序号位置参数变了）")
t = Tape(TAPE, mode="replay")
try:
    run(t, max_words=80)
except TapeDrift as e:
    print("TapeDrift 捕获:")
    print("  " + str(e).replace("\n", "\n  "))
print("drift 记录:", json.dumps(t.drift, ensure_ascii=False))
print("统计:", json.dumps(t.stats(), ensure_ascii=False))

banner("PASS 4  磁带落空：严格模式报错 vs replay+record 自动补录")
t = Tape(TAPE, mode="replay")
try:
    run(t, extra_step=True)
except TapeMiss as e:
    print("严格模式 ->", e)
print("统计:", json.dumps(t.stats(), ensure_ascii=False))

t = Tape(TAPE, mode="replay+record")
r4 = run(t, extra_step=True)          # 已录制的 4 步走磁带，新的一步真跑并追加
print("补录模式结果:", json.dumps(r4, ensure_ascii=False))
print("统计:", json.dumps(t.stats(), ensure_ascii=False))
print("磁带行数:", sum(1 for _ in open(TAPE, encoding="utf-8")))

banner("PASS 5  注入式回放：把 fetch_doc 替换成上游 500，离线复现降级分支")
t = Tape(TAPE, mode="replay")
t.override("fetch_doc", {"url": "https://example.com/agent-long-task"},
           {"url": "https://example.com/agent-long-task", "status": 500, "text": ""})
if os.path.exists(REPORT):
    os.remove(REPORT)
r5 = run(t)
print("结果:", json.dumps(r5, ensure_ascii=False))
print("统计:", json.dumps(t.stats(), ensure_ascii=False))
print("降级分支没有产生副作用:", not os.path.exists(REPORT))

print("\n磁带首行（脱敏后的真实内容）:")
print(open(TAPE, encoding="utf-8").readline().strip()[:200] + " …")
