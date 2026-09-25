# AI Agent模型路由实战指南：健康探测、错误分类与降级链

> 你写的 Agent 里大概有一行这样的配置：`model = "xxx"`。它今天能用，不代表下周还能用——模型会下线、会改名、会被地区限制、会因为你的 `max_tokens` 太小而返回一个"HTTP 200 但内容为空"的完美假成功。
> 这篇文章讲的是**多模型路由**：怎么知道一个端点"活着但能不能推理"、哪些错误该换模型、哪些错误换了也没用、以及怎么按真实价格和真实延迟选路。文中所有响应体、耗时、token 数、价格都是 2026-09-25 在本机实测抓下来的。

---

## 一、先说结论

路由的目的一般被误解成"用更强的模型"。**不是。** 路由解决的是三个跟模型强弱无关的问题：

1. **可用性**：上游端点会死、会被限流、会下线。单模型配置 = 单点故障。
2. **适配性**：同一个任务在不同时刻需要不同的上下文长度、不同的成本档位。
3. **可诊断性**：当请求失败时，你要能立刻分出"是配置错了"还是"该换下一个了"。

第三条最容易被忽略，也最贵。**把配置错误当成网络抖动去重试，等于把一个 5 分钟的修复变成一个 40 分钟的故障**——因为你的降级链会把所有模型都试一遍，全部失败，然后日志里只有一句"所有模型都不可用"，真正的原因（模型 ID 打错了）被埋在中间某行。

所以整篇文章的组织方式是：**先做错误分类，再做降级，最后才是选路。**

---

## 二、三个真实故障（都是实测）

### 故障 1：模型 ID 会突然变成 404，而且带墓碑文案

我用一个测试期已结束的模型 ID 发请求，拿到的原文是：

```
HTTP 404
{"error":{"message":"Thank you for participating in the Stealth Ox Alpha testing period.
This model was ZAI's GLM-5.3 Flash. Use it now:
https://openrouter.ai/z-ai/glm-5.3-flash","code":404},
 "user_id":"user_3IHYSY75rfdcrf1QtNybuGd4rAY"}
```

这类"墓碑响应"非常友好，但它只在聚合平台才可能存在。**直连厂商 API 时，模型下线通常就是一个光秃秃的 404 或 400**，你只能靠告警发现。

顺手抓了同一时刻的模型列表：`GET https://openrouter.ai/api/v1/models` 返回 **460 个模型**（755,701 字节 JSON）。里面既有 `stealth/space-bunny-alpha` 这种测试期代号模型（免费、1M 上下文、支持工具调用），也有大量你没有听说过的厂商 ID。

**结论**：模型 ID 是一个会腐烂的配置项。任何一个把模型名硬编码在业务代码里的系统，都需要一条"模型已下线"的告警路径，而不是等用户报错。

### 故障 2：HTTP 200，但内容是空的（最贵的假成功）

这是本次实测里最有价值的一条。我用 `max_tokens=16` 发了两条最小请求，两个模型都返回 **HTTP 200**：

| 模型 | HTTP | finish_reason | completion_tokens | reasoning_tokens | content |
|------|------|---------------|-------------------|------------------|---------|
| z-ai/glm-5.3-flash | 200 | length | 16 | **16** | `''`（空） |
| openai/gpt-oss-20b | 200 | length | 16 | **13** | `''`（空） |

看清楚：这次请求**全部 16 个 completion token 都被 reasoning（思考）吃掉了**，一个字正文都没留下。HTTP 层没有任何异常，`usage` 也正常返回、正常计费（glm-5.3-flash 这次花了 `$1.085e-05`）。

如果你的 Agent 流程只检查 HTTP 状态码，它会认为这一步成功了，然后拿着空字符串继续往下走——最后在生产环境的某个角落爆出一个莫名其妙的下游错误。

**复现与修复验证**（同一批模型，只改 `max_tokens`）：

| 模型 | max_tokens=16 | max_tokens=64 |
|------|---------------|---------------|
| z-ai/glm-5.3-flash | content=`''`，finish=length，completion=16，reasoning=16，$1.085e-05 | content=`'ok'`，finish=stop，completion=46，reasoning=48，$2.585e-05 |
| openai/gpt-oss-20b | content=`''`，finish=length，completion=16，reasoning=13，$3.98e-06 | content=`'ok'`，finish=stop，completion=32，reasoning=21，$6.38e-06 |

