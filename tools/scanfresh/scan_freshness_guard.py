#!/usr/bin/env python3
"""scan_freshness_guard — decide whether a scheduled scan is reporting reality
or re-reporting its last snapshot.

Why this exists
---------------
A daily pattern-scan job on a local A-share panel wrote a ledger entry every
day and exited 0. For ten consecutive sessions (2026-09-28 -> 2026-10-09) the
entries were byte-identical: same pattern counts, same empty pick list, while
the underlying panel's newest bar was 2026-09-28 (14 days stale by the time
anyone looked). Nothing alarmed, because "the job succeeded" was the only thing
anyone measured. Green is not evidence; a *changing* reading is.

Three checks, each one cheap, each one a thing a success flag cannot tell you:

1. panel_stale      - newest bar in the data panel vs the expected last trading
                      day. Gap is measured in business days; exchange holidays
                      can make a 1-2 day gap benign, which is why this is a
                      threshold and not an assertion.
2. trailing_repeat  - consecutive ledger entries whose fingerprint (selected
                      fields, canonicalised) is identical. Two identical days
                      is a coincidence; N identical days means nothing was read.
3. verify_frozen    - in a verification ledger, the target date advances while
                      the per-item detail is identical. That is a price refresh
                      that never happened, dressed as a new measurement.

Exit codes: 0 HEALTHY, 2 FROZEN (a hard check tripped), 3 INSUFFICIENT (no
input / not enough entries to judge). "No input" is never "clean".

Usage
-----
  python scan_freshness_guard.py selftest
  python scan_freshness_guard.py check --ledger history.jsonl [--verify v.jsonl] \
      [--panel-dir ./panel] [--today 2026-10-12] [--max-repeat 2] [--json]

Ledger format (one JSON object per line, any extra keys ignored):
  {"scan_date": "2026-10-09", "dist": {...}, "picks": [...]}
Verification ledger format:
  {"verify_date": "...", "target_date": "...", "detail": [{"code": "...", ...}]}
Panel format: one JSON file per instrument, each a list of {"date": "...", ...}.

Only the standard library is used.
"""
import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import sys

# fields that define "the same answer" for a scan snapshot; add keys here if your
# ledger carries more state that should count as a changed reading
SNAP_KEYS = ('dist', 'picks', 'rebound', 'breaks')


def fp(obj, keys=SNAP_KEYS):
    picked = {}
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            picked[k] = obj[k]
    if not picked:                      # fall back to the whole record
        picked = {k: v for k, v in sorted(obj.items()) if k not in
                  ('scan_date', 'verify_date', 'target_date', 'generated', 'run_at', 'ts')}
    blob = json.dumps(picked, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:10]


def load_jsonl(path):
    rows = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def business_days_between(a, b):
    """Business days strictly between two ISO dates (b later than a). Holidays
    are NOT modelled -- the caller decides what gap is tolerable."""
    d0 = dt.date.fromisoformat(a)
    d1 = dt.date.fromisoformat(b)
    if d1 <= d0:
        return 0
    n, cur = 0, d0
    while cur < d1:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def expected_last_trading_day(today, after_close=True):
    """Latest weekday that should already have a settled bar. Weekend-aware only."""
    d = dt.date.fromisoformat(today)
    if d.weekday() >= 5:
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d.isoformat()
    if not after_close:
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def check_panel(panel_dir, today, max_gap_days=1):
    files = sorted(glob.glob(os.path.join(panel_dir, '*.json')))
    if not files:
        return None
    last = collections.Counter()
    for p in files:
        try:
            rows = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        if isinstance(rows, list) and rows and isinstance(rows[-1], dict) and 'date' in rows[-1]:
            last[rows[-1]['date']] += 1
    if not last:
        return None
    newest = max(last)
    exp = expected_last_trading_day(today)
    gap = business_days_between(newest, exp)
    return {
        'files': len(files),
        'newest_bar': newest,
        'names_on_newest_bar': last[newest],
        'expected_last_trading_day': exp,
        'business_day_gap': gap,
        'tripped': gap > max_gap_days,
    }


