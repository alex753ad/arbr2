"""
core/backtesting.py — Backtesting functions: mini_backtest, micro_backtest,
walk_forward_validate, z_velocity_analysis, smart_exit_analysis.

Извлечено из mean_reversion_analysis.py (Волна 4/5).
НОЛЬ импортов Streamlit.

CFG() заменён на явные параметры в z_velocity_analysis и smart_exit_analysis.
Для обратной совместимости — fallback CFG если параметры не переданы.
"""

import numpy as np
from scipy import stats

# Import analysis functions used by backtesting
try:
    from .pair_analysis import (
        calculate_adaptive_robust_zscore,
        calculate_crossing_density,
        calculate_ou_parameters,
        calculate_rolling_correlation,
        calculate_rolling_zscore,
    )
except ImportError:
    # Fallback: import from mean_reversion_analysis
    from mean_reversion_analysis import (
        calculate_adaptive_robust_zscore,
        calculate_crossing_density,
        calculate_ou_parameters,
        calculate_rolling_correlation,
        calculate_rolling_zscore,
    )

# CFG fallback for backward compat
try:
    from config_loader import CFG
except ImportError:
    def CFG(section, key=None, default=None):
        _d = {'strategy': {'entry_z': 2.5, 'exit_z': 0.5, 'commission_pct': 0.10,
              'slippage_pct': 0.05, 'take_profit_pct': 2.0, 'stop_loss_pct': -5.0,
              'micro_bt_max_bars': 6},
              'z_velocity': {'lookback': 5, 'excellent_min_vel': 0.1, 'decel_threshold': 0.05},
              'monitor': {'trailing_z_bounce': 0.8, 'time_critical_ratio': 2.0,
                          'time_exit_ratio': 1.5, 'time_warning_ratio': 1.0,
                          'overshoot_deep_z': 1.0, 'pnl_trailing_threshold': 0.5,
                          'pnl_trailing_fraction': 0.4}}
        if key is None:
            return _d.get(section, {})
        return _d.get(section, {}).get(key, default)


