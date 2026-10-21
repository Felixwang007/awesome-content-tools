# 装 AI 技能/工具之前的三层验尸：1105 星的仓库里只有一个 README，写着「不要信这个仓库」

装别人的技能包、MCP 服务器、CLI 工具之前，我们通常做两个检查：看星数，看有没有人骂。两个都不算检查——星数衡量注意力，骂声衡量传播速度。

本文是 2026-10-21 早上一次真实排查的完整记录，三个样本全部当场实测：

- 一个 **1105 星**、创建 4 天的仓库，结构取证判 `FARM_SUSPECT`，内容扫描判 `CLEAN`，而**整个仓库只有一个 README，内容是叫你别用这个仓库**。
- 一个 97 个文件、84 篇 SKILL.md 的红队技能库，内容扫描出 **203 条 HIGH/MEDIUM 命中**，判 `REJECT_REVIEW`——而 **203 条全部落在 `.md` 里，安装路径上的代码一条没中**。
- 四个被今天这轮扫描判成 `CLEAN` 的仓库，其中一个是文件数 = 1。

三个案例对应三层验尸，任何一层单独用都会给你一个"看起来正确"的错误结论。工具在仓库 `tools/supplyaudit/`，两份可复算的原始回包在 `samples/`。

---

## 一、第一层：结构取证（不看内容，只看形态）

第一层不解包、不 import、不联网——输入是 GitHub API 的原始回包，离线分析、可复算：

```bash
gh api "search/repositories?q=created:>2026-10-14+stars:>50&sort=stars&per_page=30" > repos.json
gh api repos/<owner>/<repo>/events > events/<owner>__<repo>.json
python repo_forensics.py analyze --repos repos.json --events-dir events/
```

判据是形态而不是热度：**首发延迟**（创建后多久就 push 完 = 一次性搬运还是长出来的）、**fork/star 比**（有没有人在二次使用）、**体积**（装不装得下工程）、**WatchEvent 到达形态**（星是摊开的还是一分钟内挤出来的）。

今天这轮的读表（30 个仓库，`created:>2026-10-14 stars:>50`）：

| 仓库 | 判定 | ★ | fork比 | 首发延迟 | 体积KB | watch形态 | 触发的理由 |
|---|---|---|---|---|---|---|---|
| xscreensaver-official/xscreensaver | **FARM_SUSPECT** | 1105 | 0.029 | 277s | 2 | 28/SPREAD | 无 license；fork比 0.029 < 0.03；体积 2KB < 60KB；首发延迟 277s < 900s |
| JoasASantos/REA-Prompts | NEEDS_EYES | 1113 | 0.146 | 94295s | 39 | 26/SPREAD | 无 license；体积 39KB < 60KB |
| stackedlol/ascii-sharks | NEEDS_EYES | 605 | 0.073 | 246s | 26 | 28/SPREAD | 无 license；体积 26KB < 60KB；首发延迟 246s < 900s |
| deepseek-ai/DeepInfer | ORGANIC | 14831 | 0.097 | 421701s | 2965 | 26/SPREAD | 四条结构判据全部未触发 |
| LiCinCan/blender-ai-mcp-addon | ORGANIC | 947 | 0.408 | 360055s | 110274 | 25/SPREAD | 四条结构判据全部未触发 |
| ctresb/leis | ORGANIC | 541 | 0.131 | 522517s | 2527962 | 27/SPREAD | 四条结构判据全部未触发 |

分布：`FARM_SUSPECT=1, NEEDS_EYES=2, ORGANIC=27`；30 个里 4 个无 license、4 个体积 < 60KB、星数中位数 1093。

**同一个工具、同一套阈值，四天前（10-14 那轮）的分布是 `FARM_SUSPECT=0, NEEDS_EYES=7, ORGANIC=23`。** 两次样本都是 30 个、都是 7 天窗口、阈值一个字没改，可疑率从 0% 变成 3.3%，`NEEDS_EYES` 从 7 掉到 2。这组对照说明的是阈值性质：**它们是"把人叫过来看"的触发器，不是对世界的测量。** 把这类分数接进 CI 当门禁，等于把某一天的样本分布焊成永久标准。