所以判据很明确：

> **`finish_reason == "length"` 且 `content` 为空 = 失败，不是成功。**
> 尤其当 `usage.completion_tokens_details.reasoning_tokens` 接近 `max_tokens` 时，这不是模型的问题，是你的预算给少了。

### 故障 3：端点活着，但不能推理

本地 llama.cpp 服务没启动时，`GET http://127.0.0.1:8080/v1/models` 直接连接失败（`URLError`，2004ms 后返回）。这还好，至少你知道它死了。

真正危险的是**反面**：服务起来了、`/v1/models` 毫秒级秒回、但 `/chat/completions` 超时。这在本地推理里非常常见——模型权重（比如 18GB 的 31B Q4）放进 20GB 显存，KV cache 溢出到系统内存，元数据接口照样秒回，推理请求直接卡死或 OOM。

**这就是为什么健康检查必须分两层：**

| 探测 | 请求 | 回答的问题 | 耗时级别 |
|------|------|-----------|---------|
| liveness | `GET {base}/models` | 进程活着吗？网络通吗？ | 毫秒 |
| readiness | `POST {base}/chat/completions`（1~16 token） | **现在能不能真的推理？** | 秒级 |

只做 liveness 的健康检查，等于用体温计判断人能不能跑马拉松。

---

## 三、错误分类：决定能不能靠"换模型"解决

这是整个路由逻辑的核心。下面每一行都是我实测抓到的真实响应体：

| HTTP / 异常 | 分类 | 真实响应体（节选） | 能不能换下一个模型解决 |
|-------------|------|-------------------|----------------------|
| 400 | **config** | `{"error":{"message":"no-such-model-xyz-9999 is not a valid model ID","code":400}}` | ❌ 不能。ID 打错了，换谁都错 |
| 401 | config | 密钥无效/未授权 | ❌ 不能 |
| 402 | config | 余额不足 | ❌ 不能。换个模型可能更贵，但根源是钱 |
| 403 | config | `{"error":{"message":"This model is not available in your region.","code":403,"metadata":{"routing_funnel":[{"step":"Initial Endpoints","endpoint_count":1}],"failed_routing_step":"Gate Endpoints with Geo Restrictions"}}` | ❌ 不能。地区限制是账号/网络属性 |
| 404 | **gone** | 见故障 1 的墓碑文案 | ✅ 能，且**必须告警**（配置在腐烂） |
| 408 / 409 / 425 | transient | 请求超时 / 冲突 / 过早 | ✅ 能 |
| 429 | transient | 限流，看 `Retry-After` | ✅ 能（退避后） |
| 5xx | transient | 上游错误 | ✅ 能 |
| 连接失败 / 读超时 | transient | `URLError` / `TimeoutError` | ✅ 能 |
| **200 + content 为空** | **empty** | 见故障 2 | ✅ 能（但先检查 `max_tokens`） |

规则的形状就一句话：

> **config 类错误立即终止降级链并告警；gone 类降级并告警；transient / empty 类降级。**

为什么 config 类必须终止？因为它的存在说明"配置与现实的契约已经破了"。如果这种错误也走降级，你的系统会用第二个、第三个模型接着失败，最终把"配置错误"这个唯一真因淹没在一堆下游噪音里——**降级链会把显式故障变成隐式故障。**

注意 403 的一个细节：那个响应体里的 `metadata.routing_funnel` / `failed_routing_step` 字段是聚合平台给的**归因信息**（这里明确说是"Gate Endpoints with Geo Restrictions"这一步挂的）。这类字段值得直接写进你的日志，因为它是少见的"上游主动告诉你为什么"。

---

## 四、可跑的脚本：route_probe.py

下面这个脚本我实际跑过，就是它产生了上文那张 trace 表。设计上有三个刻意的地方：

