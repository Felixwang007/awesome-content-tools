# 翻到空页不等于读完了：533,692 条里拿到 1,000 条，服务端在你越过上限之后把「总量」也改成了 0

Agent 每读一次东西都在做同一个隐含断言：**「我看到的这些，就是它要给我的全部。」**

这个断言几乎从来不成立，而且失败时有三种完全不同的样子——只有一种会喊出声。

这一篇的每个数字都是今天在本机跑出来的，来源只有四处：一次读大文件、抓 GitHub 的 commits 接口、抓 HN Algolia 搜索、以及一个「服务端一声不响封顶」的构造样本。它们拼出来的是同一件事：

> **「没看到 truncated」不是全集证明。全集需要正面证据。**

工具在 [`tools/trunc/trunc_probe.py`](https://github.com/Felixwang007/awesome-content-tools/blob/main/tools/trunc/trunc_probe.py)（纯标准库，`selftest` 25/25，退出码就是结论）。

---

## 1. 四种「读了一半」，只有一种会喊出声

| 类型 | 现场读数 | 客户端会怎么想 | 该怎么判 |
|---|---|---|---|
| **好截断**（有信号） | 读 465,000 字节 / 5,000 行的文件，返回 **1,042 行**，同时给出 `truncated_by: bytes`、`total_lines: 5000`、`next_offset: 1043` | 知道自己没读完，也知道从哪继续 | `TRUNCATED` + 有续读点 |
| **坏截断**（200 + 半截正文） | GitHub `per_page=1000`：**5 次抓取，5 次半截**，`IncompleteRead(49311 bytes read, 16225 more expected)` | 最容易误判成「数据坏了 / 接口不稳」 | `TRUNCATED`，动作是重试或降低每页条数 |
| **静默封顶**（无信号） | 请求 `limit=5000`，实得 **1000**，且没有 `total` / `has_more` / `next` / `Link` 任何一个总量字段 | 认为「这就是全部 1000 条」 | `UNKNOWN`，**不许当绿** |
| **假完整**（翻到空页） | Algolia：`nbHits=533,692`，`hitsPerPage=5000` 实得 **1,000** 条、`nbPages=1`；翻到 `page=10` 时返回 **200 + hits=0 + nbHits=0 + nbPages=0** | 「空页 = 到底了 = 我拿全了 1000 条」 | `TRUNCATED`，而且续读点根本不存在 |

第三种和第四种是最贵的：它们不报错、不抛异常、不打印 "truncated"，退出码 0，日志干净，流水线一路绿灯跑到终点——**最后交付的结论建立在 0.19% 的数据上。**

---

## 2. 同一个端点：要不要得更多，决定了你读不读得到

`GET https://api.github.com/repos/torvalds/linux/commits`，同一个仓库，同一个 UA，只改 `per_page`：

| 请求 | 抓取次数 | 拿到完整 body | 半截 | 完整时的读数 |
|---|---|---|---|---|
| `per_page=1` | 1 | **1** | 0 | 1 条，5,359 字节，响应头仍带 `rel="next"` |
| `per_page=100` | 18 | **1**（5.6%） | 17 | 100 条，634,981 字节，响应头带 `rel="next"` 和 `page=1102337` 的 `rel="last"` |
| `per_page=1000` | 5 | **0** | 5 | —（一次都没拿到能解析的 body） |

（另一轮独立的 12 次抓取也只成功 1 次——两轮测量都落在「1 次」上，所以上面这行不是我挑好看的写。）

第一次失败的那条错误长这样：

```
http.client.IncompleteRead: IncompleteRead(49311 bytes read, 16225 more expected)
```

`49311 + 16225 = 65536`——**正好一个 64 KiB 分块**。这不是接口在返回错误数据，是传输层在一个 chunk 边界断掉，而 HTTP 状态码已经是 200。

这里有两个必须分开的动作：

1. **`200 + JSON 解析失败` 的正确解释是「半截」，不是「坏数据」。** 如果把它归类成数据问题，你会去查编码、查转义、查 schema，而真正该做的是重试或者把每页条数降到 100 以下。
2. **重试成功不等于读全。** 上表里 `per_page=100` 那唯一一次成功的响应，看起来「完整、干净、退出码 0」——但它同时带着 `rel="next"`，服务端在同一个响应里明说：还有第 2 页，一直到第 1,102,337 页。

一次干净的 200，可以是只读了第 1 页而误以为在读全集的完美样本。

---

## 3. 最贵的那种：翻到空页被当成「读完了」

HN Algolia 搜索 `query=agent`，同一个请求间隔几分钟各打一次：

```
hitsPerPage=100  -> nbHits=533688 hits=100  page=0 nbPages=10
hitsPerPage=5000 -> nbHits=533689 hits=1000 page=0 nbPages=1
```

先把两个坑拆开：

- **要 5000 给 1000**：请求 `hitsPerPage=5000`，实得 1,000 条。字段是给全的（`nbHits` 就在旁边），所以一个稍微认真点的客户端能发现「总数 533,689 ≠ 我手上 1,000」。
- **`nbPages=1` 更坏**：它告诉你「就一页」。只翻页不读总量的客户端会得出「第 1 页 = 最后一页 = 结束了」，然后心满意足地停在 1,000 条。

真正致命的是第三步。按 `hitsPerPage=100` 往后翻：

```
page=9  -> HTTP 200  hits=100  nbHits=533692  nbPages=10
page=10 -> HTTP 200  hits=0    nbHits=0       nbPages=0
page=11 -> HTTP 200  hits=0    nbHits=0       nbPages=0
```

`page=9` 是第 10 页、刚好第 1,000 条，一切正常。**`page=10` 越过了硬顶，服务端不但给你空页，还顺手把 `nbHits` 和 `nbPages` 一起改成了 0。**

于是行业里最常见的那段循环——

```python
while True:
    page = fetch(page_no)
    if not page["hits"]:
        break          # 空页 = 到末尾 = 我拿全了
    all_rows.extend(page["hits"])
```

——在这条路径上会安静地成功退出，`all_rows` 长度 1,000，`nbHits` 那一刻读到的也是 0，**没有任何一处数据和「已经取全集」矛盾**。实际差 532,692 条。

顺便记一个小事实：三次读到的总量分别是 **533,688 / 533,689 / 533,692**。总量字段本身在分钟级漂移（这是活的搜索索引）。所以「拿到的条数 == 声明总量」这种对账不能要求纯相等，得允许小幅漂移，否则你会把正常翻页判成不一致。

---

## 4. 为什么「没有截断标记」不能当全集证明

这是全篇唯一一条逻辑，也是所有假绿灯的来源：

> **「我读到的范围里没有 truncate 标记」是一句关于「我手上这份数据」的话；
> 「我拿到了全集」是一句关于「它没给我的那部分」的话。**

后者不可能由前者推出。截断标记是「有人在替你把关」时才存在的奢侈品：好工具会主动在输出里写 `truncated`、写 `next_offset`、写 `total_lines`；HTTP 会写 `Link: rel="next"`；有的 API 会写 `has_more`。这些东西是**别人给你的证据**，不是你自己测出来的事实。

所以判定不该是两态（成功 / 失败），而是三态：

| 判定 | 含义 | 需要什么 |
|---|---|---|
| `COMPLETE` | 有正面证据说明这确实是全部 | `total == items`，或 `has_more=false`，或显式结束标记，或「行数读完且字节数对上」 |
| `TRUNCATED` | 有正面证据说明这不是全部 | 显式截断措辞 / `total > items` / `rel="next"` / 200+半截正文 / 覆盖区间不足 |
| `UNKNOWN` | 两边证据都没有 | 默认结局。**不是「应该没问题」** |

第三条是重点：`UNKNOWN` 是一个**结论**，不是一个待定状态。它意味着「我不知道我有没有读全」，所以下游要么去补齐证据，要么在措辞和置信度上降级——不能拿它当绿。

---

## 5. 八条规则 + 一个默认结局

判定按三层证据走（流层 / 集合层 / 语义层），任何一层给出截断证据就判 `TRUNCATED`，直接压过其他层的乐观信号：

| 规则 | 层 | 证据 | 判定 |
|---|---|---|---|
| `TR001` | 语义 | 正文/备注命中截断措辞（`truncated`、`已截断`、`showing first`、`more expected`…），或给了 `next_offset` | TRUNCATED |
| `TR002` | 集合 | 声明 `total > items` | TRUNCATED |
| `TR003` | 集合 | `has_more: true` | TRUNCATED |
| `TR004` | 流 | 响应头 `Link: rel="next"` | TRUNCATED |
| `TR005` | 流 | `status=200` 但正文解析失败；或 `bytes_read < bytes_expected`；或 `lines_read < lines_total` | TRUNCATED |
| `TR006` | 集合 | 请求 `limit` 大于实得、实得命中常见上限（100/1000/5000…），且**完全没有**总量字段 | UNKNOWN |
| `TR007` | 语义 | `items=0` 且 `total>0` → TRUNCATED；`items=0` 且零证据 → UNKNOWN | TRUNCATED / UNKNOWN |
| `TR008` | 集合 | 请求 `offset` 与响应首条索引不一致 → UNKNOWN；已在翻页中途但区间没到 `total` → TRUNCATED | UNKNOWN / TRUNCATED |
| `TR009` | — | **没有任何完整性正面证据** | UNKNOWN |

`TR006` 值得单独说一句：它特意判 `UNKNOWN` 而不是 `TRUNCATED`。因为「1000」既可能是封顶，也可能真的只有 1000 条，我用一次请求分不出来。**把「我怀疑」写成「它就是」是把启发式当事实**，那个错误比漏报更难修，因为下游会按「已确认截断」去动作。

---

## 6. 六份快照，六条真实判定

工具吃一份快照 JSON（`kind` + `request` + `response`），吐判定 + 证据 + 续读点。退出码就是结论，可直接挂门禁：`0` COMPLETE / `2` TRUNCATED / `3` UNKNOWN / `4` 零输入 / `5` 自检失败。

```
$ python tools/trunc/trunc_probe.py judge samples/read_file_big_file.json
判定: TRUNCATED   (kind=file)
  [TR001] TRUNCATED: 正文/备注命中截断措辞: (showing/showing lines/truncat/truncated
  [TR001] TRUNCATED: 响应给出 next_offset=1043（服务端明说还有后续）
  [TR005] TRUNCATED: 读到 100000 字节 / 声明 465000 字节（差 365000）
  [TR005] TRUNCATED: 读到 1042 行 / 共 5000 行（差 3958）
续读: 从 next_offset=1043 续读
exit=2
```

```
$ python tools/trunc/trunc_probe.py judge samples/algolia_hits_cap.json
判定: TRUNCATED   (kind=list)
  [TR002] TRUNCATED: 声明 total=533689，实得 items=1000（缺 532689）
续读: 从 offset=1000 续读（假定该 API 用 offset/limit 语义且顺序稳定；用 page/per_page 的 API 需换算，且部分 API 有硬顶翻不过去）
exit=2
```

```
$ python tools/trunc/trunc_probe.py judge samples/github_per_page_100_complete_but_not_all.json
判定: TRUNCATED   (kind=http)
  [TR004] TRUNCATED: 响应头带 rel="next"（服务端自认还有下一页）
续读: 从 offset=100 续读（假定该 API 用 offset/limit 语义且顺序稳定；用 page/per_page 的 API 需换算，且部分 API 有硬顶翻不过去）
exit=2
```

```
$ python tools/trunc/trunc_probe.py judge samples/github_per_page_1000_short_body.json
判定: TRUNCATED   (kind=http)
  [TR001] TRUNCATED: 正文/备注命中截断措辞: more expected
  [TR004] TRUNCATED: 响应头带 rel="next"（服务端自认还有下一页）
  [TR005] TRUNCATED: status=200 但正文解析失败 —— 这是半截正文，不是坏数据
  [TR005] TRUNCATED: 读到 49311 字节 / 声明 65536 字节（差 16225）
续读: 无法给出续读点：先拿到总量字段再续读
exit=2
```

```
$ python tools/trunc/trunc_probe.py judge samples/constructed_silent_cap.json
判定: UNKNOWN   (kind=list)
  [TR006] UNKNOWN: 请求 limit=5000 实得 1000（命中常见上限 1000），且无 total/has_more/next/Link 任何总量字段 —— 无法区分「只有这些」和「封顶了」
  [TR009] UNKNOWN: 既无截断证据也无完整性证据 —— 「没看到 truncated」不能当全集证明
续读: 从 offset=1000 续读（假定该 API 用 offset/limit 语义且顺序稳定；用 page/per_page 的 API 需换算，且部分 API 有硬顶翻不过去）
exit=3
```

```
$ python tools/trunc/trunc_probe.py judge samples/constructed_complete.json
判定: COMPLETE   (kind=list)
完整性证据: total==items==12; has_more=false
exit=0
```

注意第三例：**一次 HTTP 200、JSON 完全合法、100 条整整齐齐**的响应，判定是 `TRUNCATED`。它跟「数据干净」不冲突，只是它同时带着 `rel="next"`。

六份快照里有四份是今天现场抓的（Algolia、GitHub 两个、本地大文件），两份是构造样本，文件名里直说了 `constructed_`——**样本来源必须写在文件名里**，否则三个月后没人分得清哪条是实测、哪条是想象。

---

## 7. 挂进 Agent 流水线：四条硬规则

1. **工具返回必须带「总量三件套」之一：`total` / `has_more` / `next_offset`。** 自己包的工具、自己写的 MCP 都一样。拿不到就别指望模型自己意识到它没读全——模型的输入里根本没有「没给它的那部分」。
2. **`UNKNOWN` 不许写成产出。** 读到 `UNKNOWN` 的处理只有两种：补证据（再请求一次总量，或换个能给出总量的端点），或者把结论降级为「基于前 N 条的部分观察」，并在交付物里写清 N 和已知总量。
3. **终止判据不能只看「空了」。** 空页必须与声明总量对账才叫结束；对账要允许总量漂移（今天三次读到 533,688 / 533,689 / 533,692），所以判据是「实得条数 ≈ 声明总量，差值在漂移范围内」，而不是严格相等，也不是「空页即完」。
4. **重试前先分类错误。** `200 + parse fail` = 半截（重试 / 降 page size，今天 `per_page=1000` 是 5/5 失败、`per_page=100` 是 1/18 成功（5.6%）、`per_page=1` 是 1/1 成功）；`4xx` = 请求错（重试无用）；`200 + parse ok + 没有总量字段` = 不知道（补证据，不是重试）。

第 4 条是今天最实用的一条：**同一个接口，把每页条数从 1000 降到 100，成功率从 0/5 变成 1/18（5.6%），再降到 1 就是 1/1。** 很多「接口不稳」的抱怨，其实是「一次要得太多」的换名。

---

## 8. 边界声明（这套东西不做什么）

- **快照是调用方提供的事实。** `judge` 不抓网络、不读文件、不算哈希，它只对你喂进去的那几个字段负责。字段填错，判定跟着错——这是设计，不是 bug。
- **不做语义判重。** 重复项不等于缺项，缺项也不等于重复，那是另一套工具的事。
- **`TR006` 是怀疑不是结论**，所以它判 `UNKNOWN`。想要它变成 `TRUNCATED`，得先拿到 `total`。
- **`total` 漂移时可能不判 `COMPLETE`。** 声明 533,689 到手 533,688 会被判 `TRUNCATED`（缺 1 条）。这是故意的：宁可多报一次，也不要把「差一条」当成「全了」。真要放宽，自己在上游做容许区间，别改规则。
- **续读点是建议不是保证。** 它假定该 API 用 `offset/limit` 语义且顺序稳定；Algolia 那条路径上 1,000 就是硬顶，续读点根本不存在——所以续读建议必须被执行方验证一次。
- **25 项自检覆盖规则语义，不覆盖网络行为。** 网络今天给出 50% 以上的半截率，明天可能为 0，这不是自检能保证的，只能实测。

---

## 一句话版

**「我读到了」和「我读完了」是两个命题，中间隔着一个必须有人主动提供的数字。** 没有那个数字，正确的答案不是「应该读完了」，而是「不知道」——然后在交付物里把这句话写出来。
