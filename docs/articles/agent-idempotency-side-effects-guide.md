# 同一段重试，503 时只扣一笔、超时后收两遍钱：Agent 副作用幂等性的 10 个实测场景

三个订单，六个请求，六笔扣款。日志里每一次调用都写着「成功」，账上却多收了一倍的钱。

这不是段子，是 2026-11-02 在本机（Windows 11 + Python 3.14.6）跑出来的读数：同一个重试器、同一个扣款服务，只把「失败发生在提交之前」换成「失败发生在提交之后」，扣款笔数就从 3 变成 6。

下面十条读数全部来自 `tools/idem/idem_probe.py`，真实 socket、真实 JSONL 账本、连跑两遍结果一模一样。

## 一、先看读数：10 个场景，同一台机器，同一个重试器

```
scenario                   policy     key         req  charg  uniq   dup  lost
----------------------------------------------------------------------------------
pre_fail_naive             naive      none          6      3     3     0     0  OK
post_drop_naive            naive      none          6      6     3     3     0  OK
post_drop_key_atomic       key_atomic stable        6      3     3     0     0  OK
post_drop_key_per_attempt  key_atomic per_attempt   6      6     3     3     0  OK
content_hash_two_orders    content_hash stable      2      1     1     0     1  OK
race_racy                  key_racy   stable       20     20    10    10     0  OK
race_atomic                key_atomic stable       20     10    10     0     0  OK
at_most_once               naive      stable        3      0     0     0     3  OK
ttl_shorter_than_backoff   key_ttl    stable        6      6     3     3     0  OK
restart_store_lost         key_atomic stable        2      2     1     1     0  OK
----------------------------------------------------------------------------------
wall=3.613s
```

列的含义，缺一列就会看错：

- `req` 客户端发出的请求数（含重试）
- `charg` 账本里落下的扣款条数（**这是钱**）
- `uniq` 被扣过钱的意图数
- `dup` 重复扣款数 = Σ max(0, 每意图条数 − 1)
- `lost` 一分钱没收到的意图数

只有 `dup=0 且 lost=0` 才是干净的。`dup=0、lost=3` 不是「没问题」，是「一单都没成」。

## 二、三种故障相位，客户端看到的都是同一种「失败」

同一个扣款服务，三种故障注入：

| 相位 | 服务端发生了什么 | 客户端读到 | 实际结果 |
|---|---|---|---|
| 提交前失败 | 返回 503，**没有落账** | `status=503` `kind=server-error` | 没扣钱，重试安全 |
| 提交后丢回包 | 已落账，随后 `shutdown(SHUT_RDWR)` | `status=None` `kind=transport` | **已扣钱，重试就是再扣一次** |
| 回包慢于超时 | 已落账，睡 0.6s 再回 | `status=None` `kind=transport-timeout` | **已扣钱，同上** |

后两种在客户端是同一个异常类别（连接已被对端关闭 / 读超时），日志里都写「调用失败」。客户端**没有能力**从响应本身分辨「请求从未到达」和「请求到达了、钱扣了、回包丢了」。

后两张表里那条 `post_drop_naive`：3 个意图 6 次请求 6 笔扣款，`dup=3`。第一笔和第二笔的收款凭据不一样，说明服务端老老实实收了两遍钱：

```
intent=ord-001 amount=1000 receipt=rcpt_60cb1f98 key=-
intent=ord-001 amount=1000 receipt=rcpt_677c864b key=-
```

而同一故障下换成意图键 + 服务端原子去重（`post_drop_key_atomic`），账本只有三行，每行一把键：

```
intent=ord-001 amount=1000 receipt=rcpt_0686bb27 key=bea4e30dd4
intent=ord-002 amount=1000 receipt=rcpt_a7c4733a key=6c4288e0af
intent=ord-003 amount=1000 receipt=rcpt_971637bf key=9a7372ac4c
```

客户端那两次重试的读数长这样：

```
attempt=1 status=None kind=transport replay=None
attempt=2 status=200 kind=ok        replay=True
ledger lines: 1
```

第二次是**重放**（`replay=true`），返回的是第一笔的凭据，账本没有新增行。这就是幂等：请求可以来很多次，钱只能扣一次。

## 三、幂等键的三条硬要求（缺一条就等于没键）

### 3.1 键必须由「意图」推导，不能由「本次尝试」推导

