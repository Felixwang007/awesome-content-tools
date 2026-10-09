# Windows 上跑 AI Agent 自动化：同一个 /tmp 有两个、端口被系统留着、PYTHONPATH 会劫持 import

Agent 在 Windows 上跑自动化，最常见的三类"没有报错的失败"：

- 脚本 exit 0，日志也正常，但**下游读不到文件**——因为写和读的两个进程看到的 `/tmp` 不是同一个目录。
- 服务起不来，报 `WinError 10013`，代码里找不到任何问题——因为**那个端口被 Windows 预留给 Hyper-V/WSL/Docker 了**，谁都不许绑。
- 子进程一启动就崩，或者 import 到别的项目的同名模块——因为**父进程的 `PYTHONPATH` 被继承了**，解释器找包的顺序和你想的不一样。

这三类都不是模型的问题，也不是框架的问题，是 Windows 环境的问题。本文所有数字都来自本机实跑（Windows 11 / git-bash / Python 3.13），复现脚本在仓库 `tools/winenv/`，一份真实快照存在 `tools/winenv/samples/live_snapshot.json`。

---

## 一、同一个 `/tmp`，在 bash 和原生程序里是两个目录

先看实测。同一个时刻，同一个词 `/tmp`：

```bash
$ cd /tmp && pwd -W                     # shell 眼里
C:/Users/<user>/AppData/Local/Temp

$ python -c "import os;print(os.path.abspath('/tmp'))"   # 原生程序眼里
C:\tmp
```

证据不止于路径打印。用原生 `curl` 往 `/tmp` 写一个文件：

```bash
$ curl -s -o /tmp/curl_probe.txt https://example.com -w "http=%{http_code}\n"
http=200
$ ls -la C:/tmp/curl_probe.txt
-rw-r--r-- 1 Administrator 197121 577 10月  9 14:01 C:/tmp/curl_probe.txt
```

文件确实落到了 `C:\tmp`。而在同一个 bash 会话里 `ls /tmp` 列出来的是另一批文件（`AppData\Local\Temp` 里的东西）。**两条路径都"存在"，都"可写"，都不报错**——所以这类失败永远不会出现在日志里，只会出现在"为什么下游拿不到文件"这种问题里。

机制：git-bash/MSYS 给 shell 内建命令（`cd`/`ls`/重定向）做了自己的路径映射，`/tmp` 是 MSYS 安装目录下的挂载点；而给**原生 Windows 程序**（python/curl/node/git）传 `/tmp`，它只是一个相对当前盘符的路径，落到 `C:\tmp`。两个世界各自自洽。

更常见的变体是传 `/c/Users/...` 这种 MSYS 风格路径给原生程序。在本机（MSYS 路径转换被关闭）它不会被翻译成 `C:\Users\...`，而是被当成相对路径 `c/Users/...`，于是文件写到意料之外的地方，或者报 `No such file or directory`，而报错的命令看起来完全正确。

### 修法

| 做法 | 说明 |
| --- | --- |
| 绝对路径 + 正斜杠原生形式 | 给 git/curl/node/python 传 `C:/Users/.../file.txt`，不要传 `/c/...`，也不要传 `/tmp/...` |
| 统一暂存目录 | 全流程只用一个目录（如 `TMPDIR` 指向的 scratch），由脚本显式创建并传入，不依赖任何工具的默认临时目录 |
| 写后读校验 | 写入后立刻用同一个进程读回并打印路径与字节数，路径分歧会当场暴露，而不是在三步之后 |
| 别用 `/tmp` 当常量 | 把临时目录做成参数或环境变量，而不是硬编码在脚本里 |

---

## 二、被系统预留的端口：`WinError 10013` 不是代码问题

Windows 会把成段端口留给 Hyper-V / WSL / Docker 等组件。本机实测：

```bash
$ netsh int ipv4 show excludedportrange protocol=tcp
开始端口    结束端口
      1066        1165
      1166        1265
      1266        1365
      4265        4364
      4365        4464
      4465        4564
     50000       50059     *
```

带 `*` 的是"管理的端口排除"（系统动态保留）。落在这七段里的端口，绑定一定失败。逐个实测绑定：

| 端口 | 落在 | 绑定结果 |
| --- | --- | --- |
| 1241 | 1166–1265 | **FAIL errno 13**（WinError 10013，访问权限不允许） |
| 1300 | 1266–1365 | **FAIL errno 13** |
| 7860 | 段外 | OK |
| 8300 | 段外 | OK |
| 50010 | 50000–50059 管理段 | 本次 OK（管理段是动态的，今天能绑不代表明天能绑） |