def mini_backtest(spread, p1, p2, hrs, entry_z=1.8, exit_z=0.8,
                  stop_z=4.0, halflife_bars=None, commission_pct=0.10,
                  slippage_pct=0.05, min_bars=2, max_bars=50):
    """
    v19.0: Lightweight backtest — REALISTIC exit rules for crypto.
    
    Key changes from v17:
      - exit_z=0.8 (not 0.5) — take profit at 55% reversion, not 75%
      - stop = entry + 1.5 (not +2.0) — cut losses faster
      - trailing: exit at 40% of peak (not at 0%)
      - max_bars=50 (not 100) — don't hold dying trades
      - min_bars=2 (not 3) — allow faster exits
      - Overshoot profit: z crosses zero → take profit immediately
    
    Gate: Total P&L < -8% OR Sharpe < -0.8 → FAIL (relaxed from -5%/-0.5)
    """
    spread = np.array(spread, float)
    p1 = np.array(p1, float)
    p2 = np.array(p2, float)
    hrs = np.array(hrs, float)
    n = len(spread)
    
    if n < 80:
        return {'verdict': 'SKIP', 'reason': 'Too few bars', 'n_trades': 0}
    
    # Z-score
    z_cur, zs, z_window = calculate_adaptive_robust_zscore(
        spread, halflife_bars=halflife_bars)
    
    # Adaptive stop: tighter than before
    adaptive_stop = max(stop_z, entry_z + 1.5)
    
    # Costs
    cost_total = (commission_pct + slippage_pct) * 4 / 100
    
    # Simulation
    trades_pnl = []
    position = None
    min_hold = min_bars
    cooldown = max(3, int(halflife_bars) if halflife_bars and halflife_bars < 50 else 3)
    last_close = -cooldown - 1
    warmup = max(z_window + 10, 50)
    
    for i in range(warmup, n):
        z = zs[i]
        if np.isnan(z):
            continue
        
        # OPEN (with pre-entry guard)
        if position is None and (i - last_close) > cooldown:
            if z > entry_z and z < adaptive_stop:
                position = {'bar': i, 'dir': 'SHORT', 'z': z,
                            'p1': p1[i], 'p2': p2[i], 'hr': hrs[i], 'best': 0}
            elif z < -entry_z and z > -adaptive_stop:
                position = {'bar': i, 'dir': 'LONG', 'z': z,
                            'p1': p1[i], 'p2': p2[i], 'hr': hrs[i], 'best': 0}
        
        # CLOSE
        if position is not None:
            bars = i - position['bar']
            hr_e = position['hr']
            r1 = (p1[i] - position['p1']) / position['p1'] if position['p1'] > 0 else 0
            r2 = (p2[i] - position['p2']) / position['p2'] if position['p2'] > 0 else 0
            raw = (r1 - hr_e * r2) if position['dir'] == 'LONG' else (-r1 + hr_e * r2)
            pnl = raw / (1 + abs(hr_e)) * 100
            
            if pnl > position['best']:
                position['best'] = pnl
            
            close = False
            if bars >= min_hold:
                # Mean revert: Z crosses into exit zone
                if position['dir'] == 'LONG' and z >= -exit_z:
                    close = True
                elif position['dir'] == 'SHORT' and z <= exit_z:
                    close = True
                # Overshoot: Z crosses zero to other side → take profit
                if position['dir'] == 'LONG' and z > 0.5:
                    close = True
                elif position['dir'] == 'SHORT' and z < -0.5:
                    close = True
            
            # Stop loss (tighter)
            if position['dir'] == 'LONG' and z < -adaptive_stop:
                close = True
            elif position['dir'] == 'SHORT' and z > adaptive_stop:
                close = True
            
            # Trailing: exit at 40% of peak (not 0%)
            if position['best'] >= 0.8 and pnl <= position['best'] * 0.4 and bars >= min_hold:
                close = True
            
            # Max hold (shorter)
            if bars >= max_bars:
                close = True
            
            if close:
                trades_pnl.append(pnl - cost_total * 100)
                position = None
                last_close = i
    
    # Close remaining
    if position is not None:
        hr_e = position['hr']
        r1 = (p1[-1] - position['p1']) / position['p1'] if position['p1'] > 0 else 0
        r2 = (p2[-1] - position['p2']) / position['p2'] if position['p2'] > 0 else 0
        raw = (r1 - hr_e * r2) if position['dir'] == 'LONG' else (-r1 + hr_e * r2)
        pnl = raw / (1 + abs(hr_e)) * 100 - cost_total * 100
        trades_pnl.append(pnl)
    
    # Stats
    nt = len(trades_pnl)
    if nt == 0:
        return {'verdict': 'SKIP', 'reason': 'No trades', 'n_trades': 0}
    
    total_pnl = sum(trades_pnl)
    wins = [p for p in trades_pnl if p > 0]
    losses = [p for p in trades_pnl if p <= 0]
    win_rate = len(wins) / nt * 100
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 99
    avg_pnl = np.mean(trades_pnl)
    std_pnl = np.std(trades_pnl) if nt > 1 else 1
    sharpe = avg_pnl / std_pnl * np.sqrt(nt) if std_pnl > 0 else 0
    
    # Verdict: very lenient — only block truly catastrophic pairs
    # Mini-BT on 300 bars is noisy; we only want to catch "clearly broken" pairs
    if total_pnl < -15.0 or sharpe < -1.5:
        verdict = 'FAIL'
    elif total_pnl < -5.0 or sharpe < -0.5 or pf < 0.5:
        verdict = 'WARN'
    else:
        verdict = 'PASS'
    
    return {
        'verdict': verdict,
        'total_pnl': round(total_pnl, 2),
        'sharpe': round(sharpe, 2),
        'win_rate': round(win_rate, 1),
        'pf': round(pf, 2),
        'n_trades': nt,
        'avg_pnl': round(avg_pnl, 2),
    }