`post_drop_key_per_attempt`：客户端每次都带键，只是每次换了新键 —— 结果和完全不带键一模一样（`dup=3`）。

```python
# 错：每次尝试一个新键 = 有键等于没键
key = attempt_key(intent, attempt)
# 对：同一意图的任何一次重试都是同一把键
key = intent_key(intent)      # sha256(规范化(intent))[:16]
```

规范化也有讲究：键排序、字符串去首尾空白。`{"id":" x "}` 和 `{"id":"x"}` 必须是同一把键，否则同一次重试会因为一个空格被当成两笔。

### 3.2 去重窗口必须覆盖最长重试跨度

`ttl_shorter_than_backoff`：服务端确实做了去重，但窗口 0.1s，而客户端退避 0.35s —— 键过期了，第二次照样落账，`dup=3`。

**幂等窗口（TTL）< 最坏重试跨度（次数 × 退避 + 单次超时）时，幂等只在理论上存在。** 真实支付网关的窗口通常是 24 小时，不是为了让代码好写，是因为你的重试可能隔着几小时（手动补单、隔夜任务重跑）。

### 3.3 键存储必须与副作用同一次写入落地

`restart_store_lost`：第一次提交成功但丢回包 → 进程重启（内存 store 清空、账本还在）→ 客户端带同一把键重试 → 又扣一笔，`dup=1`。

这就是为什么「内存字典 + 定时清理」的去重实现在生产上会间歇性失效：**键存在进程里，键就随进程一起死**。键必须和扣款记录落在同一个事务/同一行记录里，重启后还能查到。

## 四、内容去重不是幂等：多收钱和少收钱都是事故

`content_hash_two_orders`：两笔**真实不同**的订单（`ord-001`、`ord-002`），金额都是 1000。服务端按「金额哈希」去重 —— 第二笔被判成重放，返回第一笔的凭据。

读数：`charg=1、uniq=1、dup=0、lost=1`。

看起来 `dup=0` 很漂亮，实际上**少收了 50% 的钱**，而且顾客那一侧的订单状态是「支付成功」。

> 去重的身份是「意图」，不是「内容」。金额、商品、手机号都会撞：同一个套餐两个人买、同一个用户连下两单、同一笔补款两次发起。用内容当键，等于随机吞单。

## 五、check-then-act 在并发下不是「概率问题」，是「必发问题」

`race_racy`：2 个线程、同一把键、10 轮，服务端写法是「先查 store，查不到就睡 50ms 再落账」。

```
req=20  charg=20  uniq=10  dup=10      # 10 轮，每轮都重复
```

**10/10 轮全部重复，一次没漏。** 换原子写法（查与写在同一个临界区里）：

```
req=20  charg=10  uniq=10  dup=0
```

常见误判是「并发重复是偶发，加个重试/告警就行」。实测的结论相反：只要「读」和「写」不在同一个临界区，那个窗口是**确定性存在**的，并发一来就必进。50ms 是我故意放大的窗口，真实代码里它就是一次网络往返、一次数据库查询的时间，同样必进。

## 六、at-most-once 也是错的

`at_most_once`：不重试，只发一次。服务端第一次返回 503（提交前失败）→ `charg=0、lost=3`。

三单生意全没了，而且客户端会认为「失败了，让用户重来」，用户重来时是**新的意图**，于是多一次下单、多一次风控判断。

重试不是可选项。真正要决定的只有一件事：**带着什么重试**。答案是「带着由意图推导的、服务端会认的键」。

## 七、怎么验收：对账三数，断言账本，不要断言 200

幂等性的验收不能看 HTTP 状态码，也不能看客户端自报的 `success`。要看账本：

```python
def reconcile(entries, expected_intents):
    per = Counter(e["intent"] for e in entries)
    return {
        "committed": len(entries),
        "distinct_intents": len(per),
        "duplicates": sum(max(0, c - 1) for c in per.values()),
        "lost": sum(1 for i in expected_intents if per.get(i, 0) == 0),
    }
```

三条断言，缺一条都能混过去：

1. `duplicates == 0` —— 抓多收
2. `lost == 0` —— 抓少收（内容去重那条就是靠这个抓住的）
3. `committed == len(expected_intents)` —— 抓「账本行数对但意图对不上」