值得注意的三点：

1. **报错信息误导人**。"以一种访问权限不允许的方式做了一个访问套接字的尝试"听起来像杀毒软件或权限问题，实际是端口在保留段里。
2. **段不是固定的**。排除段会随 Hyper-V/WSL/Docker 的启停重新计算。上个月能跑的 1241，今天就在段里——这是典型的"昨天还好好的"。
3. **硬编码端口 = 定时炸弹**。遇到过的最硬的一例是某个本地 UI 把端口写死在 7860，而目标机器上 7786–7885 全在保留段内，进程连启动的机会都没有（这不是代码能重试解决的问题，重试一百次也是 errno 13）。

### 修法

```bash
# 1. 先看排除段（每次部署前都看，不要缓存）
netsh int ipv4 show excludedportrange protocol=tcp

# 2. 用 Python 探测目标端口是否真的能绑（比"端口没被占用"更准）
python -c "import socket;s=socket.socket();s.bind(('127.0.0.1',8300));print('OK')"
```

- 服务端口选 1024–49151 之间的**固定段**，避开 49152+ 动态段（会被系统随机占用）。
- 端口用配置项，不要写死；绑定失败时**换端口**而不是重试同一个端口。
- 启动脚本里加一行前置探测，失败就带着"排除段清单"报错退出，让排障从 30 分钟变成 10 秒。

---

## 三、`PYTHONPATH` 继承：子进程 import 到别的项目

实测三段（同一台机器、同一个解释器）：

```bash
$ python -c "import fake_mod"                       # 没有 PYTHONPATH
ModuleNotFoundError: No module named 'fake_mod'

$ PYTHONPATH="C:/scratch/pp_demo" python -c "import fake_mod;print(fake_mod.POLLUTED)"
child imported foreign module, POLLUTED= 1          # 静默命中外部模块

$ PYTHONPATH="C:/scratch/pp_demo" env -u PYTHONPATH python -c "import os;print(os.environ.get('PYTHONPATH'))"
None                                                # 键被删掉，干净
```

`PYTHONPATH` 在前面，`sys.path` 里自己应用的路径在后面——所以只要目录里有同名模块，子进程就会用别人的。在 Agent 自动化里这特别容易发生，因为一个宿主程序经常被 `subprocess` 用来驱动好几个不同项目的 venv。

症状往往是这三类之一，而且日志里看不到"路径串味"的字样：

- **`0xC0000142`（DLL 初始化失败）**：加载到另一个环境的 `torch_python.dll` / `_imaging.pyd` 等，只报一个退出码。
- **模块版本错**：同名包却是另一个版本，报的错是"这个函数不存在"，看着像代码写错了。
- **C 扩展不兼容**：例如 cp311 的 `.pyd` 被 cp314 的解释器加载，报 `ModuleNotFoundError` 而包明明装着。

### 修法：三层防御，缺一层就会漏

```python
# 第 1 层：入口文件最顶部，任何业务 import 之前
import sys
sys.path = [p for p in sys.path if "hermes-agent" not in p]   # 换成"不属于本应用的路径"

# 第 2 层：清环境变量——用 pop 删键，不要设成空字符串
import os
for k in list(os.environ):
    if k.startswith("PYTHON"):
        os.environ.pop(k, None)
```

```bat
:: 第 3 层：启动脚本里删键（set X= 是删除，set X="" 仍会泄漏空串）
@echo off
set PYTHONPATH=
set PYTHONHOME=
"%VENV%\Scripts\python.exe" app.py
```

还有一个容易漏的点：**别用 `--reload`**。开发服务器 reload 出来的子进程继承的是 shell 的环境，不是你进程里改过的 `os.environ`——第 1、2 层防御会在 reload 那一刻失效。

---

## 四、附带两坑：编码不统一、后台进程随会话陪葬

### 编码：同一台机器上，不同原生命令的编码不一样

```
cmd /c echo 中文测试        →  按 UTF-8 解码：UnicodeDecodeError: invalid continuation byte
                            →  按 GBK 解码：中文测试            ✅
netsh ... excludedportrange →  按 UTF-8 解码：正常
```

同一个宿主、同一时刻，`cmd` 的提示语是 GBK，`netsh` 的输出是 UTF-8。所以"统一用 UTF-8 读子进程输出"这句话在 Windows 上不成立，正确姿势是**先试 UTF-8、失败回退 GBK，并且永远带 `errors="replace"`**，同时把"解码失败"当成可见事件而不是静默替换：

