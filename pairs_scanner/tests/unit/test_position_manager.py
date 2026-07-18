"""
tests/unit/test_position_manager.py — Tests for core/position_manager.py

30+ tests covering all 9 exit scenarios + edge cases.
No Streamlit, no network, no files — runs in < 100ms.
"""

import sys, os, time
# 4 levels up: test_position_manager.py → unit/ → tests/ → pairs_scanner/ → /home/claude/
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _root)

from pairs_scanner.core.position_manager import (
    check_auto_exit, ExitParams, determine_exit_phase, calc_trailing_params,
)

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

def fresh_pos(**kw):
    base = {'id': 1, 'coin1': 'ETH', 'coin2': 'BTC', 'direction': 'LONG',
            'status': 'OPEN', 'entry_z': -2.5, 'entry_hr': 1.0}
    base.update(kw)
    return base

P = ExitParams()
t0 = time.time()

# ═══════════════════════════════════════════════════════
print("1. AUTO_SL...")
test("SL triggers", check_auto_exit(fresh_pos(), {'pnl_pct': -3.1, 'z_static': -1, 'best_pnl': 0, 'hours_in': 1}, P, pair_sl=-3.0)[0])
test("SL boundary", check_auto_exit(fresh_pos(), {'pnl_pct': -3.0, 'z_static': -1, 'best_pnl': 0, 'hours_in': 1}, P, pair_sl=-3.0)[0])
test("SL not above", not check_auto_exit(fresh_pos(), {'pnl_pct': -2.9, 'z_static': -1, 'best_pnl': 0, 'hours_in': 1}, P, pair_sl=-3.0)[0])
test("SL per-pair", check_auto_exit(fresh_pos(), {'pnl_pct': -1.5, 'z_static': -1, 'best_pnl': 0, 'hours_in': 1}, P, pair_sl=-1.0)[0])

print("2. Grace period...")
test("grace blocks", not check_auto_exit(fresh_pos(), {'pnl_pct': -5, 'z_static': 0, 'best_pnl': 0, 'hours_in': 0.01}, P)[0])
test("grace allows", check_auto_exit(fresh_pos(), {'pnl_pct': -5, 'z_static': 0, 'best_pnl': 0, 'hours_in': 0.1}, P)[0])

print("3. Z_TRAIL...")
pos_z = fresh_pos()
c, _ = check_auto_exit(pos_z, {'pnl_pct': 1.0, 'z_static': 0.1, 'best_pnl': 1.0, 'hours_in': 2}, P)
test("Z_TRAIL activated", pos_z.get('_z_trail_activated') is True)
test("Z_TRAIL no close yet", not c)
pos_z2 = fresh_pos(_z_trail_activated=True, _z_trail_peak=1.5)
c2, r2 = check_auto_exit(pos_z2, {'pnl_pct': 0.9, 'z_static': 0.2, 'best_pnl': 1.5, 'hours_in': 3}, P)
test("Z_TRAIL exit on dd", c2)
test("Z_TRAIL reason", 'AUTO_Z_TRAIL' in (r2 or ''))
pos_z3 = fresh_pos(_z_trail_activated=True, _z_trail_peak=0.3)
test("Z_TRAIL no exit PnL<0", not check_auto_exit(pos_z3, {'pnl_pct': -0.3, 'z_static': 0.1, 'best_pnl': 0.3, 'hours_in': 3}, P)[0])

print("4. TP_TRAIL...")
pos_tp = fresh_pos()
check_auto_exit(pos_tp, {'pnl_pct': 2.5, 'z_static': -0.5, 'best_pnl': 2.5, 'hours_in': 2}, P, pair_tp=2.0)
test("TP activates trail", pos_tp.get('_tp_trail_activated') is True)
pos_tp2 = fresh_pos(_tp_trail_activated=True, _tp_trail_peak=3.0)
c_tp, r_tp = check_auto_exit(pos_tp2, {'pnl_pct': 2.4, 'z_static': -1.5, 'best_pnl': 3.0, 'hours_in': 3}, ExitParams(two_phase_exit=False), pair_tp=2.0)
test("TP_TRAIL exit", c_tp)
test("TP_TRAIL reason", 'TP_TRAIL' in (r_tp or ''))

