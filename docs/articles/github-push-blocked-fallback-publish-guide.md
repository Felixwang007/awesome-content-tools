# `git push` 卡死 21 秒，`api.github.com` 却 548 毫秒返回：把产物送上线的三条通道与一次 226 个 blob 的对账

自动化做得再好，最后一步永远是「产物怎么上线」。这一步最容易烂掉，因为烂掉的时候**不报错**——cron 里 `git push` 挂掉，脚本 exit 1，日志写「推送失败」，然后没有人再看第二眼。这一篇记录的是今天在同一台机器、同一天、相隔约 40 分钟的两次实测：**一次推不上去，一次推上去了**，以及我从中间学到的三件事——通道判活的三态、repo 树的对账方式、和一次 33 个文件混进提交的翻车。

全部数字为本机实跑原样输出，工具在 [`tools/publish/gh_tree_push.py`](https://github.com/Felixwang007/awesome-content-tools/blob/main/tools/publish/gh_tree_push.py)（纯标准库，`selftest` 20/20，退出码三态）。

---

## 1. 现场：三个主机名，三种命运

先看阻断发生时的读数。同一个终端、同一条链路，连续 5 个 curl：

| 目标 | HTTP | connect | total |
|---|---|---|---|
| `github.com/Felixwang007/awesome-content-tools`（网页） | **000** | 0.000000s | **21.005s** |
| `github.com/...git/info/refs?service=git-upload-pack`（git 走这里） | **000** | 0.000000s | **21.021s** |
| `api.github.com/repos/...` | 200 | 0.096583s | **0.548s** |
| `codeload.github.com/.../tar.gz/refs/heads/main` | 200 | 0.096102s | 0.515s |
| `felixwang007.github.io/awesome-content-tools/`（Pages CDN） | 200 | 0.286382s | 1.238s |

两个细节决定了后面所有判断：

**第一，`connect=0.000000` 说明 TCP 根本没建立。** 这不是「服务器慢」，也不是「证书问题」，更不是「GitHub 挂了」——握手包发出去没有 SYN-ACK 回来，客户端等满 21 秒（SYN 重传两轮）放弃，curl 最后给的状态码是 `000`。同一条链路里 `api.github.com` 的 `connect=0.0966s` 说明**域名家族不等于同一张网**：`github.com:443` 被丢包，`api.github.com:443` 一路通畅。此时 `git push` 无论重试多少次都不会成功，因为它只会走第一个地址。

**第二，`/git/.../info/refs` 这个探测地址，匿名访问的正确答案是 401，不是 200。** 40 分钟后网络恢复，我重新跑同一条命令：

```
web 页面                            200    0.9s  ok
smart-http（git push 走这里）        401    0.5s  auth-required
api.github.com                      200    0.5s  ok
codeload 源码包                      200    0.5s  ok
Pages CDN                          200    1.2s  ok
```

`401 auth-required` 的意思是：**端点可达，只是要认证**。把 401 当成失败，就会得出「网络还没恢复」的相反结论。所以判活函数只有一条规则：

> `status == 0` → 连不上（丢包/阻断，换通道）；
> `2xx/3xx/401/403` → 通道可达（`git push` 可用；缺的只是凭证）。

我把这条写成了 `probe` 子命令，五条通道一次跑完，结论直接给「走 git push / 换 API / 先修网络」三选一。恢复后 `git ls-remote` 实测 1047ms，与 401 探测的 0.5s 同量级，三者互相印证。

---

## 2. 三条通道到底差在哪

GitHub 上「把内容写进仓库」有三条路，能力和代价完全不同：

| 通道 | 一次推送的调用数 | 原子性 | 触发 Pages 构建 | 什么时候用 |
|---|---|---|---|---|
| `git push`（smart-http） | 1 次连接、多轮 pack | 整次推送要么全成要么全不成 | 1 次 | 网络正常时的默认选择 |
| **Git Data API**（blobs → tree → commit → ref） | **2 + N**（N=改动文件数） | 建树 + 建提交 + 移 ref，ref 更新是唯一的「生效点」 | **1 次** | smart-http 被阻断、或需要逐 blob 对账时 |
| Contents API（`PUT /contents/{path}`） | **N**（一次一个文件，一次一个提交） | 无：文件之间可能只推上去一半 | **N 次** | 只改一两个文件、且不在乎构建次数时 |

第三列是很多人忽略的成本。本仓库的构建记录（`GET /repos/{r}/pages/builds`）实测：

| commit | 构建状态 | 耗时 |
|---|---|---|
| d3fcd4d | built | 17.5s |
| ab48505 | built | 16.8s |
| 7f4c679 | built | 21.7s |
| 1ec38d0 | built | 56.7s |
| 31a84f6 | built | 18.8s |
| 2c31d55 | built | 26.1s |

最近 6 次提交对应 6 次构建，**一一对应**。每次构建 17–57 秒，且是排队资源。用 Contents API 推 9 个文件 = 9 次提交 = 9 次构建，等于把 17 秒乘以 9，还要在中途接受「文件 A 生效、文件 B 还没生效」的窗口期。Git Data API 把所有文件压进**一个** tree、一个 commit，构建只触发一次。

---

## 3. 五步配方，以及三个会静默毁掉结果的地方

```
GET  /git/ref/heads/main                      ← 当前 head，兼做并发检查
GET  /git/commits/{head}                      ← base_tree（必须带！）
POST /git/blobs            × N               ← base64，二进制安全
POST /git/trees            {base_tree, tree}  ← 相对 base_tree 的增量
POST /git/commits          {message, tree, parents:[head]}
PATCH /git/refs/heads/main {sha, force:false} ← 唯一的生效点
```

**坑一：忘了 `base_tree`。** 不带 `base_tree` 建出来的 tree 是**从零开始**的——你以为在加一个文件，实际提交里只剩下这一个文件，其余全部消失，而且没有任何报错。这是这套 API 里最贵的错误：删掉的东西要从不带这东西的别人的 clone 里捞。

**坑二：`mode` 不传就默认非可执行。** 删 `mode` 字段的后果是脚本上线后 `chmod -x`：本仓库的 `.sh` 与 Python 入口都得显式带 `mode`（`100644` / `100755`）。删除文件则**必须显式写 `"sha": null`**，漏掉就是「以为删了其实没删」。

**坑三：`force:false` 不是保险，是信号。** 你在读 head 之后、移 ref 之前，可能有人推过——此时 PATCH 返回 `422`，意思是「你算的父提交已经不是 head 了，这不是快进」。**这个 422 必须当成中止，不能重试成 force**：它恰好是唯一能保护你「不覆盖别人提交」的机制。我的工具把 422 归类为 `EXIT_MISMATCH = 2` 并提示重跑（重新读 head 再算一遍），绝不 force。

实测一次完整六步（`bench` 子命令，作用在临时分支上，主线 ref 不动）：

```
API 调用      : 8 次 / 7.40s
  GET    /git/ref/heads/main          990.2ms  200
  GET    /git/commits/d3fcd4d...      963.4ms  200
  POST   /git/blobs                   880.1ms  201
  POST   /git/trees                   799.9ms  201
  POST   /git/commits                 691.2ms  201
  POST   /git/refs                    904.8ms  201
  DELETE /git/refs/heads/bench-...   1593.2ms  204
  GET    /git/ref/heads/main          581.9ms  200
临时分支      : bench-1792390164 已删除
主线未动      : d3fcd4d -> d3fcd4d OK
```

单次往返 **582–1593ms**，六步合计约 **5.2 秒**（8 次调用 7.40s 含两次基准专用调用）。对比 `git ls-remote` 的 1047ms：**通道慢，但通道能用**。判断标准从来不是「快不快」，是「出不出结果」。

---

## 4. 226 个 blob：HTTP 200 不算证据

推完了，接口全 200，ref 也指向新提交了。到这里收工，是 90% 的人的做法。我多做了一步：把远端树的 blob SHA 全拉下来，和本地逐个比。

第一次推送完成后（commit `d3fcd4d`，tree `330aed7`）：

```
远端 blob 数 = 226   本地 blob 数 = 226
仅远端有 = 0         仅本地有 = 0
共同 226 个：SHA 相同 224，不同 2
不同样本：docs/articles/cli-gate-false-green-audit-guide.html
          tools/gate/samples/audit_summary.json
CRLF 检查：这 2 个文件全部是纯 CRLF（0 个裸 LF），其余 224 个没有一个带 CRLF
```

不是巧合。那两个文件正是被自动化写出来、还没经过编辑器「修正」的文件。看一个文件的具体数字：

```
工作区字节 : 21499 B   CRLF=309  裸LF=0
索引字节   : 21190 B   CRLF=0    裸LF=309     ← delta = 309 = 行数

git hash-object --path=<file> <file>   → 530dc373...（= 索引 blob）
git hash-object --no-filters <file>    → ea762840...（= 工作区原始字节）
远端 blob                               → ea762840...  ← 与服务端一致
```

根因是这台机器的 `core.autocrlf=true`，仓库里又**没有 `.gitattributes`**：checkout 时 LF→CRLF，commit 时 CRLF→LF，于是「工作区字节」和「仓库字节」本来就是两份不同的东西。我的第一版推送脚本直接读工作区文件、base64 上传——内容当然是对的（浏览器不在乎 CRLF），但**本地 blob SHA 和远端 blob SHA 从这一刻起永远对不上**，于是：

- 本地提交树 `6aaca1ec` ≠ 远端树 `330aed73`
- 用树 SHA 对账会得出「推送失败」的假警报
- 反过来，如果谁用树 SHA 相等来*证明*推送成功，这个证明在 CRLF 文件面前是无效的

**修法只有一个：别读工作区，读索引。** `git cat-file blob <HEAD:path>` 拿到的就是已归一化的字节，用它去建 blob，远端 SHA 必然等于本地 blob SHA，树 SHA 才成为可用的证据。工具默认 `--source index`，只有显式 `--source worktree` 才退回工作区字节，并逐文件打印 `WARN ... CRLF=309 vs 索引` 提示差异。切到 index 之后，同一棵树的本地/远端 SHA 完全相等。

顺便记一条更通用的结论：

> 「接口返回 200」证明的是**请求被接受**；只有**内容寻址的比较**（blob/tree/commit 的 SHA）证明的是**字节被正确执行**。凡是有 SHA 的系统（git、OSS、镜像仓库），都应该拿 SHA 当验收标准，而不是拿状态码。

---

## 5. 一次真实的翻车：33 个文件 vs 9 个

这一节是给未来的自己看的。同一个提交，我第一版用 `git add -A`，`git diff --name-status` 数出来是 **33 个文件**：

- 真正要发布的 **9 个**：文章 md/html、`convert_articles.py`、`docs/index.html`、`docs/sitemap.xml`、`README.md`、`tools/gate/gate_lint.py` 及其样例
- 顺手被拖进来的 **24 个**：7 个一次性的 `push_*.py` 脚本、2 个 `__pycache__/*.pyc` 编译产物、**一整个 5.1MB 的样本目录**（11 个 GitHub 事件 JSON + 取证输出）

后者对线上站点没有任何作用，只会让每次 clone 和每次构建都拖着 5MB 走。处理办法不是「下次小心」，是**让仓库自己拒绝**：

```
__pycache__/
*.pyc
push_*.py
```

`.gitignore` 落地后重新 `git add` 明确点名 9 个路径，提交从 33 个文件降到 9 个。这条规矩值得写进任何自动化发布流水线：**发布提交不要用 `git add -A`**——你永远不知道工作区里躺了什么（尤其是跑过一堆脚本的机器）。

---

## 6. 上线验证：三个 URL，缺一不可

推送成功 ≠ 页面能访问。Pages 是异步构建的（本次 17.5 秒），构建完成前访问老内容或 404。我的验证三件套是：

```
1. 新文章页   200  21499 B   正文关键词命中 1 次
2. 线上 sitemap.xml  200   6775 B  含新 slug
3. 线上 index.html   200  18531 B  含新文章链接
```

三个都要，因为它们的失败模式不同：新页 404 说明构建还没到或路径错；sitemap 未更新说明「页面在但搜索引擎看不见」；index 未更新说明导航断了。两个必带参数：

- **真实浏览器 UA**：GitHub Pages 前置 CDN 对匿名脚本 UA 会返回壳或空体，不带 UA 时连不存在的页面也可能是 200；
- **`?cb=<时间戳>`**：破 CDN 缓存。本次刷新后立刻拿到 21499 B，恰好等于工作区那份带 CRLF 的文件大小——顺带印证了远端存的就是 CRLF 版本。

顺带一个 MSYS 上的小坑：`curl -o /dev/null` 在这里 exit 23（写失败），`-o /tmp/x`（原生程序不认 MSYS 路径）会把文件写到 `C:\tmp`。脚本里写 `-o C:/.../probe.bin` 最省事。

---

## 7. 12 项上线检查清单

1. 先 `probe`：五条通道跑一遍，别在断掉的通道上重试
2. `401` 当可达，`000` 当阻断——这两者搞反，判断全反
3. Git Data API 的 `base` 必须**等于**远端当前 head，不等就中止（我的工具 `EXIT_PRECONDITION=3`）
4. 建树必带 `base_tree`，否则整棵树被替换
5. 建 blob 用**索引字节**（`git cat-file`），不用工作区字节
6. 保留 `mode`（`100644`/`100755`），删除条目写 `"sha": null`
7. 二进制一律 base64，不做任何文本处理
8. `PATCH ref` 用 `force:false`；收到 **422 就重新读 head 重算**，绝不 force
9. 推送后逐 blob 比对 SHA，别只信 200
10. 发布提交点名路径，不用 `git add -A`；`.gitignore` 先落地
11. 验证新页 + sitemap + index **三个** URL，带 UA 与 cache-buster
12. 记录构建耗时（`/pages/builds`），逐文件推（Contents API）会让它乘以文件数

---

## 8. 工具

```bash
python tools/publish/gh_tree_push.py probe            # 五条通道判活，给出「走哪条路」的结论
python tools/publish/gh_tree_push.py bench            # 临时分支量六步耗时，主线不动
python tools/publish/gh_tree_push.py push --base d3fcd4d --dry-run   # 只建树，报「本地树 == 远端树」否
python tools/publish/gh_tree_push.py push --base d3fcd4d             # 真推，推完逐 blob 对账
python tools/publish/gh_tree_push.py verify --paths articles/x.html,sitemap.xml \
       --url-prefix https://felixwang007.github.io/awesome-content-tools --needle x --wait 180 --cb
python tools/publish/gh_tree_push.py selftest          # 20 项断言，不联网
```

退出码：`0` 通过 / `2` 对账或验证不一致 / `3` 前置条件不满足（用法、base 不符、无改动）。`2` 和 `3` 分开，是因为前者意味着「你得去看看线上到底变成什么样了」，后者意味着「重新读一次状态再跑就行」。

自检里有一项值得单独说：`blob sha(hello\n) == ce013625030ba8dba906f756967f9e9ca394464a`。这是 git 官方文档里那个经典例子，用它证明「我自己算的 blob SHA 和 git 的一致性」——如果这行断言挂了，说明我的对账逻辑本身不可信，那么整篇文章里的 224/2 也就没有意义。

---

## 9. 边界声明

这套东西不替代 git：

- **不支持** tag、子模块、LFS 大文件、签名提交；单个 blob 走 JSON+base64，文件越大请求体越大（几百 KB 以上建议还是等 git 通道恢复）
- **依赖 token scope**（本机用 `gh auth token`），比 SSH key 的权限面更宽，脚本里别落盘明文
- **只处理 `base..HEAD`**：删除、重命名都按路径条目处理，不做内容相似度推断
- **不做合并**：并发写者只会导致 422 中止，需要你人工重新读 head 再算
- 本文所有耗时都是**本机到 GitHub 的一次实测**，不同线路差异会很大；可复现的是**方法**（判活三态、内容寻址对账、三点验证），不是那些毫秒数

一句话收尾：网络会断是常态，但「断了就用不了」是设计缺陷。把通道判活、把对账做成内容寻址、把验证做成三个 URL，自动化才算真的跑通——不然它只是「在本地看起来跑通了」。
