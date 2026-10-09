# tools/winenv —— Windows Agent 环境自检

`win_env_doctor.py`：纯标准库、零依赖、只读探测。覆盖 Windows 上跑 AI Agent 自动化最高频的三类"没有报错的失败"：路径分歧、端口保留段、环境变量继承。

```bash
python win_env_doctor.py selftest                                  # 29 项自检，退出码即结论
python win_env_doctor.py scan                                      # 采集本机并出报告
python win_env_doctor.py scan --json --out report.json             # 机器可读
python win_env_doctor.py scan --input samples/live_snapshot.json   # 离线复算（不需要 Windows）
python win_env_doctor.py explain PORT_RESERVED                     # 单条规则解释 + 修法
```

退出码：`0` 全部通过 · `2` 有警告 · `3` 有失败或快照不完整。

## 为什么它不是又一个"打印一堆信息"的脚本

- **采集不到就判 WARN，绝不判 PASS。** "检查没跑"和"检查通过"不能共用同一个输出。
- **快照与判定分离。** `scan --out` 存下快照，判定逻辑是纯函数，可离线复算和回归。
- **带 29 项 selftest。** 每条规则都测了触发与不触发两向，包括"只采到一半必须 WARN""段外的 BLOCKED 不归因于保留段""空快照必须被认出缺 6 个必需键"。

## 样本

| 文件 | 用途 | 期望退出码 |
| --- | --- | --- |
| `samples/live_snapshot.json` | 真实机器快照（已脱敏），含 /tmp 分歧与保留端口 | 3 |
| `samples/clean_snapshot.json` | 干净样例 | 0 |

## 规则表

| 规则 | 触发条件 | 级别 |
| --- | --- | --- |
| TMP_DIVERGENCE | 原生 `/tmp` 与 shell `/tmp` 解析到不同目录；采集不全也算 | FAIL / WARN |
| MSYS_PATH_IN_ENV | 环境变量里有 `/c/...` 形态路径 | WARN |
| PYTHONPATH_POLLUTION | `PYTHONPATH` 非空 | FAIL |
| PYTHONHOME_SET | `PYTHONHOME` 被设置 | FAIL |
| SYS_PATH_FOREIGN | `sys.path` 含其它应用的 site-packages | WARN |
| PORT_RESERVED | 探测端口落在 `netsh` 排除段内且绑定失败 | FAIL |
| PORT_EPHEMERAL_TRAP | 探测端口 ≥ 49152（动态段） | WARN |
| CONSOLE_ENCODING | 原生命令输出按 UTF-8 解不开 | WARN |
| SCRATCH_NOT_WRITABLE | 暂存目录写入失败 | FAIL |
| SCRATCH_PATH_DIRTY | 暂存路径含空格 / 非 ASCII | WARN |
| LONG_PATHS_DISABLED | `LongPathsEnabled=0` 或读不到 | WARN |

## 边界

只读探测：读环境变量、试绑端口、在暂存目录写一个临时文件随即删除。不改注册表、不改系统设置。不检查业务逻辑——环境全绿不代表 Agent 产出正确，产出要在业务侧用可外部寻址的证据验证。

配套文章：[Windows 上跑 AI Agent 自动化：同一个 /tmp 有两个、端口被系统留着、PYTHONPATH 会劫持 import](https://felixwang007.github.io/awesome-content-tools/articles/windows-agent-automation-pitfalls-guide.html)