### star:watch 为什么不做判据

星数除以 watch 数（真正点"关注"的人，比 star 稀缺得多）看起来是最有区分度的指标。今天这轮实测：

- `FARM_SUSPECT` 那一个：**39.5**
- `NEEDS_EYES` 两个：42.8、21.6
- `ORGANIC` 二十七个：40.8、44.0、47.9、56.3、68.4、71.0、71.5、72.5、109.8、144.8、522.1、570.4 …

农场的 39.5 稳稳落在正常库的区间里（40.8 就不比它高）。**这个指标在本机历史实测中一直落在 180–380，两类样本完全重叠——用它做判据等于用噪声。** 工具因此显式不使用它，并把这个决定写进文档，而不是留给下一个人重新发现。

### 第一层的诚实边界

`/repos/{r}/events` 只回最近 90 天、最多 300 条。所以：

- `watch_count` 是**下界**，不是总数；
- 到达形态只在可见窗口内成立；
- events 拿不到时，形态判据置为 `UNKNOWN`，**不假装测过**。

今天 30 个仓库全部取到了 events，但表里 `span_hours` 有的只有 0.98 小时（REA-Prompts 26 个 watch 挤在 1 小时内但摊开分布）——窗口窄本身会让"挤簇"判据失效，这是样本量问题不是参数问题。

### 一个反直觉的实测：到达形态在这 60 个样本上从未改变过判决

两轮样本（10-14 的 30 个 + 10-21 的 30 个，合计 60 个仓库）做个对照实验——把 events 回包**全部拿掉**重跑，判定分布逐条比对：

| 样本 | 带 events | 不带 events | 判定变化 |
|---|---|---|---|
| 10-14（30 个） | `NEEDS_EYES=7, ORGANIC=23` | `NEEDS_EYES=7, ORGANIC=23` | **0 处** |
| 10-21（30 个） | `FARM_SUSPECT=1, NEEDS_EYES=2, ORGANIC=27` | `FARM_SUSPECT=1, NEEDS_EYES=2, ORGANIC=27` | **0 处** |

再把 60 条 `reasons` 全过一遍：**含到达形态（BURST/挤簇）的条目 = 0 条**。也就是说，在这两轮真实样本上，WatchEvent 到达形态是一个**解释性字段，从来没有当过判据**——所有判定都由 license / 体积 / fork比 / 首发延迟这四条静态形态给出。

这不是说到达形态没用（农场形态如果晚几天才开始刷星，静态判据会全部落空，那时只有挤簇能看出来），而是说：**"某条判据在我的样本上生效过"这件事必须实测，不能假设。** 一条从没触发过的判据和一个正确的判据，在输出上长得一模一样。

---

## 二、第二层：内容静态审计（不解包）

第一层告诉你"这个仓库的形态像不像话"，不告诉你"里面的代码会不会做事"。第二层把归档**当作数据读**，不落盘、不解包、不 import：

```bash
python supply_audit.py scan repo.tar.gz --json out.json
```

规则集 15 条，分 HIGH / MEDIUM 两级。今天拿一个真实的红队技能库当样本（MIT、84 篇 SKILL.md、97 个文件）：

```
文件: 共 97 | 扫了文本 95 | 二进制 0 | 超大跳过 0 | 共 4.47 MB
命中规则: H5_credential_paths×125, M4_download_then_exec×25, H6_persistence×21,
          M6_docs_pipe_shell×15, H4_reflective_pe_load×8, H3_invoke_expression×7,
          M3_obfuscation×1, H9_base64_exec×1
结论: REJECT_REVIEW (exit 3) — 有 HIGH 命中，默认不装
```