- **双探测分离**：liveness 失败不浪费一次推理超时；
- **错误分类先于降级**：config 类直接终止；
- **winner 必须满足"内容非空"**：不看状态码看正文。

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""route_probe.py — 多模型路由的"体检 + 降级链"最小可运行实现。"""
from __future__ import annotations

import json, os, time, urllib.error, urllib.request
from dataclasses import dataclass, field

CALLER = "route-probe/1.0"


@dataclass
class Route:
    name: str
    base_url: str
    model: str
    key_env: str | None = None
    ctx: int = 0
    price_in: float = 0.0      # 美元 / 百万 input token
    price_out: float = 0.0
    max_tokens: int = 16


@dataclass
class Attempt:
    route: str
    status: int | None = None
    cls: str = "ok"            # ok/config/gone/transient/empty
    ms: int = 0
    detail: str = ""
    content: str = ""
    usage: dict = field(default_factory=dict)


def _key(route: Route) -> str:
    if not route.key_env:
        return "none"
    val = os.environ.get(route.key_env, "").strip()
    if val:
        return val
    # 允许从 .env 读取，避免把密钥写进代码
    for path in (os.path.expanduser("~/AppData/Local/hermes/.env"),
                 os.path.expanduser("~/.env")):
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8", errors="replace"):
                if line.startswith(route.key_env + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    return ""


def _post_json(url: str, payload: dict, key: str, timeout: float):
    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", CALLER)
    data = json.dumps(payload).encode("utf-8")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def _get(url: str, key: str, timeout: float):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", CALLER)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def classify_http(status: int, body: str) -> tuple[str, str]:
    """把 HTTP 状态码翻译成「能不能靠换模型解决」。"""
    try:
        msg = json.loads(body).get("error", {}).get("message") or body[:160]
    except Exception:
        msg = body[:160]
    if status in (400, 401, 403, 422):
        return "config", msg
    if status == 404:
        return "gone", msg
    if status in (408, 409, 425, 429) or status >= 500:
        return "transient", msg
    return "config" if status >= 400 else "ok", msg


def probe_liveness(route: Route, timeout: float = 5.0) -> tuple[bool, int, str]:
    """只读元数据：判断「进程活没活」。秒回不代表能推理（VRAM 不足的典型表现）。"""
    url = route.base_url.rstrip("/") + "/models"
    t0 = time.time()
    try:
        status, _ = _get(url, _key(route), timeout)
        return status == 200, int((time.time() - t0) * 1000), f"HTTP {status}"
    except urllib.error.HTTPError as e:
        return False, int((time.time() - t0) * 1000), f"HTTP {e.code}"
    except Exception as e:
        return False, int((time.time() - t0) * 1000), type(e).__name__


def probe_readiness(route: Route, timeout: float = 60.0) -> Attempt:
    """真跑一次最小推理。这里才是「能不能用」的判据。"""
    url = route.base_url.rstrip("/") + "/chat/completions"
    payload = {"model": route.model,
               "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
               "max_tokens": route.max_tokens,
               "temperature": 0}
    t0 = time.time()
    try:
        status, body = _post_json(url, payload, _key(route), timeout)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        cls, detail = classify_http(e.code, raw)
        return Attempt(route.name, e.code, cls, int((time.time() - t0) * 1000), detail)
    except Exception as e:
        return Attempt(route.name, None, "transient",
                       int((time.time() - t0) * 1000), type(e).__name__)

    ms = int((time.time() - t0) * 1000)
    if status != 200:
        cls, detail = classify_http(status, json.dumps(body))
        return Attempt(route.name, status, cls, ms, detail)

    choice = (body.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    finish = choice.get("finish_reason")
    usage = body.get("usage") or {}
    if not content:
        # HTTP 200 但没有正文：最常见的原因是 token 预算被 reasoning 吃光，
        # 或上游 safety 过滤返回空。这不是成功。
        return Attempt(route.name, status, "empty", ms,
                       f"finish_reason={finish} usage={usage}", content, usage)
    return Attempt(route.name, status, "ok", ms, f"finish_reason={finish}", content, usage)


def run_chain(chain: list[Route], ledger_path: str | None = None,
              per_timeout: float = 60.0) -> tuple[Attempt | None, list[Attempt]]:
    """按顺序体检 + 推理，第一个真正返回正文的模型胜出。"""
    trace: list[Attempt] = []
    for route in chain:
        live, lms, ldetail = probe_liveness(route)
        if not live:
            trace.append(Attempt(route.name, None, "transient", lms,
                                 f"liveness failed: {ldetail}"))
            continue
        a = probe_readiness(route, timeout=per_timeout)
        trace.append(a)
        if a.cls == "ok":
            _write_ledger(ledger_path, route, a, trace)
            return a, trace
        if a.cls == "config":
            # 配置错误换模型解决不了，继续试只会把真因埋掉
            _write_ledger(ledger_path, route, a, trace)
            return None, trace
    return None, trace


def _write_ledger(path: str | None, route: Route, winner: Attempt, trace: list[Attempt]):
    if not path:
        return
    row = {"ts": int(time.time()), "winner": route.name, "model": route.model,
           "attempts": [{"route": a.route, "cls": a.cls, "status": a.status,
                         "ms": a.ms, "detail": a.detail} for a in trace]}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    OR = "https://openrouter.ai/api/v1"
    chain = [
        Route("local-llama", "http://127.0.0.1:8080/v1", "Muse-Glimmer-30B", None, 131072),
        Route("removed-id", OR, "stealth/ox-alpha", "OPENROUTER_API_KEY", 1000000, 0.075, 0.3),
        Route("glm-5.3-flash", OR, "z-ai/glm-5.3-flash", "OPENROUTER_API_KEY", 1300000, 0.075, 0.3),
        Route("gpt-oss-20b", OR, "openai/gpt-oss-20b", "OPENROUTER_API_KEY", 131072, 0.018, 0.09),
        Route("mistral-nemo", OR, "mistralai/mistral-nemo", "OPENROUTER_API_KEY", 131072, 0.019, 0.03),
    ]
    winner, trace = run_chain(chain, ledger_path="route_ledger.jsonl")
    print(f"{'route':<16}{'status':>7}{'cls':>11}{'ms':>8}  detail")
    for a in trace:
        print(f"{a.route:<16}{str(a.status):>7}{a.cls:>11}{a.ms:>8}  {a.detail[:70]}")
    print("\nwinner:", winner.route if winner else "NONE", "|",
          (winner.content if winner else ""))
```

### 实测输出（原样，未修改）

```
route            status        cls      ms  detail
local-llama        None  transient    2004  liveness failed: URLError
removed-id          404       gone     789  Thank you for participating in the Stealth Ox Alpha testing period...
glm-5.3-flash       200      empty    2429  finish_reason=length usage={'prompt_tokens': 19, ...
gpt-oss-20b         200      empty    1835  finish_reason=length usage={'prompt_tokens': 74, ...
mistral-nemo        200         ok    1175  finish_reason=stop

winner: mistral-nemo | ok
```

一次运行同时命中四类失败：`transient`（端点死）、`gone`（模型下线）、`empty`（假成功）×2，最后靠链尾最便宜的模型（$0.019/M in、$0.030/M out）成功返回。

### 落盘的账本长这样

```json
{"ts": 1790316165, "winner": "mistral-nemo", "model": "mistralai/mistral-nemo",
 "attempts": [
   {"route": "local-llama", "cls": "transient", "status": null, "ms": 2004,
    "detail": "liveness failed: URLError"},
   {"route": "removed-id", "cls": "gone", "status": 404, "ms": 789,
    "detail": "Thank you for participating in the Stealth Ox Alpha testing period..."},
   {"route": "glm-5.3-flash", "cls": "empty", "status": 200, "ms": 2429,
    "detail": "finish_reason=length usage={... 'reasoning_tokens': 16 ...}"},
   {"route": "gpt-oss-20b", "cls": "empty", "status": 200, "ms": 1835,
    "detail": "finish_reason=length usage={... 'reasoning_tokens': 13 ...}"},
   {"route": "mistral-nemo", "cls": "ok", "status": 200, "ms": 1175,
    "detail": "finish_reason=stop"}]}
```

**账本里要记的不是"成功了"，而是"谁在第几位、因为什么被淘汰"。** 否则你永远不知道降级链是不是每天都在悄悄救火。

---

## 五、选路策略：先约束，后价格，最后延迟

### 第一步：约束过滤（不满足的直接划掉）

| 约束 | 判据 | 为什么必须有 |
|------|------|-------------|
| 上下文长度 | 模型 ctx ≥ 实测最长输入 × 1.2 | 贴边必然在某一批长输入上翻车 |
| 工具调用能力 | `supported_parameters` 含 `tools` | 不带工具能力的模型无法进入 Agent 循环 |
| 地区可用性 | 见上文 403 类的 `failed_routing_step` | 账号/网络属性，跟模型质量无关 |
| 数据合规 | 是否允许把数据发给该厂商 | 这是法律约束，不是性能约束 |
| 延迟预算 | 交互场景 vs 批处理场景 | 交互场景延迟 >10s 就等于不可用 |

我在实测那一刻抓的模型列表里：**460 个模型，其中 392 个支持工具调用，416 个上下文 ≥128K，175 个 ≥1M**。约束过滤完通常还能剩几百个候选——**所以"候选不足"几乎从来不是问题，"选错判据"才是。**

### 第二步：价格（真实分布，不看宣传页）

同一时刻从 `GET /api/v1/models` 算出来的输入价格分布（美元 / 百万 token，已剔除 5 个用负值表示"动态路由"的伪模型）：

| 分位 | 输入价格 |
|------|---------|
| min | $0（24 个免费模型） |
| p25 | $0.117 |
| **中位** | **$0.42** |
| p75 | $1.25 |
| p90 | $3.00 |
| max | $150.00 |

**最贵与中位相差 357 倍。** 这个数字的意义是：如果你按"榜单第一名"选模型，你很可能为一个只需要 20 分类的任务付出 100 倍成本。反过来，满足"≥128K 上下文 + 支持工具调用"的最便宜付费档在实测中是：

| 模型 | 输入 | 输出 | 上下文 |
|------|------|------|--------|
| ibm-granite/granite-4.0-h-micro | $0.017/M | $0.112/M | 131,000 |
| openai/gpt-oss-20b | $0.018/M | $0.090/M | 131,072 |
| mistralai/mistral-nemo | $0.019/M | $0.030/M | 131,072 |

**做法：把价格当成过滤器，而不是决策器。** 先用价格切出"预算内可接受集"，再在集合里用你自己的样本选（见第六节）。价格表会变，而且变得比你想的快——所以价格要定期重抓，不能写死在代码里。

### 第三步：延迟用实测的，不用榜单的

同一次实测里三个小模型的耗时：glm-5.3-flash 1252ms、gpt-oss-20b 2706ms、mistral-nemo 1175ms（同样是"回一个词"的最小请求）。**同一时刻、同一个平台、同样的请求，延迟差 2.3 倍。** 榜单上的综合分跟你的具体请求形态相关性很弱。

写法：每个路由维护一个 EWMA 延迟（`new = 0.7 * old + 0.3 * observed`），超过预算就临时降权，而不是永久移除。**延迟是环境变量（上游排队、地域、时段），不是模型属性。**

---

## 六、粘性路由与"缓存税"

这是路由里唯一一个会让你"降级反而更贵"的坑。

实测证据：`openai/gpt-oss-20b` 那次调用返回的 usage 里 `prompt_tokens=74`，其中 **`cached_tokens=64`**。也就是说 86% 的输入命中了前缀缓存，按缓存价计费（`cost_details.upstream_inference_prompt_cost` 只有 $1.58e-06）。

而**换一个模型 = 换一套前缀缓存 = 缓存命中率归零**。所以：

| 场景 | 路由策略 |
|------|---------|
| 同一次多轮对话 / 同一个 Agent 任务 | **粘性（sticky）**：一次选定，全程不换。中途降级只应发生在真正的 transient/gone 失败之后，并且**从头重放上下文** |
| 不同任务之间 | 可以自由换。任务边界是唯一的免费换模型时机 |
| 周期性批量任务 | 按批选路，批内固定 |
| 低成本小任务（分类/打分/抽取） | 允许每次单独选，缓存收益本来就小 |

一条推论：**"为了省钱"而把一次长对话里的模型切来切去，通常会让总成本上升。** 换模型省下的是单价，丢掉的是缓存命中率——两者经常后者更大。

---

## 七、用 usage.cost 记账，而不是用估算

现代聚合平台会在每个响应里回传真实成本。实测：

| 调用 | completion_tokens | 真实 cost |
|------|-------------------|-----------|
| glm-5.3-flash（max_tokens=16，空返回） | 16 | $1.085e-05 |
| glm-5.3-flash（max_tokens=64，成功） | 46 | $2.585e-05 |
| gpt-oss-20b（max_tokens=16，空返回） | 16 | $3.98e-06 |
| gpt-oss-20b（max_tokens=64，成功） | 32 | $6.38e-06 |

两个记账纪律：

1. **失败的尝试也要记账。** 故障 2 里那次"HTTP 200 但内容为空"的调用是**收费的**。降级链上每一次尝试都在花钱——所以降级链的长度是有成本的，不是免费的保险。
2. **成本要摊到"成功产出"上，不是摊到"请求数"上。** 指标应该是 `总花费 / 有效产出数`（有效产出 = 通过你下游校验的产物）。如果降级链让请求数翻倍但产出没变，你的单位产出成本是上升的。

再进一步：把成本按路由分桶（哪个模型贡献了多少花费、多少有效产出），这是唯一能回答"这条链里最贵的一环是谁"的读数。

---

## 八、七个反模式

| 反模式 | 为什么会出事 | 改成 |
|--------|-------------|------|
| 把 config 类错误也走降级 | 所有模型都试一遍全失败，真因（ID 打错/没余额）被埋 | config 类立即终止并告警 |
| 同一错误无限重试同一模型 | 重试的是自己的固执，不是上游 | 同因失败两次就停：要么分类错误，要么告警 |
| 只用 `GET /models` 做健康检查 | 服务活着但推理不可用（VRAM 溢出）照样放行 | liveness + readiness 双探测 |
| 只检查 HTTP 状态码 | HTTP 200 且 content 为空会被当成功 | 判据加"正文非空 + finish_reason 合理" |
| 静默降级不告警 | 链条每天救火，你却以为一切正常 | `gone` 类降级必须告警（配置腐烂信号） |
| 按榜单/口碑硬编码模型 | 价差 357 倍，且榜单跟你任务无关 | 约束 → 价格 → 自测样本 |
| 长对话中途为了省钱切模型 | 缓存命中率归零，总成本反而上升 | 任务内粘性，任务边界才换 |

---

## 九、上线前 12 项检查清单

**探测层**

- [ ] liveness（`GET /models`）与 readiness（1 token 推理）分开实现
- [ ] readiness 有独立超时（本地模型建议 ≥30s，云端 60s）
- [ ] 探测结果落盘，能看到"什么时候开始不健康的"

**分类层**

- [ ] 400/401/402/403 → config，终止降级 + 告警
- [ ] 404 → gone，降级 + 告警（模型 ID 腐烂）
- [ ] 429/5xx/超时/连接失败 → transient，退避后降级
- [ ] HTTP 200 但 content 为空 → empty，按失败处理
- [ ] `finish_reason == "length"` 时同时检查 `reasoning_tokens` 占比

**选路层**

- [ ] 候选先过约束（ctx 余量 ×1.2 / 工具能力 / 地区 / 合规）
- [ ] 价格定期重抓，不硬编码；记录当次选路理由
- [ ] 延迟用 EWMA 实测值，超预算只降权不移除

**记账层**

- [ ] 每次尝试（含失败）都记 usage.cost，按路由分桶
- [ ] 指标是"总花费 / 有效产出数"，不是"请求成功率"
- [ ] 降级链有长度上限（每次降级都是一次真实花费）

---

## 结语

模型路由的工程本质一句话：**把"哪个模型"从代码里的常量，变成一个在运行时被证据驱动的决策。**

而这套东西的价值全部集中在分类上——**因为只有"该换"和"不该换"分得清，降级才是修复而不是掩盖。** HTTP 200 却返回空内容的那次实测最能说明问题：状态码说了算的系统认为那是成功，判据说了算的系统知道那是失败。

先把错误分类表和探测逻辑写出来，剩下的（价格、延迟、粘性）都是它的自然推论。

---

*本文是「AI Agent 工程实战」系列之一。相关系列：*
- [AI Agent成本优化实战指南](agent-cost-optimization-guide.html)
- [AI Agent长任务实战指南：断点续跑、幂等与进度对账](agent-long-task-resume-guide.html)
- [AI Agent自动化可靠性实战指南](agent-reliability-guide.html)
- [AI Agent评测与可观测性实战指南](agent-evaluation-observability-guide.html)
- [AI Agent工具调用实战指南](agent-tool-calling-guide.html)
