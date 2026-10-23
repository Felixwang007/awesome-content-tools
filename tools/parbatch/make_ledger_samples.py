# -*- coding: utf-8 -*-
"""生成真实 ledger：8 个写者并发 / 串行地读改写同一个 state.json，
每次调用记录 start/end 的单调时钟，落成 jsonl 给 par_batch.py ledger 对账。

产出：
  samples/ledger_parallel.jsonl   —— 并发版（应检出竞态，且计数丢失）
  samples/ledger_serial.jsonl     —— 串行版（应无重叠，计数正确）
"""
import os, sys, json, time, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORKERS, ROUNDS = 8, 10

WORKER = r'''
import json, os, sys, time
path, ledger, wid = sys.argv[1], sys.argv[2], sys.argv[3]
start = time.monotonic()
with open(path) as f:
    v = int(json.load(f)["n"])
time.sleep(0.002)
with open(path, "w") as f:
    json.dump({"n": v + 1}, f)
end = time.monotonic()
with open(ledger, "a", encoding="utf-8") as f:
    f.write(json.dumps({"job": wid, "path": path, "mode": "write",
                        "start": round(start, 6), "end": round(end, 6)}) + "\n")
'''

def run(mode):
    d = tempfile.mkdtemp(prefix="parbatch_sample_")
    state = os.path.join(d, "state.json")
    ledger = os.path.join(d, "ledger.jsonl")
    wf = os.path.join(d, "_w.py")
    with open(wf, "w", encoding="utf-8") as f:
        f.write(WORKER)
    with open(state, "w") as f:
        json.dump({"n": 0}, f)
    open(ledger, "w").close()
    t0 = time.perf_counter()
    if mode == "parallel":
        ps = [subprocess.Popen([sys.executable, wf, state, ledger, "w%d" % i]) for i in range(WORKERS)]
        for p in ps: p.wait()
    else:
        for i in range(WORKERS):
            subprocess.run([sys.executable, wf, state, ledger, "w%d" % i])
    wall = time.perf_counter() - t0
    with open(state) as f:
        n = json.load(f)["n"]
    lines = [json.loads(x) for x in open(ledger, encoding="utf-8") if x.strip()]
    out = os.path.join(HERE, "samples", "ledger_%s.jsonl" % mode)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in lines:
            f.write(json.dumps(r) + "\n")
    # 统计真实重叠对数（同一路径、至少一方写、区间真重叠）
    overl = 0
    for i in range(len(lines)):
        for k in range(i + 1, len(lines)):
            a, b = lines[i], lines[k]
            if a["start"] < b["end"] and b["start"] < a["end"]:
                overl += 1
    print(json.dumps({"mode": mode, "writers": WORKERS, "expected_n": WORKERS,
                      "final_n": n, "lost": WORKERS - n, "records": len(lines),
                      "overlapping_pairs": overl, "wall_s": round(wall, 3),
                      "ledger": out}, ensure_ascii=False))

for m in ("parallel", "serial"):
    run(m)