203 条命中，125 条是"读取凭据类路径"、25 条"下载后改权限执行"、21 条"写自启动"。按规则一算，这个包看起来像个后门合集。

**但按文件类型拆开，结论完全反过来：**

| 规则 | 落在代码文件 | 落在文档/数据文件 |
|---|---|---|
| H5_credential_paths | 0 | 125 |
| M4_download_then_exec | 0 | 25 |
| H6_persistence | 0 | 21 |
| M6_docs_pipe_shell | 0 | 15 |
| H4_reflective_pe_load | 0 | 8 |
| H3_invoke_expression | 0 | 7 |
| M3_obfuscation / H9_base64_exec | 0 | 1 / 1 |
| **合计** | **0** | **203** |

`.md` 201 条、`.json` 2 条、可执行代码 **0** 条。再看安装面：这个包顶层是 `install.sh` + `convert_skills.py` + `tools/build_manifest.py`，`install.sh` 干的事是把 `Skills/` 目录**拷贝**到 `~/.claude/skills/`，没有一条网络请求、没有一条动态执行。扫描器对这三个文件零命中。

所以正确的结论不是"这个包有 203 个危险点"，而是：

> **203 条全部是"文档里写着的攻击样例"，安装路径干净；风险在 prompt 层（把这些 SKILL.md 喂进 agent 上下文），不在安装层。**

这就是 `REJECT_REVIEW` 里那个 `REVIEW` 的含义——它不是判决，是"你得亲手把每条命中的**执行可达性**分开"。

### 判别式：命中落点 vs 执行面

拿到命中列表后只问一个问题：**这条字符串，什么时候会被执行？**

| 落点 | 实际执行时机 | 处置 |
|---|---|---|
| `install.sh` / `setup.py` / `Makefile` / `.pth` | `pip install` / `git clone` 后你手动跑的那一下 | 最高优先级，逐条读完 |
| `package.json` 的 `pre/post/install` 钩子里有网络下载（M1） | `npm install` 时**自动**执行 | 等价于 `curl\|bash`，默认拒绝 |
| 源码在**导入时**就发网络请求 / 拉二进制 | `import` 即触发 | 默认拒绝，与是否"官方"无关 |
| 源码函数体里的能力（需要你调用） | 你主动调用时 | 看它读什么路径、发去哪 |
| 文档 / 示例 / 测试数据里的攻击字符串 | **除非你复制粘贴并运行** | 记录，不作否决理由 |

今天这个包恰好是最后一行，而且是**整包 100% 最后一行**。反过来，一个 3 行的 `install.sh` 里藏着 `curl | sh`，只需要 1 条命中就该拒绝——**命中条数不携带风险信息，落点携带。**

扫描成本也是决策变量：22KB 归档 158ms、316KB 归档 206ms、2.1MB（解包 4.47MB / 97 文件）944ms。**"装之前扫一遍"这件事没有时间成本这个借口。**

---

## 三、第三层：读那个文件

回到今天被判 `FARM_SUSPECT` 的那个仓库。第一层的四条理由全部成立（无 license、fork比 0.029、2KB、首发延迟 277s）。第二层的内容扫描：

```
文件: 共 1 | 扫了文本 1 | 二进制 0 | 超大跳过 0 | 共 0.00 MB
命中规则: 无
结论: CLEAN (exit 0)
```

**1 个文件、0.00 MB、零命中、exit 0。** 如果只跑第二层，这是今天最干净的一个包。而这个唯一的文件内容是：

> **There is no official XScreenSaver GitHub repository.**
> I am jwz, the author of XScreenSaver, and I do not publish a public source code repository. I publish releases as tar files on the official web site and only there. If you see any other XScreenSaver repository (on GitHub or elsewhere) that has nothing to do with me and **you should not trust it.**

1105 个星、4 天前创建、创建后 277 秒就推完、整个仓库一个 README——**这个仓库不包含任何代码，它包含的是一条免责声明**。

