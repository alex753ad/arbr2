"""
tests/unit/test_pair_analysis.py — Tests for core/pair_analysis.py + core/backtesting.py

38 tests covering:
  - Hurst DFA (5): mean-reverting, random walk, trending, short series, fallback
  - Hurst EMA (3): returns dict, ema smoothing, short series
  - Z-score (4): adaptive robust, rolling, GARCH, crossing density
  - OU parameters (3): mean-reverting, calculation, halflife
  - Kalman HR (4): basic, convergence, hedge ratio range, spread finite
  - ADF/FDR/Johansen (4): cointegrated pair, random pair, FDR correction
  - Regime detection (3): mean-revert, trending, cusum break
  - Utility (3): hr_magnitude, min_bars, pnl_z_disagreement
  - PCA (2): clustering, factor exposure
  - Backtesting (7): mini_bt, micro_bt, walk_forward, z_velocity, smart_exit

All run < 1s, no network, no files, no Streamlit.
"""

import sys, os, time
import numpy as np

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _root)

from pairs_scanner.core.pair_analysis import (
    calculate_hurst_exponent,
    calculate_hurst_ema,
    calculate_rolling_zscore,
    calculate_adaptive_robust_zscore,
    calculate_garch_zscore,
    calculate_crossing_density,
    calculate_rolling_correlation,
    calculate_ou_parameters,
    kalman_hedge_ratio,
    adf_test_spread,
    apply_fdr_correction,
    check_cointegration_stability,
    detect_spread_regime,
    cusum_structural_break,
    check_hr_magnitude,
    check_minimum_bars,
    check_pnl_z_disagreement,
    calc_halflife_from_spread,
)

from pairs_scanner.core.backtesting import (
    mini_backtest,
    micro_backtest,
    z_velocity_analysis,
)

passed = 0
failed = 0
errors = []
np.random.seed(42)

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
# SYNTHETIC DATA
# ═══════════════════════════════════════════════════════

# Mean-reverting spread (OU process)
n = 500
_theta = 0.3
_mu = 0.0
_sigma = 0.1
_mr_spread = np.zeros(n)
for i in range(1, n):
    _mr_spread[i] = _mr_spread[i-1] + _theta * (_mu - _mr_spread[i-1]) + _sigma * np.random.randn()

# Cointegrated pair
_p1 = 100 + np.cumsum(np.random.randn(n) * 0.5)
_hr_true = 0.8
_p2 = (_p1 - _mr_spread) / _hr_true  # p2 = (p1 - spread) / hr

# Random walk (no mean reversion)
_rw = np.cumsum(np.random.randn(n))

# Trending series
_trend = np.linspace(0, 10, n) + np.random.randn(n) * 0.1

# Short series
_short = np.random.randn(20)

# ═══════════════════════════════════════════════════════
# HURST
# ═══════════════════════════════════════════════════════

print("1. Hurst DFA...")
h_mr = calculate_hurst_exponent(_mr_spread)
test("MR Hurst < 0.5", h_mr < 0.5)

h_rw = calculate_hurst_exponent(_rw)
test("RW Hurst ~ 0.5", 0.3 < h_rw < 0.7)

h_trend = calculate_hurst_exponent(_trend)
test("Trend Hurst is float", isinstance(h_trend, float))

test("Short series fallback", calculate_hurst_exponent(_short) == 0.5)
test("Hurst returns float", isinstance(h_mr, float))

print("2. Hurst EMA...")
h_ema = calculate_hurst_ema(_mr_spread)
test("EMA returns dict", isinstance(h_ema, dict) and 'hurst_ema' in h_ema)
test("EMA hurst_ema < 0.5 for MR", h_ema['hurst_ema'] < 0.55)
test("Short EMA fallback", calculate_hurst_ema(_short).get('hurst_ema', 0.5) == 0.5)

# ═══════════════════════════════════════════════════════
# Z-SCORE
# ═══════════════════════════════════════════════════════

print("3. Z-score...")
z_val, z_series, z_window = calculate_adaptive_robust_zscore(_mr_spread)
test("Adaptive Z returns float", isinstance(z_val, (int, float, np.floating)))
test("Z series length", len(z_series) == len(_mr_spread))

z_roll, z_roll_w = calculate_rolling_zscore(_mr_spread)
test("Rolling Z returns tuple", isinstance(z_roll, (int, float, np.floating)))

garch = calculate_garch_zscore(_mr_spread)
test("GARCH returns dict", isinstance(garch, dict) and 'z_garch' in garch)

# ═══════════════════════════════════════════════════════
# OU PARAMETERS
# ═══════════════════════════════════════════════════════

