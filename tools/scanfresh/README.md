# scan_freshness_guard

A scheduled scan that cannot re-fetch its data will happily keep reporting the
last snapshot it managed to read — and exit 0 while doing it. This is a 200-line
zero-dependency detector for that failure mode, plus the real ledger that
triggered it.

## The failure it was written for

A daily A-share pattern scan ran on a local panel of 3,147 per-stock bar files.
It appended one ledger line per session and exited 0 every time.

| reading | value |
|---|---|
| panel files | 3,147 |
| newest bar in the panel | 2026-09-28 |
| newest file write | 2026-09-28 12:10 |
| ledger entries | 16 (2026-09-19 → 2026-10-09) |
| trailing entries byte-identical in pattern counts **and** pick list | **10** (2026-09-28 → 2026-10-09) |
| verification entries whose `target_date` advanced while every per-stock return stayed identical | **2** (target 09-23 → 09-24 → 09-25) |
| scripts in the whole project that mentioned staleness/freshness before this one | **0** |

Three independent stages — panel, scan, verification — all froze, and all three
reported success, because "the job exited 0" was the only thing being measured.

## The three checks

| check | what it reads | trips when |
|---|---|---|
| `panel_stale` | newest bar date vs the expected last trading day (business days) | gap exceeds `--max-panel-gap` (default 1) |
| `trailing_repeat` | fingerprints of consecutive ledger entries (canonical JSON of the fields that define *the answer*) | trailing identical run > `--max-repeat` (default 2) |
| `verify_frozen` | verification ledger: does `target_date` advance while the measured detail stays identical? | any such transition |

Why fingerprints rather than "did the numbers change at all": the fingerprint
deliberately excludes `scan_date`/`run_at`/`generated`, so a timestamp bump is
not mistaken for a new reading, and it is key-order independent, so re-serialising
a dict does not fake a change.

Exit codes: `0` HEALTHY, `2` FROZEN, `3` INSUFFICIENT. `3` exists because "no
input" must never render as "clean" — a guard that returns 0 when handed nothing
is how this class of bug survives a code review.

## Usage

```bash
python scan_freshness_guard.py selftest          # 18 assertions, exit 0/1

python scan_freshness_guard.py check \
  --ledger   samples/real_frozen_history.jsonl \
  --verify   samples/real_verify_frozen.jsonl \
  --today    2026-10-12
```

Run against the real ledger shipped in `samples/`:

```
verdict: FROZEN
  ! trailing identical run of 10 entries (2026-09-28 -> 2026-10-09, fp=4c29e76725)
  ! 2 verification transition(s) advanced target_date without changing any measured detail
```

Add `--panel-dir <dir of per-instrument JSON bar files>` to include the
`panel_stale` reading; add `--json` for machine-readable output to wire into a
scheduler that should *alert* instead of printing green.

## Samples

- `samples/real_frozen_history.jsonl` — the actual 16-entry ledger. Trailing run: 10.
- `samples/real_verify_frozen.jsonl` — the actual verification ledger. Frozen transitions: 2.
- `samples/synthetic_healthy_history.jsonl` — synthetic, so the healthy path is testable. Counts change daily; it must not trip anything.

## Limits (read before trusting a green)

- **Weekends only, not holidays.** `expected_last_trading_day` skips Saturday and
  Sunday. A market holiday on the expected day shows up as a one-day gap, which
  is why the default tolerance is 1 and why the panel check is a threshold, not
  an assertion. Give it a real calendar if you need zero false positives.
- **It detects frozen *output*, not a broken *fetch*.** A source that returns
  slightly different garbage every day passes all three checks. Freshness is a
  necessary condition, not a correctness one.
- **Two identical days are normal.** The default `--max-repeat 2` deliberately
  tolerates them; a quiet market can produce identical bucket counts by chance.
  Tune it against your own hit rate rather than setting it to 1.
- **Fingerprint scope is a judgement call.** Only the fields listed in
  `SNAP_KEYS` (plus a whole-record fallback) count as "the answer". Add fields
  when your ledger carries more state that should move.
