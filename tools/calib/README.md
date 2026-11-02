# tools/calib — 精度探针（校准用）

一个规则在有罪证据之外，还需要一个**无罪读数**：它在真实语料上到底误报多少。

`naive_precision.py` 就是干这个的：它实现一条「第一版常写的那样」的朴素规则

> 一行里前五个空白分隔的 token 大多像数字 → 这是一条 cron 表达式

然后把它指向你给的目录，报出命中数、其中真的能解析成 5 字段调度的条数、精度，以及每条误报的归因
（`embedded_in_key_value` = 从引号/JSON/YAML 里切错了边界；`english_prose` = 英文散文）。

```bash
python naive_precision.py selftest                 # 10 项自检
python naive_precision.py probe <dir> [--glob '*.md'] [--min-precision 0.9]
```

退出码：`0` 跑完且达标 / `2` 精度低于门槛 / `3` NO_INPUT（扫到 0 个文件 —— 没输入不是输入干净）。

## 一次真实读数（2026-11-02）

| 语料 | 文件 | 朴素命中 | 真调度 | 精度 | 误报归因 |
|---|---|---|---|---|---|
| 17 文件 scheduler 语料（crontab/GHA/systemd/JSON） | 17 | 97 | 25 | **0.258** | embedded_in_key_value ×72 |
| 同一规则指向 markdown 文档 | 182 | 328 | 0 | **0.000** | english_prose ×327 / other ×1 |

结论写在文章里：**精度是「规则 × 语料」的属性，不是规则的属性**；只在自己写的 fixture 上测出来的精度不是测量，是彩排。