print("5. Standard trailing...")
c_t, r_t = check_auto_exit(fresh_pos(), {'pnl_pct': 0.5, 'z_static': -1.5, 'best_pnl': 2.0, 'hours_in': 8}, ExitParams(trailing_activate_pct=1.5, trailing_drawdown_pct=0.7, two_phase_exit=False))
test("Trail exit", c_t)
test("Trail reason", 'AUTO_TRAIL' in (r_t or ''))
test("Trail not below activate", not check_auto_exit(fresh_pos(), {'pnl_pct': 1.0, 'z_static': -1.5, 'best_pnl': 1.2, 'hours_in': 8}, ExitParams(trailing_activate_pct=1.5, trailing_drawdown_pct=0.7, two_phase_exit=False))[0])

print("6. Recovery trail...")
pos_rec = fresh_pos(best_pnl_during_trade=0.2)
check_auto_exit(pos_rec, {'pnl_pct': 0.6, 'z_static': -0.5, 'best_pnl': 0.2, 'hours_in': 3}, ExitParams(recovery_trail_threshold=0.5, auto_exit_z_min_pnl=999))
test("Recovery activated", pos_rec.get('_recovery_trail_activated') is True)
pos_r2 = fresh_pos(_recovery_trail_activated=True, _recovery_trail_peak=0.8, best_pnl_during_trade=0.1)
c_r, r_r = check_auto_exit(pos_r2, {'pnl_pct': 0.4, 'z_static': -0.5, 'best_pnl': 0.8, 'hours_in': 4}, P)
test("Recovery exit", c_r)
test("Recovery reason", 'RECOVERY' in (r_r or ''))

print("7. PNLSTOP...")
# PNLSTOP is safety net — SL always catches first in normal flow.
# Test 1: SL = -5, pnl_stop = -10 (already wider) → SL triggers at -5.5
c_ps1, r_ps1 = check_auto_exit(fresh_pos(pnl_stop_pct=-10.0), {"pnl_pct": -5.5, "z_static": -3, "best_pnl": 0, "hours_in": 5}, P, pair_sl=-5.0)
test("SL catches before PNLSTOP", c_ps1 and 'AUTO_SL' in (r_ps1 or ''))
# Test 2: PNLSTOP widening — pnl_stop=-1 (narrower than SL=-3) → widened to -4
pos_w = fresh_pos(pnl_stop_pct=-1.0)
c_ps2, r_ps2 = check_auto_exit(pos_w, {"pnl_pct": -3.5, "z_static": -3, "best_pnl": 0, "hours_in": 5}, P, pair_sl=-3.0)
test("SL catches widened case too", c_ps2 and 'AUTO_SL' in (r_ps2 or ''))

print("8. TIMEOUT...")
c_to, r_to = check_auto_exit(fresh_pos(), {'pnl_pct': 0.3, 'z_static': -1, 'best_pnl': 0.5, 'hours_in': 17}, P)
test("Timeout 17h>16h", c_to)
test("Timeout reason", 'TIMEOUT' in (r_to or ''))
test("No timeout 15h", not check_auto_exit(fresh_pos(), {'pnl_pct': 0.3, 'z_static': -1, 'best_pnl': 0.5, 'hours_in': 15}, P)[0])
test("Timeout per-pos 8h", check_auto_exit(fresh_pos(max_hold_hours=8), {'pnl_pct': 0.3, 'z_static': -1, 'best_pnl': 0.5, 'hours_in': 9}, P)[0])

