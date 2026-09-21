# AI Agent长任务实战指南：断点续跑、幂等与进度对账

> 一个跑 4 小时的批量任务，在第 3 小时因为网络抖动挂掉。这时候真正值钱的问题不是"怎么让它不挂"，而是"**怎么知道它跑到哪了，以及剩下的 1 小时能不能只跑剩下的**"。
> 这篇文章讲的是长任务（批量生成、批量爬取、数据集构建、批量转码/上传）的工程化：清单、账本、幂等键、状态指纹、对账。全文是踩坑后总结的，不是教科书。

---

## 一、先说结论：长任务最大的成本不是算力，是"不知道"

短任务（<60s）的失败处理方式很简单：**整段重跑**。代价可接受。

长任务不行。一个 200 项、每项 90 秒的批量任务：

- 总时长 5 小时
- 第 173 项挂掉，前面 172 项的结果如果只存在内存或只存在"最后一步的汇总文件"里，这 172 项全部作废
- 重跑的边际成本 = 172 × 90s ≈ 4.3 小时 + 对应 API/电费成本

于是长任务失败会发生两件事，而且第二件更贵：

1. **重跑成本线性叠加**（每挂一次重跑一遍）
2. **进度信息不可信**——你不知道是"跑完了"还是"看起来跑完了"

第 2 点是长任务的第一性问题。工程上有一句很实用的话：**没有账本的长任务，失败一次就从零开始；有账本的长任务，失败一次只损失"正在跑的那一项"。**

### 长任务和短任务的设计分界线

| 维度 | 短任务 | 长任务 |
|------|--------|--------|
| 恢复策略 | 整段重跑 | 按项续跑 |
| 状态存放 | 内存 / 局部变量 | 外部账本（落盘或数据库） |
| 失败处理 | 抛异常 → 退出码 | 记失败项 → 继续 → 最后统一重试 |
| 幂等要求 | 弱 | 强（写操作必须可重复执行） |
| 可观测性 | 结束时打日志 | 每一秒都能回答"现在跑到第几项" |
| 关键指标 | 是否成功 | 单位时间完成项数 + 失败项占比 |

**判据**：如果你的任务满足"单项耗时 × 项数 > 10 分钟"，就应该按长任务设计；如果"重跑一次的成本 > 10 分钟"，就必须按长任务设计。没有第三种。

---

## 二、把长任务定义成「可恢复的执行单元」：四要素

不管任务形态是"批量下载 500 个页面"还是"逐场景生成 44 段视频"，能恢复的长任务都要有这四样东西：

```
① 清单 manifest     —— 这次要做哪些项（不可变，带版本号）
② 账本 ledger       —— 每项做到了什么状态（只追加，不改写历史）
③ 幂等键 idem_key   —— 重复执行同一项时，能识别出"这就是同一件事"
④ 恢复入口 resume   —— 一个命令：读清单 + 读账本 → 得到待办集合 → 开跑
```

这四样缺任何一样，"断点续跑"就退化成一个口号。

### 清单必须是不可变的

清单变了但账本没变，就会出现"账本说 A 项完成，但新清单里 A 项的定义已经换了"——这是最隐蔽的一类脏数据。

做法：清单本身带一个内容哈希，写进账本的每一行。

```python
import hashlib, json

def manifest_hash(items: list[dict]) -> str:
    """清单内容哈希：项顺序、内容任一变化都会变"""
    blob = json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]
```

跑之前先比一下：账本里记录的 `mh` 和当前清单的 `mh` 不一致 → **要么换新账本，要么显式确认复用**。不允许静默复用。

---

## 三、执行粒度：四档，选错就白干

"断点续跑"不是一个粒度，是四个。粒度越细，恢复成本越低，但记账开销越高。

| 粒度 | 恢复成本 | 记账开销 | 适用场景 |
|------|----------|----------|----------|
| 整任务重跑 | 100% | 0 | 总时长 < 10 分钟 |
| 分段（每 N 项一个检查点） | 1/N | 极低 | 项之间强耦合（有共享上下文） |
| 逐项（每项一个检查点） | 1 项 | 低（每项一行 JSON） | **默认推荐**：项之间独立 |
| 增量（项内可续传） | < 1 项 | 高（要处理部分产物） | 单项耗时很长（>10 分钟，如大文件下载、长视频渲染） |