def check_ledger(rows, max_repeat):
    if len(rows) < 2:
        return None
    fps = [fp(r) for r in rows]
    run_start, runs = 0, []
    for i in range(1, len(fps) + 1):
        if i == len(fps) or fps[i] != fps[run_start]:
            runs.append({'from': rows[run_start].get('scan_date', '?'),
                         'to': rows[i - 1].get('scan_date', '?'),
                         'days': i - run_start, 'fingerprint': fps[run_start]})
            run_start = i
    trailing = runs[-1]
    return {'entries': len(rows),
            'first': rows[0].get('scan_date', '?'),
            'last': rows[-1].get('scan_date', '?'),
            'runs': runs,
            'trailing_run_days': trailing['days'],
            'max_repeat_allowed': max_repeat,
            'trailing_fingerprint': trailing['fingerprint'],
            'tripped': trailing['days'] > max_repeat}


def check_verify(rows):
    """target_date advances but the measured detail does not change."""
    if len(rows) < 2:
        return None
    pairs = []
    hits = 0
    for prev, cur in zip(rows, rows[1:]):
        same_detail = fp(prev, ('detail',)) == fp(cur, ('detail',))
        advanced = prev.get('target_date') != cur.get('target_date')
        pairs.append({'from_target': prev.get('target_date'),
                      'to_target': cur.get('target_date'),
                      'detail_changed': not same_detail,
                      'has_next': True})
        if same_detail and advanced:
            hits += 1
    return {'entries': len(rows), 'frozen_transitions': hits,
            'pairs': pairs, 'tripped': hits > 0}


def run_check(args):
    evidence, reasons = [], []

    panel = check_panel(args.panel_dir, args.today, args.max_panel_gap) if args.panel_dir else None
    if panel:
        evidence.append({'check': 'panel_stale', **panel})
        if panel['tripped']:
            reasons.append(f"panel newest bar {panel['newest_bar']} is {panel['business_day_gap']} "
                           f"business days behind {panel['expected_last_trading_day']}")

    ledger = None
    if args.ledger:
        rows = load_jsonl(args.ledger)
        ledger = check_ledger(rows, args.max_repeat)
        if ledger is None:
            evidence.append({'check': 'trailing_repeat', 'entries': len(rows), 'tripped': False,
                             'note': 'fewer than 2 entries -- cannot judge'})
        else:
            evidence.append({'check': 'trailing_repeat', **{k: v for k, v in ledger.items() if k != 'runs'},
                             'runs': ledger['runs']})
            if ledger['tripped']:
                reasons.append(f"trailing identical run of {ledger['trailing_run_days']} entries "
                               f"({ledger['runs'][-1]['from']} -> {ledger['runs'][-1]['to']}, "
                               f"fp={ledger['trailing_fingerprint']})")

    verify = None
    if args.verify:
        vrows = load_jsonl(args.verify)
        verify = check_verify(vrows)
        if verify is None:
            evidence.append({'check': 'verify_frozen', 'entries': len(vrows), 'tripped': False,
                             'note': 'fewer than 2 entries -- cannot judge'})
        else:
            evidence.append({'check': 'verify_frozen', **verify})
            if verify['tripped']:
                reasons.append(f"{verify['frozen_transitions']} verification transition(s) advanced "
                               f"target_date without changing any measured detail")

    if panel is None and ledger is None and verify is None:
        verdict = 'INSUFFICIENT'
        reasons = ['nothing to judge: supply --panel-dir, --ledger or --verify']
        code = 3
    elif reasons:
        verdict, code = 'FROZEN', 2
    else:
        verdict, code = 'HEALTHY', 0

    out = {'verdict': verdict, 'reasons': reasons, 'evidence': evidence,
           'today': args.today, 'exit_code': code}
    if args.json_out:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"verdict: {verdict}")
        for r in reasons:
            print(f"  ! {r}")
        for e in evidence:
            tripped = e.get('tripped')
            mark = 'TRIP' if tripped else ('ok  ' if tripped is False else 'n/a ')
            print(f"  [{mark}] {e['check']}: " +
                  json.dumps({k: v for k, v in e.items() if k not in ('check', 'tripped')},
                             ensure_ascii=False)[:220])
    return code


