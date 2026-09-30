#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canon_check.py — 为什么磁带必须做参数规范化：四个真实反例（同一语义、不同指纹）。"""
import json, hashlib
from tape import canon, call_key

def naive_key(tool, args):   # 不规范化、不排序键的版本
    return tool + ":" + hashlib.sha256(json.dumps(args, ensure_ascii=False).encode()).hexdigest()[:12]

cases = [
    ("dict 键序不同（语义相同）", {"query": "agent", "top_k": 3}, {"top_k": 3, "query": "agent"}),
    ("字符串首尾空白", {"query": " agent"}, {"query": "agent"}),
    ("数字 vs 字符串", {"top_k": 3}, {"top_k": "3"}),
    ("布尔 vs 整数", {"cache": True}, {"cache": 1}),
    ("全角空格 U+3000", {"query": "agent　"}, {"query": "agent"}),
]

print(f"{'用例':<26} {'朴素指纹一致?':<14} {'canon指纹一致?':<14} {'canon 后的值'}")
for name, a, b in cases:
    same_naive = naive_key("t", a) == naive_key("t", b)
    same_canon = call_key("t", a) == call_key("t", b)
    print(f"{name:<24} {str(same_naive):<16} {str(same_canon):<16} {json.dumps(canon(a), ensure_ascii=False)} vs {json.dumps(canon(b), ensure_ascii=False)}")

print()
print("朴素指纹（键序不同 → 不同 hash）:")
print("  ", naive_key("t", {"query": "agent", "top_k": 3}))
print("  ", naive_key("t", {"top_k": 3, "query": "agent"}))
print("规范化指纹（键序不同 → 同一 hash）:")
print("  ", call_key("t", {"query": "agent", "top_k": 3}))
print("  ", call_key("t", {"top_k": 3, "query": "agent"}))
print()
print("意外发现（测之前我以为要去掉，实测不用）: U+3000 已被 str.strip() 处理，",
      repr("agent　".strip()), "长度", len("agent　".strip()))
print("但类型漂移不会被规整：3 vs \"3\"、True vs 1 仍算两次不同的调用（这是故意的）")