**选择规则**：

1. 项之间独立 → 逐项。这覆盖 80% 的批量任务。
2. 项之间需要共享上下文（比如第二项要用第一项的输出做提示词）→ 分段，段大小 = 一次能安全重跑的量。
3. 单项就要跑十几分钟（视频渲染、模型训练分片）→ 看该工具本身支不支持断点续传。支持就用增量（如 `ffmpeg` 分段、`curl -C -`、数据集分片训练）；不支持就承认它是"不可恢复原子项"，靠**缩短单项**来降风险（把 1 个 30 分钟的任务切成 6 个 5 分钟的子任务）。

> 真实教训：一批 44 段的视频生成任务，如果按"整批提交、等全部完成"来做，任何一段挂掉都要重新排队整批。改成"逐段提交 + 逐段记状态"之后，挂掉的重新提交成本是 4 分钟，不是 3 小时。

---

## 四、账本设计：只追加（append-only），不要改写

账本的正确形态是 **JSONL（一行一条 JSON）**，只追加，不修改历史行。

```jsonl
{"ts":"2026-09-21T09:14:02+08:00","mh":"a3f9c1b2e004","item":"sc_017","attempt":1,"status":"ok","artifact":"out/sc_017.mp4","bytes":4821093,"cost_usd":0.031,"dur_s":87.4}
{"ts":"2026-09-21T09:15:31+08:00","mh":"a3f9c1b2e004","item":"sc_018","attempt":1,"status":"fail","err":"timeout: read 30s","dur_s":30.2}
{"ts":"2026-09-21T09:16:44+08:00","mh":"a3f9c1b2e004","item":"sc_018","attempt":2,"status":"ok","artifact":"out/sc_018.mp4","bytes":5011777,"cost_usd":0.029,"dur_s":73.1}
```

### 字段设计（最小可用集）

| 字段 | 必填 | 说明 |
|------|------|------|
| `ts` | ✅ | 带时区的 ISO 时间戳。**不带时区的本地时间会在跨天/跨时区复盘时坑死你** |
| `mh` | ✅ | 清单哈希，防"清单换了账本没换" |
| `item` | ✅ | 项 ID。稳定、可排序、不含路径分隔符 |
| `attempt` | ✅ | 第几次尝试（从 1 开始）。**重试也是历史，不能覆盖** |
| `status` | ✅ | `ok` / `fail` / `skip`。只允许这三种，不要发明 `partial`（见第七节） |
| `artifact` | ok 时必填 | 产物路径或 URL。**账本里没有 artifact 的 ok 是假成功** |
| `err` | fail 时必填 | 错误摘要（截断到 200 字符）+ 错误类别前缀（`timeout:` / `http403:` / `oom:`） |
| `dur_s` | 建议 | 单项耗时，用来估算剩余时间 |
| `cost_usd` | 建议 | 单项成本，用来做预算熔断 |
| `bytes` | 建议 | 产物大小，**用来抓"成功但产出为 0 字节"** |

### 为什么不用「数据库里一行、状态字段 UPDATE」

可以，但要注意两点：

1. **丢掉历史**。`status` 被反复覆盖后，"这项重试了几次、失败原因是什么"就永久消失了。长任务的失败模式诊断恰恰依赖这些。要么双写事件表，要么直接用 JSONL。
2. **并发写冲突**。SQLite 在多进程/多线程写的时候容易 `database is locked`。JSONL 用"一行一次 `write()` + 文件锁"或"每项一个文件+qps 合并"更省心。

**回放能力**是 JSONL 的隐藏价值：任何时刻可以用几行代码把账本重放成"当前状态视图"，而数据库里你只能看到最终快照。

```python
def current_state(ledger_path: str) -> dict[str, dict]:
    """把 append-only 账本压成当前状态（最后一次 attempt 说了算）"""
    state: dict[str, dict] = {}
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            state[r["item"]] = r          # 后写的覆盖记录，历史仍在文件里
    return state

def todo(items: list[dict], state: dict[str, dict], max_attempt: int = 3) -> list[dict]:
    out = []
    for it in items:
        s = state.get(it["id"])
        if s is None or (s["status"] == "fail" and s["attempt"] < max_attempt):
            out.append(it)
    return out
```