这里必须诚实：这个仓库有两种可能，一种是作者本人占位以便劝退，一种是别人冒名后被发现。**两种可能里，1105 个星都同样只衡量了注意力。** 星不是可用性证据，也不是安全性证据——它只证明有人点了一下。

三层各自的输出并排放：

| 层 | 观测 | 结论 | 对决策的贡献 |
|---|---|---|---|
| 结构取证 | 1105★/0.029/2KB/277s/无license | `FARM_SUSPECT` | 指出"该来看一眼"，**不能指出为什么** |
| 内容扫描 | 1 文件 / 0 命中 / exit 0 | `CLEAN` | 技术上说真话："我这套规则没看到东西" |
| 读那一个文件 | 全文 6 行，内容是"别信这个仓库" | 人类判决 | **唯一能落地的结论** |

结构层对、内容层对、结论只有第三层给得出。**内容扫描的 `CLEAN` 语义是"本次规则集没看到"，不是"安全"。** 一个 0 文件的项目、一个只有 README 的项目、一个把 payload 全写在 README 里的项目，都会得到 `CLEAN`。

---

## 四、退出码语义：把"没输入"和"输入干净"分开

这类工具最容易犯的错是让空集合满足全称命题——扫了 0 个文件，"没有发现问题"自动成立，于是 exit 0、全绿、可以合并。两个工具都把零输入做成独立的非成功状态：

`supply_audit.py`：

| 码 | 含义 |
|---|---|
| 0 | 干净（只有 LOW 或无命中）——**不等于安全证明** |
| 2 | 只命中 MEDIUM，需人看一眼 |
| 3 | 命中 HIGH，默认拒绝安装，除非逐条解释 |
| 4 | **零输入 / 路径不可读 / 归档里没有可扫文件** ← 「没输入」不是「输入干净」 |
| 5 | selftest 失败 |

`repo_forensics.py`：`0 无可疑 / 2 有 NEEDS_EYES / 3 有 FARM_SUSPECT / 4 零输入 / 5 selftest 失败`。

两个都自带自检，读数当场跑：**`supply_audit selftest` 20/20 通过**（含"干净样本假阳性=0"、"tar.gz 不解包扫描结果与目录扫描一致 19=19"、"归档扫描期间未释放 payload 到磁盘"、"下载不完整的归档 → exit 4 而不是崩溃也不是全绿"），**`repo_forensics selftest` 11/11 通过**（含"缺一半信号 → NEEDS_EYES 而不是直接判农场"、"watch 数少于阈值不判 BURST"、"events 缺失记 UNKNOWN 不假装测过"、"同输入两次判定一致"）。

### 今天踩到的一个小坑

`--quiet` 会**同时**抑制 `--json` 落盘（源码里是 `if args.json_out and not args.quiet`）。我第一遍加 `--quiet` 跑，屏幕上结论是 `REJECT_REVIEW`，回头找 `out.json` 却没有——**安静模式连审计证据一起安静掉了**。在 CI 或发布流水线里想留证据，就不能用 `--quiet`。这不是 bug，但它是个默认值陷阱：**能出结论不等于留了证据。**

---

## 五、装之前的 12 项检查（按顺序，越靠前越便宜）