def micro_backtest(spread, p1, p2, hrs, entry_z=1.8, exit_z=0.8,
                   max_hold_bars=6, take_profit_pct=1.5, stop_loss_pct=-2.0,
                   commission_pct=0.10, slippage_pct=0.05, min_bars=1):
    """
    v23: R2 Micro-Backtest — 1-6 bar horizon, matches real trading style.
    
    Problem: Standard BT simulates 20+ trades over 50 days, but real trading
    is 1-3 hours. BT shows -8%, reality is +0.9%. 
    
    Solution: Micro-BT enters at Z > threshold, holds max 6 bars,
    exits at exit_z OR take_profit OR stop_loss.
    
    Returns metrics matching real trading: avg P&L per entry, % quick reversions,
    mean Z velocity toward 0.
    """
    spread = np.array(spread, float)
    p1 = np.array(p1, float)
    p2 = np.array(p2, float)
    hrs = np.array(hrs, float)
    n = len(spread)
    
    if n < 50:
        return {'verdict': 'SKIP', 'error': 'Недостаточно данных', 'n_trades': 0}
    
    # Compute Z-score
    from scipy.stats import median_abs_deviation
    window = max(10, min(30, n // 10))
    zs = np.full(n, np.nan)
    for i in range(window, n):
        seg = spread[i-window:i+1]
        med = np.median(seg)
        mad = median_abs_deviation(seg)
        if mad > 1e-10:
            zs[i] = (spread[i] - med) / (mad * 1.4826)
    
    commission = (commission_pct + slippage_pct) / 100
    trades = []
    
    i = window + 1
    while i < n - 1:
        z = zs[i]
        if np.isnan(z):
            i += 1
            continue
        
        direction = None
        if z > entry_z:
            direction = 'SHORT'
        elif z < -entry_z:
            direction = 'LONG'
        
        if direction is None:
            i += 1
            continue
        
        # Entry
        entry_bar = i
        entry_hr = hrs[i]
        entry_p1, entry_p2 = p1[i], p2[i]
        entry_z_val = z
        best_pnl = 0
        
        # Hold loop (max_hold_bars)
        exit_bar = None
        exit_reason = 'MAX_HOLD'
        
        for j in range(1, max_hold_bars + 1):
            if i + j >= n:
                exit_bar = min(i + j, n - 1)
                exit_reason = 'END_DATA'
                break
            
            # Calculate P&L at bar i+j
            r1 = (p1[i+j] - entry_p1) / entry_p1
            r2 = (p2[i+j] - entry_p2) / entry_p2
            
            if direction == 'LONG':
                pnl = (r1 - entry_hr * r2) / (1 + abs(entry_hr)) * 100
            else:
                pnl = (-r1 + entry_hr * r2) / (1 + abs(entry_hr)) * 100
            
            if pnl > best_pnl:
                best_pnl = pnl
            
            z_now = zs[i+j] if not np.isnan(zs[i+j]) else z
            
            # Exit conditions
            if j >= min_bars:
                # Z mean-reverted
                if direction == 'LONG' and z_now >= -exit_z:
                    exit_bar = i + j
                    exit_reason = 'MEAN_REVERT'
                    break
                elif direction == 'SHORT' and z_now <= exit_z:
                    exit_bar = i + j
                    exit_reason = 'MEAN_REVERT'
                    break
                
                # Take profit
                if take_profit_pct > 0 and pnl >= take_profit_pct:
                    exit_bar = i + j
                    exit_reason = 'TAKE_PROFIT'
                    break
            
            # Stop loss (always active)
            if pnl <= stop_loss_pct:
                exit_bar = i + j
                exit_reason = 'STOP_LOSS'
                break
        
        if exit_bar is None:
            exit_bar = min(i + max_hold_bars, n - 1)
        
        # Final P&L
        r1 = (p1[exit_bar] - entry_p1) / entry_p1
        r2 = (p2[exit_bar] - entry_p2) / entry_p2
        if direction == 'LONG':
            final_pnl = (r1 - entry_hr * r2) / (1 + abs(entry_hr)) * 100
        else:
            final_pnl = (-r1 + entry_hr * r2) / (1 + abs(entry_hr)) * 100
        
        final_pnl -= commission * 2 * 100  # entry + exit commission
        bars_held = exit_bar - entry_bar
        
        # Z velocity (how fast Z moved toward 0)
        z_exit = zs[exit_bar] if not np.isnan(zs[exit_bar]) else entry_z_val
        z_velocity = (abs(entry_z_val) - abs(z_exit)) / max(1, bars_held)
        
        trades.append({
            'entry_bar': entry_bar, 'exit_bar': exit_bar,
            'direction': direction, 'entry_z': entry_z_val,
            'exit_z': z_exit, 'bars_held': bars_held,
            'pnl_pct': round(final_pnl, 3),
            'best_pnl': round(best_pnl, 3),
            'exit_reason': exit_reason,
            'z_velocity': round(z_velocity, 4),
        })
        
        i = exit_bar + 1  # Skip past exit
    
    # Aggregate
    nt = len(trades)
    if nt == 0:
        return {'verdict': 'SKIP', 'error': 'Нет сделок', 'n_trades': 0}
    
    pnls = [t['pnl_pct'] for t in trades]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / nt
    win_rate = sum(1 for p in pnls if p > 0) / nt * 100
    avg_bars = sum(t['bars_held'] for t in trades) / nt
    avg_z_vel = sum(t['z_velocity'] for t in trades) / nt
    
    # Quick reversion rate: % of trades that exited via MEAN_REVERT or TAKE_PROFIT
    quick_exits = sum(1 for t in trades if t['exit_reason'] in ('MEAN_REVERT', 'TAKE_PROFIT'))
    quick_rate = quick_exits / nt * 100
    
    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0) or 0.001
    gross_loss = abs(sum(p for p in pnls if p < 0)) or 0.001
    pf = gross_profit / gross_loss
    
    # Exit type breakdown
    exit_counts = {}
    for t in trades:
        er = t['exit_reason']
        if er not in exit_counts:
            exit_counts[er] = {'count': 0, 'pnl': 0}
        exit_counts[er]['count'] += 1
        exit_counts[er]['pnl'] += t['pnl_pct']
    
    # Verdict — quick_reversion_rate is the PRIMARY criterion
    # Real trades: avg +0.5%, WR ~60%, but BT can't capture maker orders etc.
    # Commission (0.30% roundtrip) dominates small P&L, so we focus on reversion speed.
    if quick_rate >= 50 and avg_pnl >= -0.3:
        verdict = 'PASS'  # High quick revert, P&L within commission noise
    elif quick_rate >= 35 or (avg_pnl >= -0.1 and win_rate >= 35):
        verdict = 'WARN'  # Moderate reversion
    else:
        verdict = 'FAIL'  # Spread doesn't revert quickly
    
    return {
        'verdict': verdict,
        'n_trades': nt,
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 3),
        'win_rate': round(win_rate, 1),
        'pf': round(pf, 2),
        'avg_bars_held': round(avg_bars, 1),
        'avg_z_velocity': round(avg_z_vel, 4),
        'quick_reversion_rate': round(quick_rate, 1),
        'exit_breakdown': exit_counts,
        'max_hold_bars': max_hold_bars,
        'trades': trades[-20:],  # Last 20 for display
    }