---

## 五、幂等：写操作必须能重复执行

断点续跑的前提是"同一项跑两遍，结果一样、副作用只发生一次"。三种写法，按可靠性排序：

### 写法 1：幂等键（推荐）

让下游接受一个调用方生成的幂等键，同一个键重复提交只生效一次。

```python
def idem_key(mh: str, item: str) -> str:
    return hashlib.sha256(f"{mh}:{item}".encode()).hexdigest()[:32]
```

- 提交任务、上传文件、发消息、写数据库，都带上这个键
- 服务端不支持幂等键时，自己在本地做一层（键 → 结局 的映射文件）

### 写法 2：先读回，再决定写不写（read-before-write）

```python
def ensure_artifact(item_id: str) -> str:
    path = f"out/{item_id}.json"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path                      # 已存在且非空 → 视为完成，不重复生成
    data = generate(item_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)                # 原子替换，防"半截文件被当成完成"
    return path
```

**注意 `os.replace` + `.tmp` 这个组合**：直接写目标文件的话，进程在第 50% 处被杀，会留下一个"存在但残缺"的文件，而"存在"就是幂等判据——于是残缺文件被永久当成成功产物。**先写临时文件、再原子改名**，是长任务里最便宜的防呆。

### 写法 3：自然键去重

产物自带唯一标识（URL、内容哈希、文件名含业务键）时用它去重。适合"结果集合"型任务。

```python
seen = {json.loads(l)["cid"] for l in open("out/items.jsonl", encoding="utf-8") if l.strip()}
if cand["cid"] in seen:
    continue
```

**别用写法 4："我记得刚才好像做过"**（内存标记 / 只靠日志）。进程一重启就失灵。

---

## 六、状态指纹：防「陈旧状态」这类最贵的脏数据

断点续跑有一个反直觉的风险：**它会把旧版本的产物当成当前版本的产物复用**。

场景：你改了提示词模板或生成参数，想重跑全部 200 项。但账本说 200 项都是 `ok`，于是 resume 逻辑把 200 项全部跳过——产物还是旧版本的。

解法：把"决定产出内容的东西"做成指纹，进账本的每一行。

```python
import hashlib, json, os

def config_fingerprint(config: dict) -> str:
    """影响产出的配置指纹：模型版本、提示词模板哈希、参数、下游 schema 版本"""
    keys = ["model", "prompt_template_hash", "temperature", "max_tokens", "output_schema_ver"]
    sub = {k: config.get(k) for k in keys}
    return hashlib.sha256(json.dumps(sub, sort_keys=True).encode()).hexdigest()[:12]
```

账本行里多两个字段：`cf`（配置指纹）、`v`（脚本版本）。resume 的判据从"这项 ok 过"变成：

> 这项 ok 过 **且** `mh` 相同 **且** `cf` 相同 **且** 产物文件存在且非空

任何一条不满足 → 重新执行。指纹变了的项要显式记账为"因指纹变化重跑"，否则复盘时你会很奇怪"为什么这项跑了两次"。

**指纹粒度别做太细**。把 `temperature` 甚至随机种子都算进去，会导致"每次跑都全量重跑"，续跑能力归零。经验值：指纹只包含**会导致产物语义变化**的东西——模型 ID、提示词模板、关键参数、输出结构版本。纯粹的日志级别、并发数、超时秒数**不进指纹**。

---

## 七、可复用的断点续跑执行器（约 60 行）