```python
def decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")   # 两条路都失败：留下替换符，别吞掉
```

### 进程生命周期：`nohup` 挡不住会话结束

- `nohup cmd &` 在 Windows 上**不能**让进程脱离 bash 会话，会话结束照样被杀——于是"启动成功"的服务在几分钟后消失，日志戛然而止，看不出原因。要让进程真正脱离，用 PowerShell 起独立进程并把 stdout/stderr 重定向到文件：

```bash
powershell -Command "Start-Process -WindowStyle Hidden -FilePath 'C:\app\app.exe' -RedirectStandardOutput 'C:\app\logs\out.log' -RedirectStandardError 'C:\app\logs\err.log'"
```

- `taskkill //F //PID 1234` 在 git-bash 里会报 **`错误: 无效参数/选项 - '//F'`**（本机实测）——因为路径转换被关闭，`//F` 没有被改写成 `/F`。改用 `cmd //c taskkill /F /PID 1234`，或者 `powershell -Command "Stop-Process -Id 1234 -Force"`。
- 清理进程时**不要 blanket kill**。`taskkill /F /IM node.exe` 会顺手杀掉你的 n8n、MCP 服务、编辑器插件宿主；按端口或按 PID 精确杀。

---

## 五、别靠猜：11 条规则的一次性体检

上面每一条都能写成脚本，`tools/winenv/win_env_doctor.py` 就是干这个的（纯标准库、零依赖、不联网）：

```bash
python tools/winenv/win_env_doctor.py selftest     # 29 项自检，退出码即结论
python tools/winenv/win_env_doctor.py scan         # 采集本机并出报告（退出码 0/2/3）
python tools/winenv/win_env_doctor.py scan --json --out report.json
python tools/winenv/win_env_doctor.py scan --input samples/live_snapshot.json   # 离线复算
python tools/winenv/win_env_doctor.py explain TMP_DIVERGENCE
```

本机实跑结果（原文照抄）：

```
win_env_doctor 体检报告  ——  PASS 6 / WARN 3 / FAIL 2
------------------------------------------------------------------------
[FAIL] TMP_DIVERGENCE       同一个 /tmp 在两个世界里指向不同目录：原生程序=C:\tmp，shell=C:/Users/<user>/AppData/Local/Temp
       修法: 两边都别用 /tmp：显式写绝对路径，或统一用 TMPDIR 指向的暂存目录
[PASS] MSYS_PATH_IN_ENV     未发现 MSYS 风格路径
[PASS] PYTHONPATH_POLLUTION PYTHONPATH 未被设置
[PASS] PYTHONHOME_SET       PYTHONHOME 未被设置
[WARN] SYS_PATH_FOREIGN     sys.path 混入了其它应用的 site-packages（5 条）
[FAIL] PORT_RESERVED        这些端口绑不了：1241∈[1166,1265], 1300∈[1266,1365]
[WARN] PORT_EPHEMERAL_TRAP  探测端口落在动态端口段（49152+）：50010
[WARN] CONSOLE_ENCODING     原生命令输出是 GBK（stdout=utf-8）
[PASS] SCRATCH_NOT_WRITABLE 暂存目录可写：C:\Users\<user>\AppData\Local\Temp
[PASS] SCRATCH_PATH_DIRTY   暂存路径为 ASCII 且无空格
[PASS] LONG_PATHS_DISABLED  长路径支持已开启
------------------------------------------------------------------------
探测端口: {"1241": "BLOCKED", "1300": "BLOCKED", "7860": "OK", "8300": "OK", "50010": "OK"}
排除段:   [[1066,1165],[1166,1265],[1266,1365],[4265,4364],[4365,4464],[4465,4564],[50000,50059]]
------------------------------------------------------------------------
结论: 有失败项，先修再跑。   exit=3
```

规则表：

| 规则 | 触发条件 | 级别 |
| --- | --- | --- |
| TMP_DIVERGENCE | 原生 `/tmp` 与 shell `/tmp` 解析到不同目录 | FAIL |
| MSYS_PATH_IN_ENV | 环境变量里有 `/c/...` 形态路径 | WARN |
| PYTHONPATH_POLLUTION | `PYTHONPATH` 非空 | FAIL |
| PYTHONHOME_SET | `PYTHONHOME` 被设置 | FAIL |
| SYS_PATH_FOREIGN | `sys.path` 含其它应用的 site-packages | WARN |
| PORT_RESERVED | 探测端口落在排除段内且绑定失败 | FAIL |
| PORT_EPHEMERAL_TRAP | 探测端口 ≥ 49152（动态段） | WARN |
| CONSOLE_ENCODING | 原生命令输出按 UTF-8 解不开 | WARN |
| SCRATCH_NOT_WRITABLE | 暂存目录写入失败 | FAIL |
| SCRATCH_PATH_DIRTY | 暂存路径含空格/非 ASCII | WARN |
| LONG_PATHS_DISABLED | `LongPathsEnabled=0` 或读不到 | WARN |