def z_velocity_analysis(zscore_series, lookback=5):
    """
    v24: R4 Z-Velocity Entry Filter.
    
    Analyzes Z-score momentum to determine if spread is:
    - Decelerating (good for entry — Z slowing down before reverting)
    - Accelerating away (bad — Z still expanding)
    - Reversing (best — Z already moving toward zero)
    
    Returns dict with velocity, acceleration, and entry recommendation.
    """
    zs = np.array(zscore_series, float)
    zs = zs[~np.isnan(zs)]
    
    if len(zs) < lookback + 2:
        return {
            'velocity': 0, 'acceleration': 0,
            'direction': 'UNKNOWN', 'entry_quality': 'UNKNOWN',
            'description': 'Недостаточно данных'
        }
    
    # Last `lookback` Z-scores
    recent = zs[-lookback:]
    
    # Velocity: dZ/dt over last bars (positive = Z going up)
    velocities = np.diff(recent)
    avg_velocity = np.mean(velocities)
    last_velocity = velocities[-1]
    
    # Acceleration: d²Z/dt² (change of velocity)
    if len(velocities) >= 2:
        accelerations = np.diff(velocities)
        avg_acceleration = np.mean(accelerations)
    else:
        avg_acceleration = 0
    
    # Current Z
    z_current = zs[-1]
    z_sign = np.sign(z_current)  # +1 for positive Z, -1 for negative
    
    # Is Z moving TOWARD zero? (good)
    # For Z>0: velocity<0 means toward zero
    # For Z<0: velocity>0 means toward zero
    z_toward_zero = (z_sign > 0 and avg_velocity < 0) or \
                    (z_sign < 0 and avg_velocity > 0) or \
                    z_sign == 0
    
    # Is Z DECELERATING its move away from zero? (medium)
    # v27: thresholds from config
    _decel_thr = CFG('z_velocity', 'decel_threshold', 0.05)
    z_decelerating = (z_sign > 0 and avg_acceleration < -_decel_thr) or \
                     (z_sign < 0 and avg_acceleration > _decel_thr)
    
    # Entry quality assessment
    _exc_vel = CFG('z_velocity', 'excellent_min_vel', 0.1)
    abs_vel = abs(avg_velocity)
    
    if z_toward_zero and abs_vel > _exc_vel:
        entry_quality = 'EXCELLENT'
        description = f'Z ревертирует (v={avg_velocity:+.2f}/бар). Идеальный вход!'
    elif z_toward_zero:
        entry_quality = 'GOOD'
        description = f'Z замедлился и разворачивается (v={avg_velocity:+.2f}/бар).'
    elif z_decelerating:
        entry_quality = 'FAIR'
        description = f'Z замедляется (a={avg_acceleration:+.2f}). Скоро может развернуться.'
    elif abs_vel < 0.05:
        entry_quality = 'FAIR'
        description = f'Z стабилен (v={avg_velocity:+.2f}/бар). Нейтральный вход.'
    else:
        entry_quality = 'POOR'
        description = f'Z ускоряется от нуля (v={avg_velocity:+.2f}/бар). Подождите замедления!'
    
    return {
        'velocity': round(avg_velocity, 4),
        'last_velocity': round(last_velocity, 4),
        'acceleration': round(avg_acceleration, 4),
        'z_toward_zero': z_toward_zero,
        'z_decelerating': z_decelerating,
        'entry_quality': entry_quality,
        'description': description,
        'lookback': lookback,
    }