print("9. Phase2 fallback...")
pos_fb = fresh_pos(direction='SHORT')
check_auto_exit(pos_fb, {'pnl_pct': 0.8, 'z_static': 2.0, 'best_pnl': 2.0, 'hours_in': 7}, ExitParams(phase2_pnl_fallback=1.5, phase2_hours_fallback=6.0))
test("Phase2 by hours", pos_fb.get('exit_phase') == 2)
pos_fb2 = fresh_pos(direction='SHORT')
check_auto_exit(pos_fb2, {'pnl_pct': 1.0, 'z_static': 2.0, 'best_pnl': 1.8, 'hours_in': 3}, ExitParams(phase2_pnl_fallback=1.5))
test("Phase2 by PnL", pos_fb2.get('exit_phase') == 2)

print("10. Edge cases...")
test("Disabled", not check_auto_exit(fresh_pos(), {'pnl_pct': -99, 'z_static': 0, 'best_pnl': 0, 'hours_in': 99}, ExitParams(auto_exit_enabled=False))[0])
c_zc, r_zc = check_auto_exit(fresh_pos(), {'pnl_pct': 0.8, 'z_static': 0.1, 'best_pnl': 0.8, 'hours_in': 2}, ExitParams(auto_exit_z_mode='CLOSE'))
test("Z CLOSE mode", c_zc and 'AUTO_Z' in (r_zc or ''))
test("trailing=false no trail", not check_auto_exit(fresh_pos(), {'pnl_pct': 0.5, 'z_static': -1.5, 'best_pnl': 2.5, 'hours_in': 5}, ExitParams(trailing_enabled=False))[0])

print("11. BUG-018 lock...")
pos_lk = fresh_pos()
check_auto_exit(pos_lk, {'pnl_pct': 0.8, 'z_static': 0.1, 'best_pnl': 0.8, 'hours_in': 2}, P)
test("Locked", pos_lk.get('_trail_params_locked') is True)
lk_act = pos_lk.get('_trail_act_locked')
check_auto_exit(pos_lk, {'pnl_pct': 0.7, 'z_static': 0.1, 'best_pnl': 0.8, 'hours_in': 8}, ExitParams(trailing_activate_pct=99))
test("Lock unchanged", pos_lk.get('_trail_act_locked') == lk_act)

print("12. determine_exit_phase...")
test("LONG Z+→p2", determine_exit_phase({'direction': 'LONG'}, 0.1, 0.5, P)['phase'] == 2)
test("LONG Z-→p1", determine_exit_phase({'direction': 'LONG'}, -2.0, 0.5, P)['phase'] == 1)
test("SHORT Z-→p2", determine_exit_phase({'direction': 'SHORT'}, -0.1, 0.5, P)['phase'] == 2)
test("disabled→p2", determine_exit_phase({'direction': 'LONG'}, -5, 0, ExitParams(two_phase_exit=False))['phase'] == 2)

print("13. calc_trailing_params...")
_, dd1 = calc_trailing_params(fresh_pos(), 1.0, P)
test("Early wider dd", dd1 >= 1.0)
_, dd2 = calc_trailing_params(fresh_pos(), 8.0, P)
test("Late tighter dd", dd2 <= 0.5)
a3, d3 = calc_trailing_params(fresh_pos(_trail_params_locked=True, _trail_act_locked=2.5, _trail_dd_locked=0.8), 1.0, P)
test("Locked stored", a3 == 2.5 and d3 == 0.8)
a4, d4 = calc_trailing_params(fresh_pos(), 4.0, ExitParams(phantom_trail_activate=1.8, phantom_trail_drawdown=0.6))
test("Phantom override", a4 == 1.8 and d4 == 0.6)
a5, d5 = calc_trailing_params(fresh_pos(entry_mad=2.0), 4.0, P)
test("MAD widens", a5 >= 2.0 and d5 >= 1.4)

elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"  {passed} passed, {failed} failed, {elapsed:.3f}s")
print(f"{'='*60}")
if errors:
    print(f"\nFailed:")
    for e in errors: print(f"  - {e}")
    sys.exit(1)
else:
    print("\nAll tests passed!")