1. 星数只当发现面，**不当判据**；先看 `fork/star`——fork 是二次使用，star 是点赞。
2. 看 `created_at` → 第一次 push 的间隔：几分钟内推完的，是搬运不是长出来的。
3. 看体积：几十 KB 号称完整工程，先去读它到底装了什么。
4. 看有没有 LICENSE。无 license ≠ 恶意，但意味着**你没有使用授权**，企业环境直接卡死。
5. 拉 `/events` 看 WatchEvent 到达形态；events 缺失就记 UNKNOWN，不要用"没发现异常"替代。
6. 下载归档（`codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/main`），**先扫再解包**，别 `git clone && cd && ./install.sh`。
7. 定位安装面：`install.sh` / `setup.py` / `Makefile` / `package.json` 的 `pre|post|install` / `.pth` / `sitecustomize.py`。这几个文件里的每一条命中都独立成立。
8. 命中要按落点分类，不是按条数排序：**安装即执行 > 导入即执行 > 需调用 > 仅文档**。
9. 搜索"import 期副作用"：顶层代码里的网络请求、文件写入、`exec/eval`。
10. 可执行/二进制文件（`.node/.dll/.so/.jar`）出现即 HIGH，无源码可读的一律不进生产。
11. 读 README 全文。今天最值钱的结论就来自那 6 行——规则扫不出来，因为它不是代码。
12. 装完留证据：用 `--json` 存一份扫描结果（别加 `--quiet`），出问题时拿同一份输入复算，而不是靠回忆"当时好像扫过"。

---

## 六、边界声明

- 本文所有数字均为 **2026-10-21 上午本机实跑**（Windows 11 + git-bash + Python 3.13/3.14，GitHub API 由 `gh` CLI 调用）。星数、fork 数、体积、首发延迟**每一分钟都在变**，表里的值只能证明"当时是这样"，不能当常量引用；结论是"要这样测"，不是"这几个仓库有问题"。
- `supply_audit.py` 是**静态字符串规则**，不解析 AST、不做数据流分析、不执行代码。它能看见语法层面的模式，看不见语义层面的意图：**它能告诉你"这里有一条 `curl | sh`"，不能告诉你"这条是无害的示例还是真的后门"。** 命中是线索，可复核的证据行才是产物。
- 归档模式不落盘解包，避免"审计动作本身执行了 payload"；但扫描器本身用 `tarfile/zipfile` 读归档，**对恶意构造的归档不做防护**（如归档炸弹）——超过 2MB 的单文件只记大小不扫内容。
- `repo_forensics.py` 的阈值是**从真实样本上校准的启发式**，不是硬件事实；`FARM_SUSPECT` 是"把人叫过来看"的触发器，不是"判定为恶意"。真正的判定必须看内容（第二层）加人读（第三层）。
- 两个工具都只覆盖"这个包/这个仓库"，不覆盖模型行为：包干净不等于 agent 用它的方式干净。

### 复现材料

- `tools/supplyaudit/repo_forensics.py` —— 结构取证器（`analyze` / `selftest`，11 项自检，纯标准库，离线可复算）
- `tools/supplyaudit/supply_audit.py` —— 内容静态审计器（`list-rules` / `scan` / `selftest`，15 条规则，20 项自检，零依赖，支持 tar.gz/zip 不解包）
- `tools/supplyaudit/samples/repos_20261021.json` —— 今天这轮的 GitHub 原始回包（30 个仓库）
- `tools/supplyaudit/samples/events20261021/` —— 对应的 30 份 events 回包（1.2MB，为了上面那张读表可逐格复算）
- `tools/supplyaudit/samples/forensics_20261014.json` / `forensics_20261014.txt` / `repos_20261014.json` —— 10-14 那轮的对照样本（`NEEDS_EYES=7, ORGANIC=23`）。**这一组不附 events 回包（原目录 4.8MB）**，因为实测下来不带 events 重跑判定 0 处变化（见上一节的对照实验），形态字段会记为 `UNKNOWN` 而不是猜一个值
- `tools/supplyaudit/out_f20261021.json` —— 本文读表的原始判定（含每条 reason 与全部中间读数）

一条命令跑完第一层：

```bash
python repo_forensics.py analyze --repos samples/repos_20261021.json \
  --events-dir samples/events20261021/ --json /tmp/f.json
```

一条命令跑完第二层（把 URL 换成你要装的那个）：

```bash
curl -sL -o pkg.tar.gz https://codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/main
python supply_audit.py scan pkg.tar.gz --json pkg_audit.json
```

**先看落点，再看条数；先读 README，再信星数。**
