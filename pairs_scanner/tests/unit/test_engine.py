"""
tests/unit/test_engine.py — Tests for engine/ layer (monitor, auto_entry, scanner).

33 tests covering:
  - build_exit_params (6 tests)
  - run_monitor_tick (8 tests)
  - validate_pending (10 tests)
  - check_pending_ttl (2 tests)
  - load_filters_state (2 tests)
  - scanner helpers (5 tests)
"""

import sys, os, time, tempfile, json

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _root)

from pairs_scanner.core.position_manager import ExitParams
from pairs_scanner.engine.monitor import build_exit_params, run_monitor_tick, build_trail_patch, TRAIL_KEYS
from pairs_scanner.engine.auto_entry import validate_pending, check_pending_ttl, load_filters_state
from pairs_scanner.engine.scanner import filter_coins, calc_scan_bars, build_pair_list, should_skip_pair, estimate_scan_time

passed = 0
failed = 0
errors = []

def test(name, condition):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        errors.append(name)
        print(f"  FAIL: {name}")

t0 = time.time()

# ═══════════════════════════════════════════════════════
# ENGINE/MONITOR
# ═══════════════════════════════════════════════════════

print("engine/build_exit_params...")

def _mock_cfg(section, key, default=None):
    _d = {
        ('monitor', 'auto_tp_pct'): 2.5,
        ('monitor', 'auto_sl_pct'): -3.5,
        ('monitor', 'trailing_enabled'): True,
        ('monitor', 'trailing_activate_pct'): 1.8,
        ('monitor', 'trailing_drawdown_pct'): 0.9,
        ('monitor', 'max_hold_hours'): 12,
        ('strategy', 'max_hold_hours'): 12,
    }
    return _d.get((section, key), default)

_pos = {'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG'}
params = build_exit_params(_pos, cfg_fn=_mock_cfg)
test("cfg tp", params.default_tp_pct == 2.5)
test("cfg sl", params.default_sl_pct == -3.5)
test("cfg trailing", params.trailing_activate_pct == 1.8)
test("cfg max_hold", params.max_hold_hours == 12.0)

# Per-pair override
_pos2 = {'coin1': 'SOL', 'coin2': 'BTC', 'direction': 'SHORT', 'pair_tp_pct': 3.0, 'pair_sl_pct': -2.0}
params2 = build_exit_params(_pos2, cfg_fn=_mock_cfg)
test("pair_tp override", params2.default_tp_pct == 3.0)
test("pair_sl override", params2.default_sl_pct == -2.0)

print("engine/run_monitor_tick...")

def _fresh_pos(**kw):
    base = {'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG',
            'entry_z': -2.5, 'best_pnl_during_trade': 0.0}
    base.update(kw)
    return base

_p = ExitParams(default_sl_pct=-3.0)
pos = _fresh_pos()
result = run_monitor_tick(pos, {'pnl_pct': -4.0, 'z_static': -1, 'best_pnl': 0, 'hours_in': 1}, params=_p)
test("tick SL", result['should_close'] and 'AUTO_SL' in result['reason'])

pos2 = _fresh_pos()
result2 = run_monitor_tick(pos2, {'pnl_pct': 0.5, 'z_static': -1.5, 'best_pnl': 0.5, 'hours_in': 1}, params=_p)
test("tick no close", not result2['should_close'])

pos3 = _fresh_pos(best_pnl_during_trade=0.3)
result3 = run_monitor_tick(pos3, {'pnl_pct': 1.5, 'z_static': 0, 'best_pnl': 0.3, 'hours_in': 1}, params=_p)
test("tick best_pnl updated", result3['best_pnl'] == 1.5)
test("tick best_patch", result3['best_pnl_patch'] is not None)

pos4 = _fresh_pos()
run_monitor_tick(pos4, {'pnl_pct': 0.8, 'z_static': 0.1, 'best_pnl': 0.8, 'hours_in': 1},
                 params=ExitParams(auto_exit_z=0.3, auto_exit_z_min_pnl=0.5))