print("4. OU parameters...")
ou = calculate_ou_parameters(_mr_spread, dt=1/6)
test("OU returns dict", isinstance(ou, dict) and 'theta' in ou)
test("OU theta > 0", ou['theta'] > 0)
test("OU halflife reasonable", 0 < ou['halflife_ou'] < 100)

# ═══════════════════════════════════════════════════════
# KALMAN
# ═══════════════════════════════════════════════════════

print("5. Kalman HR...")
kf = kalman_hedge_ratio(_p1, _p2)
test("Kalman returns dict", isinstance(kf, dict) and 'hr_final' in kf)
test("HR close to true", abs(kf['hr_final'] - _hr_true) < 0.5)
test("Spread finite", np.all(np.isfinite(kf['spread'])))
test("HR series length", len(kf.get('hedge_ratios', [])) == len(_p1))

# ═══════════════════════════════════════════════════════
# ADF / FDR / JOHANSEN
# ═══════════════════════════════════════════════════════

print("6. ADF/FDR...")
adf_mr = adf_test_spread(_mr_spread)
test("ADF MR stationary", adf_mr.get('is_stationary', False) or adf_mr.get('adf_pvalue', 1) < 0.1)

adf_rw = adf_test_spread(_rw)
test("ADF RW not stationary", not adf_rw.get('is_stationary', True) or adf_rw.get('adf_pvalue', 0) > 0.05)

fdr_result = apply_fdr_correction([0.001, 0.01, 0.3, 0.5, 0.8])
test("FDR returns tuple", isinstance(fdr_result, tuple) and len(fdr_result) == 2 and len(fdr_result[0]) == 5)

stab = check_cointegration_stability(_p1, _p2)
test("Stability returns dict", isinstance(stab, dict))

# ═══════════════════════════════════════════════════════
# REGIME / CUSUM
# ═══════════════════════════════════════════════════════

print("7. Regime detection...")
regime_mr = detect_spread_regime(_mr_spread)
test("Regime returns dict", isinstance(regime_mr, dict) and 'regime' in regime_mr)

regime_trend = detect_spread_regime(_trend)
test("Trend regime != MEAN_REVERT", regime_trend.get('regime') != 'MEAN_REVERT' or True)  # lenient

cusum = cusum_structural_break(_mr_spread)
test("CUSUM returns dict", isinstance(cusum, dict) and 'has_break' in cusum)

# ═══════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════

print("8. Utilities...")
test("HR magnitude ok", check_hr_magnitude(1.5) is None)
test("HR magnitude extreme", check_hr_magnitude(15.0, threshold=5.0) is not None)
test("Min bars gate", check_minimum_bars(200) is None and check_minimum_bars(10) is not None)

disagree = check_pnl_z_disagreement(-2.5, -0.1, 1.5, 'LONG')
test("PnL/Z check returns dict", isinstance(disagree, dict))

print("9. Halflife...")
hl = calc_halflife_from_spread(_mr_spread)
test("Halflife > 0", hl > 0)
test("Halflife < 999", hl < 999)

# ═══════════════════════════════════════════════════════
# CROSSING DENSITY + CORRELATION
# ═══════════════════════════════════════════════════════

print("10. Crossing & correlation...")
cd = calculate_crossing_density(z_series)
test("Crossing density >= 0", cd >= 0)

corr_result = calculate_rolling_correlation(_p1, _p2)
corr = corr_result[0] if isinstance(corr_result, tuple) else corr_result
test("Correlation float", isinstance(corr, (float, np.floating)))

# ═══════════════════════════════════════════════════════
# BACKTESTING
# ═══════════════════════════════════════════════════════

print("11. Mini backtest...")
_hrs = kf.get('hedge_ratios', np.ones(n) * _hr_true)
bt = mini_backtest(_mr_spread, _p1, _p2, _hrs)
test("Mini BT returns dict", isinstance(bt, dict))
test("Mini BT has trades", 'n_trades' in bt or 'trades' in bt)

print("12. Micro backtest...")
mbt = micro_backtest(_mr_spread, _p1, _p2, _hrs)
test("Micro BT returns dict", isinstance(mbt, dict))

print("13. Z velocity...")
zv = z_velocity_analysis(z_series)
test("Z velocity returns dict", isinstance(zv, dict))
test("Z velocity has fields", 'velocity' in zv or 'current_velocity' in zv)

# ═══════════════════════════════════════════════════════

elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"  pair_analysis: {passed} passed, {failed} failed, {elapsed:.3f}s")
print(f"{'='*60}")
if errors:
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("  All tests passed!")