Regression 测试里断言 `sink` 收到的**确切参数和条数**，不要断言 `approved == True` 或 `status_code == 200`。一个「成功」的状态和一个「成功了一次」的事实是两件事。

## 八、重试动作表

| 读到的现象 | 请求是否可能已生效 | 动作 |
|---|---|---|
| 连接被拒 / DNS 失败 / 无路由 | 否 | 可原样重试（仍建议带键） |
| 4xx（400/401/403/404/422） | 否 | **不重试**，改参数或改权限 |
| 408 / 425 / 429 | 否（多半） | 按 `Retry-After` 退避后重试，带同一键 |
| 5xx | **可能已生效** | 带同一键重试，服务端必须能重放 |
| 超时 / 连接被关 / 无响应 | **可能已生效** | 带同一键重试；**绝不换键、绝不换意图** |
| 200 + 半截 body / 解析失败 | **可能已生效** | 先降 page size 或改用查询接口核对，再决定 |

## 九、12 项上线检查清单

1. 这套调用的副作用是什么（扣款/发券/发短信/建资源），写下来
2. 副作用接口是否接受幂等键；不接受就先改服务端，客户端加重试只会放大事故
3. 键由意图推导，一次意图只生成一次（持久化在前，调用在后）
4. 键的规范化规则写死（排序、去空白、大小写、全角半角）
5. 键与副作用同事务落盘，重启后仍可查
6. 去重窗口 ≥ 最坏重试跨度（含人工补单、隔夜重跑）
7. 服务端重放要返回**原凭据**，而不是「已处理」三个字
8. 并发路径上用唯一约束/行锁/条件写，不要用 check-then-act
9. 4xx 不进重试；408/425/429/5xx 按分类重试
10. 重试有总预算（次数 + 总时长），并有上限退出
11. 每次重试都把「第几次」写进日志（键相同、次数不同）
12. 对账作业每天跑一次 `duplicates / lost`，非零就报警

## 十、工具与复现

```bash
python3 tools/idem/idem_probe.py selftest    # 31/31，纯逻辑，不开端口
python3 tools/idem/idem_probe.py probe       # 10 个场景，真实 socket + 真实账本
python3 tools/idem/idem_probe.py classify --input readings.json
```

退出码：`0` 全部场景与声明一致 / `2` 有场景与声明不符（号称幂等却重复扣款）/ `3` 前置条件不满足 / `4` 零输入 / `5` 自检失败。

`probe` 会在每个场景里重建服务端和账本，把每次尝试的 `status/kind/retry/replay` 和账本行数一起打出来，最后把 JSON 明细也贴出来 —— 复制这段 JSON 就能做回归对比。

## 十一、边界声明

- 数字是本机 2026-11-02 一次实测（连跑两遍结果相同），是读数不是常数；Windows 上的连接关闭行为与 Linux 可能不同，换平台要重测
- 服务端是 `ThreadingHTTPServer` + 内存 store + JSONL 账本，用于复现语义，**不是**真实支付网关；真实网关的幂等窗口、重放格式要看它的文档
- 故障注入是确定性的（固定丢第一次、固定先失败一次），没有覆盖随机丢包、网络分区、半开连接
- 只测了单进程共享 store，未测多副本/多区域下的分布式去重（那是另一篇文章：唯一键 + 全局约束）
- TTL 用 0.1s 是为了在秒级复现，不代表生产该用 0.1s
- 本文不替代 T+1 对账系统：幂等是「尽量不重复」，对账是「事后一定能发现」
- 只对有副作用且可重试的调用成立；纯读接口不需要键（但需要防缓存穿透）

## 相关

- [把「超时」拆成五段](https://felixwang007.github.io/awesome-content-tools/articles/timeout-phase-splitting-guide.html)：先分清错误落在哪一段，再决定这一条要不要重试
- [并行工具调用安全分批](https://felixwang007.github.io/awesome-content-tools/articles/parallel-tool-call-safety-guide.html)：并发下的丢失更新与「批边界是唯一恢复点」
- [工具调用磁带 record-replay](https://felixwang007.github.io/awesome-content-tools/articles/agent-record-replay-tape-guide.html)：把副作用短路掉，让幂等逻辑可以离线复跑
- [AI Agent 规则绑定审计](https://felixwang007.github.io/awesome-content-tools/articles/agent-rule-binding-audit-guide.html)：为什么「自报成功」不能当验收证据