# --------------------------------------------------------------------------
# selftest -- fixtures are synthetic on purpose: they assert the contract
# (what SHOULD be flagged), not the state of anybody's real ledger.
# --------------------------------------------------------------------------
def _write(path, rows):
    with open(path, 'w', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')


def _selftest():
    import contextlib
    import io
    import shutil
    import tempfile
    checks = []
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())  # noqa: E731
    _rc = globals()['run_check']

    def run_check(args):                 # quiet wrapper so the selftest prints only results
        with quiet():
            return _rc(args)

    def ok(name, cond, got=''):
        checks.append((name, bool(cond), got))

    tmp = tempfile.mkdtemp(prefix='sfg_selftest_')
    try:
        # 1. healthy ledger: distribution changes every day
        healthy = os.path.join(tmp, 'healthy.jsonl')
        _write(healthy, [{'scan_date': f'2026-10-{d:02d}',
                          'dist': {'A': 10 + d, 'B': 20 - d}, 'picks': [{'code': str(d)}]}
                         for d in (5, 6, 7, 8, 9)])
        rc = run_check(argparse.Namespace(today='2026-10-12', panel_dir=None, ledger=healthy,
                                          verify=None, max_repeat=2, max_panel_gap=1, json_out=True,
                                          ))
        ok('healthy ledger -> exit 0', rc == 0, rc)

        # 2. frozen ledger: ten identical entries
        frozen = os.path.join(tmp, 'frozen.jsonl')
        _write(frozen, [{'scan_date': f'2026-09-{d:02d}',
                         'dist': {'A': 402, 'B': 753}, 'picks': []}
                        for d in (28, 29, 30)] +
                       [{'scan_date': f'2026-10-{d:02d}', 'dist': {'A': 402, 'B': 753}, 'picks': []}
                        for d in (1, 2, 5, 6, 7, 8, 9)])
        rc = run_check(argparse.Namespace(today='2026-10-12', panel_dir=None, ledger=frozen,
                                          verify=None, max_repeat=2, max_panel_gap=1, json_out=True))
        ok('frozen ledger -> exit 2', rc == 2, rc)
        ledger_check = check_ledger(load_jsonl(frozen), 2)
        ok('frozen ledger -> trailing run == 10', ledger_check['trailing_run_days'] == 10,
           ledger_check['trailing_run_days'])
        ok('frozen ledger -> tripped', ledger_check['tripped'] is True)

        # 3. a two-day repeat is a coincidence, not a freeze
        pair = os.path.join(tmp, 'pair.jsonl')
        _write(pair, [{'scan_date': '2026-10-08', 'dist': {'A': 1}, 'picks': []},
                      {'scan_date': '2026-10-09', 'dist': {'A': 1}, 'picks': []}])
        rc = run_check(argparse.Namespace(today='2026-10-12', panel_dir=None, ledger=pair,
                                          verify=None, max_repeat=2, max_panel_gap=1, json_out=True))
        ok('two identical entries -> not frozen', rc == 0, rc)

        # 4. verification ledger whose dates advance but detail never changes
        vfrozen = os.path.join(tmp, 'verify_frozen.jsonl')
        same = [{'code': '603065', 'ret': -0.28}, {'code': '002028', 'ret': -12.71}]
        _write(vfrozen, [{'verify_date': f'2026-10-0{d}', 'target_date': f'2026-09-2{d + 2}',
                          'avg': -0.69, 'detail': same} for d in (1, 2, 3)])
        rc = run_check(argparse.Namespace(today='2026-10-12', panel_dir=None, ledger=None,
                                          verify=vfrozen, max_repeat=2, max_panel_gap=1, json_out=True))
        ok('verify freeze -> exit 2', rc == 2, rc)
        vc = check_verify(load_jsonl(vfrozen))
        ok('verify freeze -> 2 frozen transitions', vc['frozen_transitions'] == 2, vc['frozen_transitions'])

        # 5. honest verification ledger (same target dates, different detail) is NOT flagged
        vok = os.path.join(tmp, 'verify_ok.jsonl')
        _write(vok, [{'verify_date': '2026-10-01', 'target_date': '2026-09-24',
                      'detail': [{'code': 'A', 'ret': 1.0}]},
                     {'verify_date': '2026-10-02', 'target_date': '2026-09-25',
                      'detail': [{'code': 'A', 'ret': 2.0}]}])
        vc2 = check_verify(load_jsonl(vok))
        ok('detail changes -> not frozen', vc2['frozen_transitions'] == 0, vc2['frozen_transitions'])

        # 6. stale panel trips, fresh panel does not
        stale = os.path.join(tmp, 'stale_panel')
        fresh = os.path.join(tmp, 'fresh_panel')
        os.makedirs(stale, exist_ok=True)
        os.makedirs(fresh, exist_ok=True)
        for i in range(3):
            json.dump([{'date': '2026-09-28', 'close': 1.0}], open(os.path.join(stale, f'{i}.json'), 'w'))
            json.dump([{'date': '2026-10-09', 'close': 1.0}], open(os.path.join(fresh, f'{i}.json'), 'w'))
        sp = check_panel(stale, '2026-10-12')
        fpp = check_panel(fresh, '2026-10-12')
        ok('stale panel tripped', sp['tripped'] is True, sp)
        ok('fresh panel not tripped', fpp['tripped'] is False, fpp)
        ok('stale gap = 10 business days (2026-09-28 -> 2026-10-12)',
           sp['business_day_gap'] == 10, sp['business_day_gap'])

        # 7. no input at all is INSUFFICIENT (exit 3), never HEALTHY
        rc = run_check(argparse.Namespace(today='2026-10-12', panel_dir=None, ledger=None,
                                          verify=None, max_repeat=2, max_panel_gap=1, json_out=True))
        ok('zero input -> exit 3 (not 0)', rc == 3, rc)

        # 8. a single-entry ledger cannot be judged -> exit 3
        one = os.path.join(tmp, 'one.jsonl')
        _write(one, [{'scan_date': '2026-10-09', 'dist': {'A': 1}, 'picks': []}])
        rc = run_check(argparse.Namespace(today='2026-10-12', panel_dir=None, ledger=one,
                                          verify=None, max_repeat=2, max_panel_gap=1, json_out=True))
        ok('single entry -> exit 3', rc == 3, rc)

        # 9. weekend expectation lands on the Friday
        ok('weekend expected day = Friday',
           expected_last_trading_day('2026-10-11') == '2026-10-09',
           expected_last_trading_day('2026-10-11'))

        # 10. verification ledger with a changed pick list is not a freeze
        vrot = os.path.join(tmp, 'verify_rotate.jsonl')
        _write(vrot, [{'verify_date': '2026-10-01', 'target_date': '2026-09-24',
                       'detail': [{'code': 'A', 'ret': 1.0}, {'code': 'B', 'ret': 0.5}]},
                      {'verify_date': '2026-10-02', 'target_date': '2026-09-25',
                       'detail': [{'code': 'A', 'ret': 1.0}]}])
        ok('pick-list change counts as a new reading',
           check_verify(load_jsonl(vrot))['frozen_transitions'] == 0)

        # 11. ordering independence: the same snapshot different keys order -> same fp
        ok('fingerprint is key-order independent',
           fp({'dist': {'A': 1, 'B': 2}, 'picks': []}) == fp({'picks': [], 'dist': {'B': 2, 'A': 1}}))
        # 12. scan_date differences alone must NOT count as a change
        ok('scan_date is not part of the fingerprint',
           fp({'scan_date': '2026-10-01', 'dist': {'A': 1}, 'picks': []}) ==
           fp({'scan_date': '2026-10-09', 'dist': {'A': 1}, 'picks': []}))
        # 13. business day maths skips the weekend
        ok('Mon -> next Mon = 5 business days',
           business_days_between('2026-10-05', '2026-10-12') == 5,
           business_days_between('2026-10-05', '2026-10-12'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in checks if not c[1]]
    for name, passed, got in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + ('' if passed else f'   got={got}'))
    print(f'selftest: {len(checks) - len(bad)}/{len(checks)} passed')
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('selftest')
    c = sub.add_parser('check')
    c.add_argument('--ledger', help='scan ledger JSONL (one snapshot per line)')
    c.add_argument('--verify', help='verification ledger JSONL (target_date + detail)')
    c.add_argument('--panel-dir', help='directory of per-instrument JSON bar files')
    c.add_argument('--today', default=dt.date.today().isoformat())
    c.add_argument('--max-repeat', type=int, default=2,
                   help='identical trailing entries tolerated before FROZEN (default 2)')
    c.add_argument('--max-panel-gap', type=int, default=1,
                   help='business days the panel may lag (default 1)')
    c.add_argument('--json', dest='json_out', action='store_true')
    args = ap.parse_args(argv)

    if args.cmd == 'selftest':
        return _selftest()
    if args.cmd == 'check':
        return run_check(args)
    ap.print_help()
    return 3


if __name__ == '__main__':
    sys.exit(main())