def smart_exit_analysis(z_entry, z_now, z_history, pnl_pct, hours_in,
                        halflife_hours, direction, best_pnl=None):
    """
    v24: R5 Smart Exit Signals.
    
    Three exit strategies:
    1. Trailing Z-stop: after Z passes 0.5 toward zero, lock in by trailing
    2. Time-based urgency: if >1.5x halflife without reversion → urgent exit
    3. Overshoot profit lock: Z crossed 0 and going further → take profit
    
    Returns exit recommendation with urgency level.
    """
    signals = []
    urgency = 0  # 0=hold, 1=watch, 2=exit, 3=urgent
    
    z_hist = np.array(z_history, float) if z_history is not None else np.array([z_now])
    z_hist = z_hist[~np.isnan(z_hist)]
    
    if best_pnl is None:
        best_pnl = max(pnl_pct, 0)
    
    # === 1. TRAILING Z-STOP ===
    # v28: FIX — only track Z since entry, not full 300-bar history!
    # BUG WAS: min(z_hist) picks up Z=-28 from weeks before trade
    _z_bounce = CFG('monitor', 'trailing_z_bounce', 0.8)
    
    # Estimate bars since entry to limit z_history
    _hpb = {'1h': 1, '4h': 4, '1d': 24}.get('4h', 4)
    _bars_since_entry = max(1, int(hours_in / _hpb) + 2)  # +2 safety margin
    z_since_entry = z_hist[-_bars_since_entry:] if len(z_hist) > _bars_since_entry else z_hist
    
    # Track best Z (closest to 0) ONLY since entry
    if direction == 'SHORT':
        best_z_for_us = min(z_since_entry) if len(z_since_entry) > 0 else z_now
        z_reverted_well = best_z_for_us < 0.5
        z_retreated = z_now > best_z_for_us + _z_bounce
    else:
        best_z_for_us = max(z_since_entry) if len(z_since_entry) > 0 else z_now
        z_reverted_well = best_z_for_us > -0.5
        z_retreated = z_now < best_z_for_us - _z_bounce
    
    if z_reverted_well and z_retreated:
        signals.append({
            'type': 'TRAILING_Z',
            'urgency': 2,
            'message': f'📉 Best Z (since entry): {best_z_for_us:+.2f} → now {z_now:+.2f} '
                       f'(bounce ≥{_z_bounce:.1f}). Z-trailing stop.'
        })
        urgency = max(urgency, 2)
    
    # === 2. TIME-BASED URGENCY ===
    _t_crit = CFG('monitor', 'time_critical_ratio', 2.0)
    _t_exit = CFG('monitor', 'time_exit_ratio', 1.5)
    _t_warn = CFG('monitor', 'time_warning_ratio', 1.0)
    
    if halflife_hours > 0:
        time_ratio = hours_in / halflife_hours
        
        if time_ratio > _t_crit:
            signals.append({
                'type': 'TIME_CRITICAL',
                'urgency': 3,
                'message': f'⏰ В позиции {hours_in:.0f}ч = {time_ratio:.1f}x HL ({halflife_hours:.0f}ч). '
                           f'Коинтеграция могла разрушиться. СРОЧНЫЙ ВЫХОД.'
            })
            urgency = max(urgency, 3)
        elif time_ratio > _t_exit:
            signals.append({
                'type': 'TIME_WARNING',
                'urgency': 2,
                'message': f'⏰ В позиции {hours_in:.0f}ч = {time_ratio:.1f}x HL. '
                           f'Если Z не вернулся — рассмотрите выход.'
            })
            urgency = max(urgency, 2)
        elif time_ratio > _t_warn:
            signals.append({
                'type': 'TIME_WATCH',
                'urgency': 1,
                'message': f'⏳ В позиции {hours_in:.0f}ч = {time_ratio:.1f}x HL. Следите за Z.'
            })
            urgency = max(urgency, 1)
    
    # === 3. OVERSHOOT PROFIT LOCK ===
    _overshoot_z = CFG('monitor', 'overshoot_deep_z', 1.0)
    if direction == 'SHORT':
        z_crossed_zero = z_entry > 0 and z_now < 0
        z_overshoot_deep = z_now < -_overshoot_z
    else:
        z_crossed_zero = z_entry < 0 and z_now > 0
        z_overshoot_deep = z_now > _overshoot_z
    
    if z_crossed_zero:
        if z_overshoot_deep:
            signals.append({
                'type': 'OVERSHOOT_DEEP',
                'urgency': 2,
                'message': f'🎯 OVERSHOOT: Z пересёк 0 и достиг {z_now:+.2f}. '
                           f'P&L={pnl_pct:+.2f}%. ФИКСИРУЙТЕ ПРИБЫЛЬ!'
            })
            urgency = max(urgency, 2)
        else:
            signals.append({
                'type': 'OVERSHOOT_MILD',
                'urgency': 1,
                'message': f'🎯 Z пересёк 0 (сейчас {z_now:+.2f}). '
                           f'Можно зафиксировать или подождать overshoot.'
            })
            urgency = max(urgency, 1)
    
    # === 4. PnL PROFIT PROTECTION ===
    _pnl_thr = CFG('monitor', 'pnl_trailing_threshold', 0.5)
    _pnl_frac = CFG('monitor', 'pnl_trailing_fraction', 0.4)
    if best_pnl > _pnl_thr and pnl_pct < best_pnl * _pnl_frac:
        signals.append({
            'type': 'PNL_TRAILING',
            'urgency': 2,
            'message': f'💰 P&L упал с пика {best_pnl:+.2f}% до {pnl_pct:+.2f}%. '
                       f'Фиксируйте остаток прибыли.'
        })
        urgency = max(urgency, 2)
    elif best_pnl > 1.0:
        signals.append({
            'type': 'PNL_PEAK',
            'urgency': 1,
            'message': f'💰 P&L достигал {best_pnl:+.2f}%. Текущий: {pnl_pct:+.2f}%. '
                       f'Рассмотрите фиксацию.'
        })
        urgency = max(urgency, 1)
    
    # === OVERALL RECOMMENDATION ===
    if urgency >= 3:
        recommendation = '🛑 СРОЧНО ЗАКРЫТЬ'
    elif urgency >= 2:
        recommendation = '✅ ЗАКРЫВАТЬ'
    elif urgency >= 1:
        recommendation = '👀 НАБЛЮДАТЬ'
    else:
        recommendation = '⏳ ДЕРЖАТЬ'
    
    return {
        'signals': signals,
        'urgency': urgency,
        'recommendation': recommendation,
        'n_signals': len(signals),
        'best_z_for_us': round(best_z_for_us, 4) if len(z_hist) > 0 else 0,
    }


