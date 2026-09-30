#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tape.py — record/replay tape for agent tool calls. Pure stdlib."""
import hashlib, json, os, time

class TapeError(RuntimeError):
    pass

class TapeMiss(TapeError):
    """回放时请求了一个磁带里没有的调用（严格模式）。"""

class TapeDrift(TapeError):
    """同一序号位置上，参数和录制时不一致 —— 行为漂移。"""

def canon(obj):
    """稳定规范化：dict 按键排序、字符串去首尾空白、丢 None、浮点统一。"""
    if isinstance(obj, dict):
        return {k: canon(v) for k, v in sorted(obj.items()) if v is not None}
    if isinstance(obj, list):
        return [canon(v) for v in obj]
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, float):
        return round(obj, 6)
    return obj

def brief(obj, limit=32):
    """只为打印：长字符串截断，避免日志被正文淹没。"""
    if isinstance(obj, dict):
        return {k: brief(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [brief(v, limit) for v in obj]
    if isinstance(obj, str) and len(obj) > limit:
        return obj[:limit] + f"…(+{len(obj) - limit}字)"
    return obj

def call_key(tool, args):
    payload = json.dumps(canon(args), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return tool + ":" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

SECRET_KEYS = ("token", "password", "api_key", "authorization", "cookie", "secret")

def redact(args):
    if isinstance(args, dict):
        return {k: ("<redacted>" if k.lower() in SECRET_KEYS else redact(v)) for k, v in args.items()}
    if isinstance(args, list):
        return [redact(v) for v in args]
    return args

class Tape:
    def __init__(self, path, mode="record", strict=True):
        assert mode in ("record", "replay", "replay+record")
        self.path, self.mode, self.strict = path, mode, strict
        self.entries, self.seq = [], 0
        self.hits = self.misses = self.live_calls = self.side_effects = 0
        self.drift = []
        if mode.startswith("replay") and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.entries = [json.loads(l) for l in f if l.strip()]
        if mode == "record":
            open(path, "w", encoding="utf-8").close()

    def _append(self, entry):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def _by_key(self, key):
        for e in self.entries:
            if e["key"] == key and not e.get("_used"):
                e["_used"] = True
                return e
        return None

    def _peek(self, key):
        """查找但不消费（override 用）。"""
        for e in self.entries:
            if e["key"] == key:
                return e
        return None

    def _at_seq(self, seq):
        for e in self.entries:
            if e["seq"] == seq:
                return e
        return None

    def call(self, tool, args, fn, side_effect=False, clock=None):
        """fn(**args) 只在录制（或 replay+record 落空）时被真正执行。"""
        key = call_key(tool, args)
        self.seq += 1
        t0 = time.perf_counter()

        if self.mode == "record":
            result = fn(**args)
            self.live_calls += 1
            self.side_effects += 1 if side_effect else 0
            self._append({"seq": self.seq, "tool": tool, "key": key, "args": redact(canon(args)),
                          "result": canon(result), "ms": round((time.perf_counter() - t0) * 1000, 2),
                          "side_effect": bool(side_effect),
                          "clock": clock() if clock else None})
            return result

        hit = self._by_key(key)
        if hit:
            self.hits += 1
            return hit["result"]

        recorded = self._at_seq(self.seq)
        if recorded is not None and recorded["key"] != key:
            self.drift.append({"seq": self.seq, "tool": tool,
                               "recorded_args": brief(recorded["args"]), "replayed_args": brief(redact(canon(args))),
                               "recorded_key": recorded["key"], "replayed_key": key})
            if self.strict:
                raise TapeDrift(
                    f"step {self.seq}: {tool} 参数漂移\n"
                    f"  recorded {recorded['key']} {json.dumps(brief(recorded['args']), ensure_ascii=False)}\n"
                    f"  replayed {key} {json.dumps(brief(redact(canon(args))), ensure_ascii=False)}")
        self.misses += 1
        if self.mode == "replay+record":
            result = fn(**args)
            self.live_calls += 1
            self.side_effects += 1 if side_effect else 0
            self._append({"seq": self.seq, "tool": tool, "key": key, "args": redact(canon(args)),
                          "result": canon(result), "ms": round((time.perf_counter() - t0) * 1000, 2),
                          "side_effect": bool(side_effect), "recorded_on_miss": True,
                          "clock": clock() if clock else None})
            return result
        raise TapeMiss(f"step {self.seq}: 磁带里没有 {tool} 的这条调用（{key}）")

    def override(self, tool, args, result):
        """注入式回放：把某次调用的结果换成指定值（复现错误分支用）。"""
        key = call_key(tool, args)
        entry = self._peek(key)
        if entry:
            entry["result"] = canon(result)
        else:
            raise TapeMiss(f"override 失败：磁带里没有 {key}")

    def stats(self):
        return {"mode": self.mode, "steps": self.seq, "tape_hits": self.hits, "tape_misses": self.misses,
                "live_tool_calls": self.live_calls, "real_side_effects": self.side_effects,
                "drift_events": len(self.drift)}