把上面所有东西合成一个小执行器。直接照抄可用。

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可恢复批量执行器：清单 + 账本 + 幂等 + 对账。"""
import hashlib, json, os, time, traceback
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

def now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")

class Ledger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())          # 崩溃前必须落盘

    def replay(self) -> dict[str, dict]:
        if not os.path.exists(self.path):
            return {}
        state = {}
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    state[r["item"]] = r
        return state

def artifact_ok(path: str | None) -> bool:
    return bool(path) and os.path.exists(path) and os.path.getsize(path) > 0

class ResumableRunner:
    def __init__(self, name: str, items: list[dict], cfg: dict,
                 run_one, out_dir: str = "out", max_attempt: int = 3):
        self.name, self.items, self.run_one = name, items, run_one
        self.out_dir, self.max_attempt = out_dir, max_attempt
        self.mh = hashlib.sha256(
            json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        self.cf = hashlib.sha256(
            json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        self.ledger = Ledger(f"state/{name}.jsonl")

    def pending(self) -> list[dict]:
        state, out = self.ledger.replay(), []
        for it in self.items:
            r = state.get(str(it["id"]))
            fresh = r and r.get("mh") == self.mh and r.get("cf") == self.cf
            if fresh and r["status"] == "ok" and artifact_ok(r.get("artifact")):
                continue                                    # 真完成，跳过
            if not fresh and r and r["status"] == "ok":
                out.append({**it, "_reason": "fingerprint_changed"})
                continue                                    # 陈旧状态，重跑
            if r and r["status"] == "fail" and r["attempt"] >= self.max_attempt:
                continue                                    # 超次数，交给人工
            out.append(it)
        return out

    def run(self, limit: int | None = None) -> dict:
        todo = self.pending()[:limit] if limit else self.pending()
        state0 = self.ledger.replay()
        print(f"[{self.name}] mh={self.mh} cf={self.cf} total={len(self.items)} "
              f"done={len(self.items) - len(todo)} todo={len(todo)}")
        stats = {"ok": 0, "fail": 0}
        for i, it in enumerate(todo, 1):
            iid = str(it["id"])
            attempt = (state0.get(iid, {}).get("attempt") or 0) + 1
            t0 = time.time()
            try:
                artifact = self.run_one(it)
                rec = {"ts": now(), "mh": self.mh, "cf": self.cf, "item": iid,
                       "attempt": attempt, "status": "ok", "artifact": artifact,
                       "bytes": os.path.getsize(artifact) if artifact and os.path.exists(artifact) else 0,
                       "dur_s": round(time.time() - t0, 1)}
                stats["ok"] += 1
            except Exception as e:
                rec = {"ts": now(), "mh": self.mh, "cf": self.cf, "item": iid,
                       "attempt": attempt, "status": "fail",
                       "err": f"{type(e).__name__}: {str(e)[:180]}",
                       "trace": traceback.format_exc()[-400:],
                       "dur_s": round(time.time() - t0, 1)}
                stats["fail"] += 1
            self.ledger.append(rec)                          # 无论成败都记账
            print(f"  [{i}/{len(todo)}] {iid} {rec['status']} ({rec['dur_s']}s)")
        return stats
```

**注意 `run()` 里的两个刻意的设计**：

1. **无论成功还是失败都写账本**。失败也是信息，且下次 resume 要靠 `attempt` 计数做熔断。
2. **异常被吞掉，不向上抛**。长任务里一个坏项不该终止整批——它应该被记账、被跳过、被最后统一处理。整批退出码反映的是"跑没跑完"，不是"有没有失败项"（失败项看账本统计）。

配合一个 `--retry-failed` 入口，长任务就具备了完整的自我恢复能力：

```bash
python run_batch.py                 # 首跑，断哪算哪
python run_batch.py                 # 直接再跑一遍（自动续跑，只补缺口）
python run_batch.py --retry-failed  # 只重试失败项（读取账本，max_attempt 内）
python run_batch.py --reconcile     # 只做对账，不执行
```

---

## 八、并发与限流：让失败是「局部」的

逐项执行是串行的，5 小时太长。加并发，但并发会引入三个新问题：

### 1. 限流（rate limit）

不要用 `time.sleep(1)` 这种手写节流。用令牌桶 + per-provider 独立桶：

```python
import threading, time

class TokenBucket:
    """线程安全的令牌桶：capacity 个突发配额，每秒补充 rate 个"""
    def __init__(self, rate: float, capacity: int):
        self.rate, self.cap, self.tokens = rate, capacity, float(capacity)
        self.lock, self.last = threading.Lock(), time.monotonic()

    def take(self, n: int = 1) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                need = (n - self.tokens) / self.rate
            time.sleep(min(need, 1.0))
```

不同供应商（模型 API / 图床 / 视频接口）各一个桶——它们的限额互不相干。

### 2. 并发度不等于线程数

对 IO 密集任务，并发度取 `限额 / 单项平均耗时` 的倒数关系。经验值：

- 有明确 QPS 上限的 API：并发 = QPS 上限 × 单次耗时（秒）× 0.7（留 30% 余量）
- 无明确上限的本地任务（渲染、CPU 计算）：并发 = CPU 核数 或 GPU 显存能容纳的批数
- **不确定就先用 2**。并发从 1 提到 2 的收益最大，2 → 4 收益减半，8 以上经常因为限流反而变慢。

### 3. 一个坏项不许拖死整批

三个保护：

- **单项超时**：每项都有硬超时（如 `timeout=90`），超时按 fail 记账，继续下一项
- **熔断**：连续 N 项失败（如 8 项）→ 停下，避免"配额用完/密钥失效"这类系统性故障把 500 项全部刷成 fail
- **失败隔离**：失败项和第 3 次重试项放到批末尾统一处理，不要在原位反复重试（会把 5 分钟修复拖成 40 分钟故障）

```python
consecutive_fail = 0
for it in todo:
    ...
    consecutive_fail = consecutive_fail + 1 if rec["status"] == "fail" else 0
    if consecutive_fail >= 8:
        print("连续 8 项失败，疑似系统性故障，停机")
        break
```

---

## 九、对账：长任务的最后一道门

跑完不等于跑对。长任务结束（以及每天定时）都要做一次**三方对账**：

```
清单（应该有哪些）  vs  账本（记录了哪些）  vs  实际产物（磁盘上真的有哪些）
```

三方对账要能回答这四个问题：

| 问题 | 判据 | 严重性 |
|------|------|--------|
| 有项漏跑 | 清单项在账本里没有任何记录 | 🔴 高 |
| 有项假成功 | 账本 `ok` 但产物不存在 / 0 字节 | 🔴 高 |
| 有孤儿产物 | 磁盘上有产物但账本没记录（手工生成的、或账本写失败） | 🟡 中 |
| 有项反复失败 | 同一项 `fail` 次数 ≥ 3 | 🟡 中 |

```python
def reconcile(items: list[dict], ledger_path: str, out_dir: str) -> dict:
    state = Ledger(ledger_path).replay()
    ids = {str(i["id"]) for i in items}
    missing  = sorted(i for i in ids if i not in state)
    fake_ok  = sorted(i for i, r in state.items()
                     if r["status"] == "ok" and not artifact_ok(r.get("artifact")))
    stuck    = sorted(i for i, r in state.items()
                      if r["status"] == "fail" and r["attempt"] >= 3)
    produced = {f.rsplit(".", 1)[0] for f in os.listdir(out_dir)} if os.path.isdir(out_dir) else set()
    orphan   = sorted(produced - ids)
    return {"missing": missing, "fake_ok": fake_ok, "stuck": stuck, "orphan": orphan}
```

### 缺勤检测：把"没有产物"变成事件

这是对账最有价值的一条延伸，也是定时长任务的关键：

> **"这次任务没产出"不是一个事件，"在预期时间窗口内该出现的产物不存在"才是一个事件。**

做法：给每个长任务声明期望窗口（如"每天 09:00 前必须有当天的账本文件，且 `ok` 数 ≥ 阈值"），让一个独立的检查器（不是长任务自己）来验：

```python
def absence_check(ledger_path: str, expect_ok: int, window_hours: int = 26) -> str | None:
    """返回 None=正常，否则返回告警文本"""
    if not os.path.exists(ledger_path):
        return f"账本文件不存在：{ledger_path}"
    rows = [json.loads(l) for l in open(ledger_path, encoding="utf-8") if l.strip()]
    if not rows:
        return "账本为空"
    latest = max(r["ts"] for r in rows)
    if (datetime.now(CST) - datetime.fromisoformat(latest)).total_seconds() > window_hours * 3600:
        return f"最近一次记账在 {latest}，已超过 {window_hours}h 无新记录"
    today_ok = sum(1 for r in rows if r["ts"][:10] == datetime.now(CST).date().isoformat()
                   and r["status"] == "ok")
    if today_ok < expect_ok:
        return f"今日 ok 数 {today_ok} < 期望 {expect_ok}"
    return None
```

**检查器必须独立于被检查的任务**。让任务自己汇报"我正常"，等于让被告当法官——任务挂掉的时候，恰恰是它没法汇报的时候。

---

## 十、五个真实踩坑对照表

| 现象 | 根因 | 修法 |
|------|------|------|
| 重跑后产物翻倍（同样的东西生成两遍） | 幂等判据是"内存里有没有跑过" | 幂等判据改成"产物存在且非空"；写临时文件 + `os.replace` 原子落盘 |
| resume 之后产出还是旧版本内容 | 账本没有配置指纹，改了提示词/参数后全部被跳过 | 账本行加 `cf`（配置指纹）+ `mh`（清单哈希），resume 判据包含两者 |
| 账本说 173 项 ok，实际磁盘只有 171 个文件 | 先写账本后写产物（顺序反了），中间被杀 | 顺序必须是：写产物（原子） → 校验产物 → 才写 `ok` 行。**账本是产物的从属，不能领先于产物** |
| 3 小时的批量任务，一次挂了以后重跑 3 小时 | 逐项状态没落盘，只有最后汇总 | 逐项 append + `fsync`；哪怕每项只记 4 个字段 |
| 定时长任务"看起来每天在跑"，但一周没产出 | 只看退出码/`last_status=ok`，没人验产物 | 独立缺勤检测器：窗口内应有的产物不存在即告警；对账三方比对 |

另外两个容易忽略的：

- **时间戳不带时区**：跨天任务、跨时区复盘时，"昨天的数据"会算错。统一写 `+08:00` 或 UTC。
- **产物 0 字节也算存在**：`os.path.exists()` 对 0 字节文件返回 True。判据必须带 `getsize() > 0`（必要时还要带"文件可解析"的校验，比如 JSON 能 `load`、视频能 `ffprobe`）。

---

## 十一、上线前检查清单（照着抄）

**清单与身份**

- [ ] 任务有一份不可变的清单，带内容哈希
- [ ] 清单哈希写进账本每一行，resume 前先比对
- [ ] 项 ID 稳定、可排序、不含路径分隔符

**账本**

- [ ] 账本 append-only（JSONL 或事件表），失败也记账
- [ ] 每行写入后 `flush` + `fsync`（或等价的持久化保证）
- [ ] 每行带时区时间戳、`attempt` 计数、`status` 三态、`artifact`、错误类别前缀
- [ ] 账本可回放成当前状态视图，且有 `--reconcile` 只用账本不执行的模式

**幂等**

- [ ] 所有写操作有幂等键（调用方生成，跨进程稳定）
- [ ] 产物先写临时文件、再原子改名
- [ ] 幂等判据是"产物存在且非空且可解析"，不是内存标记

**指纹**

- [ ] 影响产出语义的配置进指纹（模型/模板/关键参数/输出结构版本）
- [ ] 日志级别、并发数、超时秒数**不进**指纹
- [ ] 指纹变化导致的"重跑"在账本里显式标记原因

**并发与隔离**

- [ ] 每项有硬超时
- [ ] 有 per-provider 令牌桶限流，不是全局 `sleep`
- [ ] 连续失败熔断（阈值 5~10 项）
- [ ] 失败项与重试项延后到批末尾统一处理

**对账与缺勤**

- [ ] 有独立于任务的缺勤检测器（窗口 + 期望产物）
- [ ] 三方对账：清单 / 账本 / 磁盘产物
- [ ] 告警到达的是"产物缺失"，不是"任务退出码非 0"

---

## 结语

长任务的工程本质是一句话：**把不可恢复的过程，拆成可恢复的项，并为每一项留下可被外部核对的痕迹。**

模型能力决定单项做得好不好；清单、账本、幂等、指纹、对账这五样东西，决定的是"跑到一半崩了以后，你还剩多少"。前者靠换模型提升，后者只能靠设计。

先把账本写出来，其他都会跟着清晰。

---

*本文是「AI Agent 工程实战」系列之一。相关系列：*
- [AI Agent工具调用实战指南](agent-tool-calling-guide.html)
- [AI Agent自动化可靠性实战指南](agent-reliability-guide.html)
- [AI Agent定时任务实战指南](cron-agent-automation-guide.html)
- [AI Agent成本优化实战指南](agent-cost-optimization-guide.html)