def walk_forward_validate(spread, p1, p2, hrs, entry_z=1.8,
                          n_folds=3, train_pct=0.65, **bt_kwargs):
    """
    v19.0: Walk-Forward Validation — anti-overfitting.
    
    Splits data into overlapping train/test windows:
      Fold 1: Train [0:195], Test [195:260]
      Fold 2: Train [40:235], Test [235:300]
      Fold 3: Train [65:260], Test [260:300]
    
    Only pairs profitable in MAJORITY of out-of-sample folds → PASS.
    
    Returns: dict with oos_pnl, oos_sharpe, folds_passed, verdict
    """
    n = len(spread)
    if n < 120:
        return {'verdict': 'SKIP', 'reason': 'Too few bars for WF', 'folds_passed': 0}
    
    fold_size = n // n_folds
    train_size = int(n * train_pct)
    test_size = n - train_size
    
    oos_pnls = []
    oos_details = []
    
    for fold in range(n_folds):
        # Sliding window
        offset = fold * (n - train_size) // max(1, n_folds - 1)
        if fold == n_folds - 1:
            offset = n - train_size - test_size  # Ensure last fold reaches end
        
        t_start = offset
        t_end = min(offset + train_size, n)
        test_start = t_end
        test_end = min(test_start + test_size, n)
        
        if test_end - test_start < 30:
            continue
        
        # Run mini-backtest on TEST portion only
        # (In walk-forward, the "parameters" come from train, but we test on OOS)
        test_spread = spread[test_start:test_end]
        test_p1 = p1[test_start:test_end]
        test_p2 = p2[test_start:test_end]
        test_hrs = hrs[test_start:test_end]
        
        if len(test_spread) < 40:
            continue
        
        result = mini_backtest(
            test_spread, test_p1, test_p2, test_hrs,
            entry_z=entry_z, **bt_kwargs
        )
        
        fold_pnl = result.get('total_pnl', 0)
        oos_pnls.append(fold_pnl)
        oos_details.append({
            'fold': fold + 1,
            'test_bars': test_end - test_start,
            'pnl': fold_pnl,
            'trades': result.get('n_trades', 0),
            'wr': result.get('win_rate', 0),
        })
    
    if not oos_pnls:
        return {'verdict': 'SKIP', 'reason': 'No valid folds', 'folds_passed': 0}
    
    folds_positive = sum(1 for p in oos_pnls if p > -1.0)  # Slightly negative OK
    folds_passed = folds_positive
    total_oos = sum(oos_pnls)
    avg_oos = np.mean(oos_pnls)
    
    # Verdict
    if folds_passed >= n_folds * 0.6 and total_oos > -3.0:
        verdict = 'PASS'
    elif folds_passed >= 1 and total_oos > -8.0:
        verdict = 'WARN'
    else:
        verdict = 'FAIL'
    
    return {
        'verdict': verdict,
        'total_oos_pnl': round(total_oos, 2),
        'avg_fold_pnl': round(avg_oos, 2),
        'folds_passed': folds_passed,
        'n_folds': len(oos_pnls),
        'folds': oos_details,
    }