退出码约定：`0` 全过、`2` 有警告、`3` 有失败。另外一条是给自己定的：**采集不到的项一律判 WARN，不判 PASS**。"没有数据"和"数据正常"是两件事——这条规则是踩出来的，见下一节。

---

## 六、自检工具自己也会假成功

第一版脚本里，shell 侧 `/tmp` 的采集是用 `bash -lc "cd /tmp && pwd -W"` 做的。实跑时这次调用返回了空字符串，而规则写的是"采集不到就跳过"——于是报告里 `TMP_DIVERGENCE` 显示 **PASS**。真相反过来：**恰恰是存在分歧的那台机器上，这项检查静默失效了**。修法是两处：采集函数按 `bash -c` → `bash --noprofile --norc -c` → `sh -c` 依次重试；规则改成"只采到一半 → WARN（不是通过）"。

这类问题值得单独记一笔，因为它和被测系统的失败模式是同一个：**"检查没跑"和"检查通过"共用了一个输出**。任何自检工具的默认值都应该是"未知/不可判"，只有在**主动比对成功**之后才允许输出 PASS。

因此脚本自带 `selftest`：29 项断言覆盖每条规则的触发与不触发（包括"只采到一半必须 WARN""段外的 BLOCKED 不归因于保留段""空快照必须被识别为缺 6 个必需键"），退出码非 0 就说明工具本身坏了，而不是环境干净。

```
$ python tools/winenv/win_env_doctor.py selftest
  ...
selftest: 29/29
```

---

## 七、上线前检查清单（12 项）

1. 临时目录是显式路径（来自配置/`TMPDIR`），脚本里没有裸 `/tmp`。
2. 给原生程序传的路径是 `C:/...` 形态，没有 `/c/...`、没有裸 `/tmp`。
3. 关键产物写入后**同进程读回**校验（路径 + 字节数），失败即报错。
4. 服务端口来自配置，且不在 `netsh` 排除段内（每次部署重新查）。
5. 启动前对目标端口做一次真实 `bind` 探测，失败带排除段清单退出。
6. 启动子进程时 `PYTHONPATH`/`PYTHONHOME` 不存在（用 `env -u` 或 `set X=` 删键）。
7. 入口文件在业务 import 前过滤 `sys.path`。
8. 不依赖 `--reload`；若使用，确认子进程也走了清理逻辑。
9. 读子进程输出用"UTF-8 → GBK 回退 + `errors='replace'`"，并把解码回退记进日志。
10. 常驻服务用 `Start-Process -WindowStyle Hidden` 脱离会话，日志重定向到文件。
11. 杀进程按 PID/端口，禁止 `taskkill /IM node.exe` 这类批量杀。
12. 上线后 `win_env_doctor scan` 留一份快照，出问题时拿快照复算（`scan --input`），别靠回忆。

---

## 八、边界声明

- 本文数据采集环境是 Windows 11（build 28000）+ git-bash + Python 3.13/3.14，排除段、端口可绑性、命令编码会随 Hyper-V/WSL/Docker 状态和系统版本变化，**数字是当次实测结果，不是常量**；结论是"要先探测再依赖"，不是"这几个端口不行"。
- `win_env_doctor.py` 只做只读探测（读环境变量、试绑端口、写一个临时文件再删除），不改注册表、不改系统设置；`LONG_PATHS_DISABLED` 在无管理员权限时读不到注册表，会按 WARN 处理。
- 工具只覆盖环境层，不检查业务逻辑：环境全绿不等于 Agent 产出正确。产出正确性要在业务侧用可外部寻址的证据（文件、URL、数据库行）验证。

复现材料都在仓库里：

- `tools/winenv/win_env_doctor.py` —— 体检工具（含 29 项 selftest）
- `tools/winenv/samples/live_snapshot.json` —— 本文的原始快照（已脱敏），`scan --input` 可直接复算
- `tools/winenv/samples/clean_snapshot.json` —— 干净样例，退出码应为 0
