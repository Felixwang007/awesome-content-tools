#!/usr/bin/env python3
"""naive_precision.py — measure a rule's precision on the corpus you point it at.

The rule being measured is deliberately naive (the shape a first draft ships):

    "a line whose first five whitespace-separated tokens are mostly numeric-ish
     is a cron expression"

It reports how many hits actually parse as a 5-field schedule, and classifies
every false positive so you learn *why* it broke, not just that it broke.

Usage:
    python naive_precision.py probe <dir> [--glob '*.txt'] [--quiet]
    python naive_precision.py selftest

Exit codes:
    0 = probe ran, precision printed
    2 = probe ran, precision below --min-precision (default 1.0)
    3 = NO_INPUT — nothing was scanned. "No input" is not "clean input".

Stdlib only. No network. Safe to run on any readable directory.
"""

import argparse
import fnmatch
import os
import re
import sys
import tempfile

TOKEN = re.compile(r"^\s*(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(\S.*)$")
FIELD = re.compile(r"^(\*|[\d*/,\-]+)$")


def parse_as_schedule(tokens):
    """True only if all five tokens are valid cron field syntax."""
    return len(tokens) == 5 and all(FIELD.match(t) for t in tokens)


def classify(line, expr):
    """Why did this hit fail? Boundary/lexing errors are not dialect errors."""
    head = line.split(expr)[0]
    if re.search(r"[\"']", expr) or re.search(r"[:=]", head):
        return "embedded_in_key_value"
    if re.search(r"[A-Za-z]{4,}", expr):
        return "english_prose"
    return "other"


def probe(root, glob="*", quiet=False):
    files = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fnmatch.fnmatch(fn, glob):
                files.append(os.path.join(dirpath, fn))
    files.sort()

    hits = real = 0
    causes = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = TOKEN.match(s)
            if not m:
                continue
            expr = m.group(1)
            tokens = expr.split()
            numeric = sum(1 for t in tokens if re.fullmatch(r"[\d*/,\-]+", t))
            if numeric < 3:
                continue
            hits += 1
            if parse_as_schedule(tokens):
                real += 1
            else:
                k = classify(s, expr)
                causes[k] = causes.get(k, 0) + 1

    precision = (real / hits) if hits else 0.0
    report = {
        "root": root,
        "glob": glob,
        "files_scanned": len(files),
        "naive_hits": hits,
        "parses_as_schedule": real,
        "false_positives": hits - real,
        "precision": round(precision, 3),
        "fp_causes": causes,
    }
    if not quiet:
        print("files_scanned      %d" % report["files_scanned"])
        print("naive_hits         %d" % report["naive_hits"])
        print("parses_as_schedule %d" % report["parses_as_schedule"])
        print("false_positives    %d" % report["false_positives"])
        print("precision          %.3f" % report["precision"])
        if causes:
            print("\nfalse positives by cause:")
            for k in sorted(causes, key=lambda x: -causes[x]):
                print("  %-22s %d" % (k, causes[k]))
    return report


def selftest():
    ok = fail = 0

    def chk(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("PASS  %s" % label)
        else:
            fail += 1
            print("FAIL  %s" % label)

    tmp = tempfile.mkdtemp(prefix="calib_")
    with open(os.path.join(tmp, "cron.txt"), "w", encoding="utf-8") as f:
        f.write("*/5 * * * * /usr/bin/backup.sh\n")
        f.write("0 3 * * * /usr/bin/rotate.sh >>/var/log/r.log 2>&1\n")
    with open(os.path.join(tmp, "prose.txt"), "w", encoding="utf-8") as f:
        f.write('  "expr": "15 8 * * *",\n')          # boundary error
        f.write("* [Petals](https://petals.dev/) - Self-Hosted 1 / 2\n")  # prose

    r = probe(tmp, "*.txt", quiet=True)
    chk(r["files_scanned"] == 2, "扫到 2 个文件")
    chk(r["naive_hits"] >= 2, "朴素规则至少命中 2 条")
    chk(r["parses_as_schedule"] == 2, "2 条真 cron 被认成调度")
    chk(r["false_positives"] >= 2, "有假阳性被记入")
    chk(r["precision"] < 1.0, "精度小于 1（说明规则确实会错）")
    chk("embedded_in_key_value" in r["fp_causes"], "假阳性归因含 embedded_in_key_value")

    # empty dir must be NO_INPUT, not a pass
    empty = tempfile.mkdtemp(prefix="calib_empty_")
    r2 = probe(empty, "*", quiet=True)
    chk(r2["files_scanned"] == 0 and r2["naive_hits"] == 0, "空目录 hits=0")
    chk(parse_as_schedule("1 2 3 4 5".split()), "纯数字五字段=合法调度")
    chk(not parse_as_schedule('"15 8 * * *",'.split()), "带引号残留=不合法")
    chk(not parse_as_schedule("a b c d".split()), "四字段=不合法")

    print("\nselftest: %d/%d 通过" % (ok, ok + fail))
    return 0 if fail == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="朴素规则精度探针")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe")
    p.add_argument("dir")
    p.add_argument("--glob", default="*")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--min-precision", type=float, default=1.0)
    sub.add_parser("selftest")

    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd != "probe":
        ap.print_help()
        return 0

    if not os.path.isdir(args.dir):
        print("路径不存在：%s" % args.dir)
        print("NO_INPUT —— 没输入不等于输入干净（退出码 3）")
        return 3

    r = probe(args.dir, args.glob, args.quiet)
    if r["files_scanned"] == 0:
        print("该路径下没有匹配 %s 的文件。" % args.glob)
        print("NO_INPUT —— 没输入不等于输入干净（退出码 3）")
        return 3
    if r["precision"] < args.min_precision:
        print("\n精度 %.3f 低于门槛 %.2f（退出码 2）" % (r["precision"], args.min_precision))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