# =============================================================================
# ТЕСТ
# =============================================================================

if __name__ == "__main__":
    print("=" * 65)
    print("  v10.0.0 — Adaptive Robust Z + Crossing Density + Correlation")
    print("=" * 65)
    np.random.seed(42)

    # Generate synthetic mean-reverting spread
    n = 300
    spread_mr = [0.0]
    for i in range(n - 1):
        dx = 0.3 * (0 - spread_mr[-1]) + 0.5 * np.random.randn()
        spread_mr.append(spread_mr[-1] + dx)
    spread_mr = np.array(spread_mr)

    # OU params for HL
    ou = calculate_ou_parameters(spread_mr, dt=1/6)  # 4h
    hl_bars = ou['halflife_ou'] / (4.0) if ou else 10  # HL_hours / hours_per_bar

    # 1. Adaptive Robust Z-score
    print(f"\n--- Adaptive Robust Z-Score ---")
    print(f"OU HL = {ou['halflife_ou']:.1f}ч → {hl_bars:.1f} bars (4h TF)")

    z_old, zs_old = calculate_rolling_zscore(spread_mr, window=30)
    z_new, zs_new, w_used = calculate_adaptive_robust_zscore(spread_mr, halflife_bars=hl_bars)
    print(f"Old (window=30, std):  Z={z_old:+.3f}")
    print(f"New (window={w_used}, MAD): Z={z_new:+.3f}")

    # Test with different HL values
    print(f"\nAdaptive windows for different HL:")
    for hl in [2, 5, 10, 20, 50]:
        _, _, w = calculate_adaptive_robust_zscore(spread_mr, halflife_bars=hl)
        print(f"  HL={hl:>2d} bars → window={w}")

    # 2. Crossing Density
    print(f"\n--- Crossing Density ---")
    cd_mr = calculate_crossing_density(zs_new)
    # Generate "stuck" spread
    stuck = np.concatenate([np.ones(100) * 2, np.ones(100) * -1, np.ones(100) * 1.5])
    cd_stuck = calculate_crossing_density(stuck)
    print(f"Mean-reverting: density={cd_mr:.3f} ({'✅ active' if cd_mr > 0.03 else '❌ stuck'})")
    print(f"Stuck spread:   density={cd_stuck:.3f} ({'✅ active' if cd_stuck > 0.03 else '❌ stuck'})")

    # 3. Rolling Correlation
    print(f"\n--- Rolling Correlation ---")
    s2 = np.cumsum(np.random.randn(n) * 0.3) + 100
    s1 = 1.5 * s2 + np.random.randn(n) * 0.5 + 20
    corr, corr_s = calculate_rolling_correlation(s1, s2, window=30)
    print(f"Correlated pair: ρ={corr:.3f}")
    s1_uncorr = np.cumsum(np.random.randn(n) * 0.3) + 50
    corr_u, _ = calculate_rolling_correlation(s1_uncorr, s2, window=30)
    print(f"Uncorrelated:    ρ={corr_u:.3f}")

    # 4. Sanitizer with min_bars + HR uncertainty
    print(f"\n--- Sanitizer v10.1 ---")
    tests = [
        (1.2, 3, 4, 2.0, 300, 0.1, "normal 300 bars"),
        (1.2, 3, 4, 2.0, 29,  0.1, "only 29 bars"),
        (1.2, 3, 4, 2.0, 30,  0.1, "30 bars (boundary)"),
        (0.0003, 2, 4, -2.3, 300, 0.0, "HR<0.001"),
        (0.18, 3, 4, -1.8, 300, 0.25, "HR unc=139%"),
        (0.18, 3, 4, -1.8, 300, 0.05, "HR unc=28%"),
    ]
    for hr, sp, st, z, nb, hs, name in tests:
        ok, reason = sanitize_pair(hr, sp, st, z, n_bars=nb, hr_std=hs)
        print(f"  {name:<22s} → {'✅' if ok else '❌'} {reason}")

    # 5. Quality with crossing penalty + HR unc penalty
    print(f"\n--- Quality Score v10.1 ---")
    q1, bd1 = calculate_quality_score(0.2, ou, 0.01, 0.75, 1.5, True, crossing_density=0.08, hr_std=0.1)
    q2, bd2 = calculate_quality_score(0.2, ou, 0.01, 0.75, 1.5, True, crossing_density=0.01, hr_std=0.1)
    q3, bd3 = calculate_quality_score(0.2, ou, 0.01, 0.75, 1.5, True, crossing_density=0.08, hr_std=1.0)
    print(f"Active/good HR: Q={q1} cross={bd1.get('crossing_penalty',0)} hr_unc={bd1.get('hr_unc_penalty',0)}")
    print(f"Stuck/good HR:  Q={q2} cross={bd2.get('crossing_penalty',0)} hr_unc={bd2.get('hr_unc_penalty',0)}")
    print(f"Active/bad HR:  Q={q3} cross={bd3.get('crossing_penalty',0)} hr_unc={bd3.get('hr_unc_penalty',0)}")

    # 6. Signal gates: Q, Z, Stability
    print(f"\n--- Signal Gates v10.3 ---")
    s1, d1, t1 = get_adaptive_signal(2.69, 'LOW', 8, '4h')
    s2, d2, t2 = get_adaptive_signal(2.69, 'LOW', 25, '4h')
    s3, d3, t3 = get_adaptive_signal(-1.83, 'HIGH', 63, '4h')
    s4, d4, t4 = get_adaptive_signal(2.5, 'MEDIUM', 56, '4h', stability_ratio=0.25)
    s5, d5, t5 = get_adaptive_signal(2.5, 'MEDIUM', 56, '4h', stability_ratio=0.75)
    s6, d6, t6 = get_adaptive_signal(4.8, 'LOW', 50, '4h')
    print(f"Q=8  LOW:           {s1} (expect NEUTRAL)")
    print(f"Q=25 LOW:           {s2} (expect NEUTRAL)")
    print(f"Q=63 HIGH:          {s3} (expect SIGNAL)")
    print(f"Q=56 Stab=1/4:      {s4} (expect WATCH)")
    print(f"Q=56 Stab=3/4:      {s5} (expect SIGNAL)")
    print(f"Z=4.8:              {s6} (expect NEUTRAL)")

    print(f"\n✅ v11.4 ready!")


# =============================================================================
# SHARED UTILITIES (imported by scanner, monitor, backtester)
# =============================================================================