test("tick Z_TRAIL activated", pos4.get('_z_trail_activated') is True)

pos5 = _fresh_pos()
result5 = run_monitor_tick(pos5, {'pnl_pct': 0.1, 'z_static': -2, 'best_pnl': 0.1, 'hours_in': 18},
                           params=ExitParams(max_hold_hours=16))
test("tick timeout", result5['should_close'] and 'TIMEOUT' in result5['reason'])

test("trail_patch is dict", isinstance(result['trail_patch'], dict))
test("trail_keys subset", all(k in TRAIL_KEYS for k in result['trail_patch']))

# ═══════════════════════════════════════════════════════
# ENGINE/AUTO_ENTRY
# ═══════════════════════════════════════════════════════

print("engine/validate_pending...")

test("valid data", validate_pending({'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG', 'entry_z': 2.5, 'entry_hr': 1.0})[0])
test("no coin1", not validate_pending({'coin2': 'BTC', 'direction': 'LONG', 'entry_z': 2.5})[0])
test("bad direction", not validate_pending({'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'UP', 'entry_z': 2.5})[0])
test("z too high", not validate_pending({'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG', 'entry_z': 20})[0])
test("z zero", not validate_pending({'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG', 'entry_z': 0})[0])
test("hr too high", not validate_pending({'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG', 'entry_z': 2.5, 'entry_hr': 200})[0])
test("bad coin chars", not validate_pending({'coin1': 'ETH;DROP', 'coin2': 'BTC', 'direction': 'LONG', 'entry_z': 2.5})[0])
test("not dict", not validate_pending("string")[0])
test("SHORT valid", validate_pending({'coin1': 'SOL', 'coin2': 'AVAX', 'direction': 'SHORT', 'entry_z': -3.0, 'entry_hr': 0.5})[0])
test("negative z valid", validate_pending({'coin1': 'AA', 'coin2': 'BB', 'direction': 'LONG', 'entry_z': -2.5, 'entry_hr': 1.0})[0])

print("engine/check_pending_ttl...")

with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
    f.write(b'{}')
    _tmp = f.name

test("fresh not expired", not check_pending_ttl(_tmp, ttl_seconds=7200)[0])
# Make file old
os.utime(_tmp, (time.time() - 8000, time.time() - 8000))
test("old expired", check_pending_ttl(_tmp, ttl_seconds=7200)[0])
os.unlink(_tmp)

print("engine/load_filters_state...")

_tmpdir = tempfile.mkdtemp()
test("no file = all False", all(v is False for v in load_filters_state(_tmpdir).values()))

with open(os.path.join(_tmpdir, 'filters_state.json'), 'w') as f:
    json.dump({'block_green': True, 'block_long': True}, f)
fs = load_filters_state(_tmpdir)
test("reads from file", fs['block_green'] is True and fs['block_long'] is True and fs['block_short'] is False)
import shutil
shutil.rmtree(_tmpdir)

# ═══════════════════════════════════════════════════════
# ENGINE/SCANNER
# ═══════════════════════════════════════════════════════

print("engine/scanner helpers...")

test("filter coins", 'USDT' not in filter_coins(['BTC', 'ETH', 'USDT', 'BUSD']))
test("calc bars 4h 50d", calc_scan_bars(50, '4h') == 300)
test("calc bars 1h 7d", calc_scan_bars(7, '1h') == 168)

pairs = build_pair_list(['BTC', 'ETH', 'SOL'])
test("pair count", len(pairs) == 3 and ('BTC', 'ETH') in pairs)

skip, reason = should_skip_pair('ETH', 'BTC', hedge_ratio=0.001, min_hr=0.05)
test("skip tiny HR", skip and 'HR' in reason)

# ═══════════════════════════════════════════════════════

elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"  engine: {passed} passed, {failed} failed, {elapsed:.3f}s")
print(f"{'='*60}")
if errors:
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("  All tests passed!")
