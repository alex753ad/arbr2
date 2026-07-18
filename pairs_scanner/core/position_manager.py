"""
core/position_manager.py — Auto-exit state machine (pure functions).

Extracted from monitor_v38_3.py check_auto_exit() (строки 1102-1370).
НОЛЬ импортов Streamlit. НОЛЬ вызовов CFG(). НОЛЬ файлового I/O.
Все параметры — явные аргументы → тестируется за миллисекунды.

Это позволяет daemon импортировать напрямую вместо exec()-хака.

Trailing state machine (порядок проверок):
  1. AUTO_SL          — аварийный стоп (pair SL)
  2. TP → trail       — TP достигнут, активируем trailing
  3. Z → trail        — Z вернулся к 0, активируем Z_TRAIL
  4. Z_TRAIL exit     — откат от пика ≥ z_trail_drawdown
  5. Recovery trail   — 2ч в убытке → восстановление → trail
  6. Standard trail   — HWM trailing в Phase 2
  7. TP-trail         — trailing после TP с tighter drawdown
  8. PNLSTOP          — emergency safety net (шире SL)
  9. TIMEOUT          — max_hold_hours

pos: dict — мутируется in-place (trail flags, exit_phase).
    Caller отвечает за persist.
"""

from __future__ import annotations
from dataclasses import dataclass


# ═══════════════════════════════════════════════════════
# EXIT PARAMS — все конфигурационные значения в одном месте
# ═══════════════════════════════════════════════════════

@dataclass
class ExitParams:
    """Все параметры auto-exit. Caller заполняет из CFG() один раз."""
    # Global
    auto_exit_enabled: bool = True
    entry_grace_minutes: float = 5.0

    # SL / TP defaults (per-pair overrides передаются отдельно)
    default_tp_pct: float = 2.0
    default_sl_pct: float = -3.0

    # Z-exit
    auto_exit_z: float = 0.3
    auto_exit_z_min_pnl: float = 0.5
    auto_exit_z_mode: str = 'TRAIL'   # TRAIL | CLOSE | DISABLED

    # Z_TRAIL
    z_trail_drawdown: float = 0.5

    # Trailing (standard PnL trail)
    trailing_enabled: bool = True
    trailing_activate_pct: float = 1.5
    trailing_drawdown_pct: float = 0.7

    # Recovery trail
    recovery_trail_threshold: float = 0.5
    recovery_trail_drawdown: float = 0.3

    # Two-phase exit
    two_phase_exit: bool = True
    phase1_z_threshold: float = 0.5
    phase2_trail_activate: float = 0.8
    phase2_trail_drawdown: float = 0.4
    phase2_pnl_fallback: float = 1.5
    phase2_hours_fallback: float = 6.0

    # PNLSTOP
    pnl_stop_pct: float = -10.0

    # Timeout
    max_hold_hours: float = 16.0

    # [A16] Stale exit — ранний выход для «мёртвых» сделок
    # Если hours_in >= stale_exit_hours И best_pnl < stale_exit_min_best И pnl < 0 → закрыть.
    # Phantom Dual TF: 7/12 TIMEOUT имели best=0. Экономит 0.3-0.5% на каждой.
    stale_exit_hours: float = 6.0
    stale_exit_min_best: float = 0.3

    # [A23] Adaptive SL — стоп-лосс зависит от entry Z.
    # При |Z|=2.5 (порог) SL=-2.0%, при |Z|=3.5 SL=-2.4%.
    # Формула: SL = -(adaptive_sl_base + adaptive_sl_per_z × |entry_z|)
    # Cap: не шире default_sl_pct (аварийный максимум).
    adaptive_sl_enabled: bool = True
    adaptive_sl_base: float = 1.0
    adaptive_sl_per_z: float = 0.4

    # [A24] Time-decay trailing — ужесточение trail для зависших сделок.
    # Если hours_in >= time_decay_hours И best_pnl >= time_decay_min_best →
    # drawdown сужается до time_decay_dd. Быстрее фиксирует после пика.
    time_decay_hours: float = 8.0
    time_decay_min_best: float = 0.5
    time_decay_dd: float = 0.3

    # Phantom autocalibrate result (inject from caller if available)
    phantom_trail_activate: float | None = None
    phantom_trail_drawdown: float | None = None


# ═══════════════════════════════════════════════════════
# DETERMINE EXIT PHASE
# ═══════════════════════════════════════════════════════

def determine_exit_phase(pos: dict, z_static: float, pnl_pct: float,
                         params: ExitParams) -> dict:
    """Phase 1 (Z still moving toward 0) vs Phase 2 (trailing active).

    Returns: {'phase': 1|2, 'reason': str, 'trail_params': dict|None}
    """
    if not params.two_phase_exit:
        return {'phase': 2, 'reason': 'Two-phase disabled', 'trail_params': None}

    direction = pos.get('direction', 'LONG')
    z_thresh = params.phase1_z_threshold

    # Check if Z has crossed the threshold (toward zero)
    if direction == 'LONG':
        z_crossed = z_static >= -z_thresh  # LONG entered at Z<0, wants Z→0+
    else:
        z_crossed = z_static <= z_thresh   # SHORT entered at Z>0, wants Z→0-

    min_pnl = params.auto_exit_z_min_pnl
    if z_crossed and pnl_pct >= min_pnl * 0.5:
        return {
            'phase': 2,
            'reason': f'Z crossed {z_thresh}: Z={z_static:+.2f}, P&L={pnl_pct:+.2f}%',
            'trail_params': {
                'activate': params.phase2_trail_activate,
                'drawdown': params.phase2_trail_drawdown,
            },
        }

    return {
        'phase': 1,
        'reason': f'Z still moving: Z={z_static:+.2f}, target=0',
        'trail_params': None,
    }


# ═══════════════════════════════════════════════════════
# CALC TRAILING PARAMS
# ═══════════════════════════════════════════════════════

def calc_trailing_params(pos: dict, hours_in: float,
                         params: ExitParams) -> tuple[float, float]:
    """Calculate trail_act and trail_dd for this tick.

    Priority chain (BUG-018 FIX):
      1. Locked params (if trail already activated) — immutable
      2. Config defaults + time-adaptive
      3. Phantom autocalibrate (if injected via params)
      4. MAD per-pair volatility (from pos['entry_mad'])

    Returns: (trail_activate, trail_drawdown)
    """
    if pos.get('_trail_params_locked', False):
        return (
            float(pos.get('_trail_act_locked', params.trailing_activate_pct)),
            float(pos.get('_trail_dd_locked', params.trailing_drawdown_pct)),
        )

    # Step 1+2: Config defaults + time-adaptive
    trail_act = params.trailing_activate_pct
    trail_dd = params.trailing_drawdown_pct
    if hours_in < 2.0:
        trail_dd = max(trail_dd, 1.0)   # Wider trail first 2h
    elif hours_in > 7.0:
        trail_dd = min(trail_dd, 0.5)   # Tighter near timeout

    # [A24] Time-decay: ещё жёстче для зависших сделок после пика.
    # Если best>0.5% но сделка провела >8ч — пик был, возврат маловероятен.
    # Сужаем drawdown до 0.3% чтобы зафиксировать остаток прибыли быстрее.
    best_pnl = pos.get('best_pnl_during_trade', pos.get('best_pnl', 0))
    if (hours_in >= params.time_decay_hours
            and best_pnl >= params.time_decay_min_best):
        trail_dd = min(trail_dd, params.time_decay_dd)

    # Step 3: Phantom autocalibrate (injected by caller)
    if params.phantom_trail_activate is not None:
        trail_act = params.phantom_trail_activate
    if params.phantom_trail_drawdown is not None:
        trail_dd = params.phantom_trail_drawdown

    # Step 4: MAD per-pair volatility
    entry_mad = pos.get('entry_mad', 0)
    if entry_mad and entry_mad > 0:
        trail_dd = max(trail_dd, max(1.0, entry_mad * 0.7))
        trail_act = max(trail_act, max(1.5, entry_mad * 1.0))

    return trail_act, trail_dd


# ═══════════════════════════════════════════════════════
# CHECK AUTO EXIT — главная функция
# ═══════════════════════════════════════════════════════

def check_auto_exit(pos: dict, mon: dict, params: ExitParams,
                    pair_tp: float | None = None,
                    pair_sl: float | None = None,
                    ) -> tuple[bool, str | None]:
    """Check if position should be auto-closed.

    Pure function: no CFG(), no st.session_state, no file I/O.
    pos is mutated in-place (trail flags) — caller persists.

    Args:
        pos:     position dict (мутируется: trail flags, exit_phase)
        mon:     monitor state dict with keys:
                   pnl_pct, z_static (or z_now), best_pnl, hours_in
        params:  ExitParams with all config values
        pair_tp: per-pair TP override (None → params.default_tp_pct)
        pair_sl: per-pair SL override (None → params.default_sl_pct)

    Returns:
        (should_close: bool, reason: str | None)
    """
    if not params.auto_exit_enabled:
        return False, None

    pnl = mon.get('pnl_pct', 0)
    z_static = mon.get('z_static', mon.get('z_now', 0))
    best_pnl = mon.get('best_pnl', 0)
    hours_in = mon.get('hours_in', 0)

    # 0. Grace period
    if hours_in < params.entry_grace_minutes / 60:
        return False, None

    # Resolve per-pair TP/SL
    tp = float(pair_tp if pair_tp is not None else params.default_tp_pct)
    sl = float(pair_sl if pair_sl is not None else params.default_sl_pct)

    # [A23] Adaptive SL: tighter for low |Z|, wider for high |Z|
    # SL = -(base + per_z × |entry_z|).  Cap at default_sl_pct.
    # |Z|=2.5 → -2.0%;  |Z|=3.0 → -2.2%;  |Z|=3.5 → -2.4%
    if params.adaptive_sl_enabled and pair_sl is None:
        _entry_z_abs = abs(float(pos.get('entry_z', 0) or 0))
        if _entry_z_abs > 0:
            _adaptive_sl = -(params.adaptive_sl_base + params.adaptive_sl_per_z * _entry_z_abs)
            # Cap: не шире аварийного default_sl_pct
            sl = max(_adaptive_sl, params.default_sl_pct)

    # ── 1. STOP LOSS ──────────────────────────────────────
    if pnl <= sl:
        return True, f"AUTO_SL: P&L={pnl:+.2f}% ≤ {sl:.1f}% (adaptive)" if params.adaptive_sl_enabled else f"AUTO_SL: P&L={pnl:+.2f}% ≤ {sl}%"

    # ── 2. TAKE PROFIT → activate trail ───────────────────
    if pnl >= tp:
        if params.trailing_enabled:
            pos['_tp_trail_activated'] = True
            pos['_tp_trail_peak'] = max(best_pnl, pnl)
        # trailing_enabled=false: Z_TRAIL is the only exit mechanism

    # ── 3. Z → activate Z_TRAIL ──────────────────────────
    if (params.auto_exit_z_mode != 'DISABLED'
            and abs(z_static) <= params.auto_exit_z
            and pnl >= params.auto_exit_z_min_pnl):
        if params.auto_exit_z_mode == 'TRAIL':
            pos['_z_trail_activated'] = True
            pos['_z_trail_peak'] = max(best_pnl, pnl)
        else:
            return True, (f"AUTO_Z: |Z|={abs(z_static):.2f} ≤ "
                         f"{params.auto_exit_z}, P&L={pnl:+.2f}%")

    # ── 4. Z_TRAIL exit (independent of trailing_enabled) ─
    if pos.get('_z_trail_activated', False):
        peak_val = pos.get('_z_trail_peak', best_pnl)
        actual_peak = max(peak_val, best_pnl)
        pos['_z_trail_peak'] = actual_peak
        if pnl > 0 and (actual_peak - pnl) >= params.z_trail_drawdown:
            return True, (f"AUTO_Z_TRAIL: Z→0 trail, "
                         f"peak={actual_peak:+.2f}%, now={pnl:+.2f}%, "
                         f"drop={actual_peak - pnl:.2f}%")

    # ── 4b. Trailing block (only if trailing_enabled) ─────
    if params.trailing_enabled:
        trail_act, trail_dd = calc_trailing_params(pos, hours_in, params)

        # ── Recovery trail ────────────────────────────────
        # BUG-021 FIX (corrected): use best_pnl_during_trade (stored HWM)
        # NOT max(stored, best_pnl, pnl) — including current pnl in the max
        # makes the condition _live_best < threshold impossible when pnl >= threshold.
        # Intent: position spent most time losing (historical best < threshold),
        # but THIS tick just recovered above threshold → activate tight trail.
        _stored_best = pos.get('best_pnl_during_trade', best_pnl)
        if (hours_in >= 2.0
                and _stored_best < params.recovery_trail_threshold
                and pnl >= params.recovery_trail_threshold):
            pos['_recovery_trail_activated'] = True
            pos['_recovery_trail_peak'] = pnl

        if pos.get('_recovery_trail_activated', False):
            rec_peak = pos.get('_recovery_trail_peak', pnl)
            actual_rec = max(rec_peak, pnl)
            pos['_recovery_trail_peak'] = actual_rec
            if pnl > 0 and (actual_rec - pnl) >= params.recovery_trail_drawdown:
                return True, (f"AUTO_RECOVERY_TRAIL: 2ч+ loss → recovery, "
                             f"peak={actual_rec:+.2f}%, now={pnl:+.2f}%, "
                             f"drop={actual_rec - pnl:.2f}%")

        # ── Lock trail params on first activation (BUG-018) ─
        _any_trail = (
            pos.get('_z_trail_activated', False) or
            pos.get('_tp_trail_activated', False) or
            pos.get('_recovery_trail_activated', False)
        )
        if _any_trail and not pos.get('_trail_params_locked', False):
            pos['_trail_act_locked'] = trail_act
            pos['_trail_dd_locked'] = trail_dd
            pos['_trail_params_locked'] = True

        # ── Two-phase exit ────────────────────────────────
        _in_phase2 = True
        if params.two_phase_exit:
            _phase = determine_exit_phase(pos, z_static, pnl, params)
            pos['exit_phase'] = _phase['phase']
            _in_phase2 = (_phase['phase'] == 2)
            if _in_phase2 and _phase.get('trail_params'):
                trail_act = min(trail_act, _phase['trail_params']['activate'])
                trail_dd = min(trail_dd, _phase['trail_params']['drawdown'])

            # Phase 2 fallback (v41 FIX)
            if not _in_phase2:
                if (best_pnl >= params.phase2_pnl_fallback
                        or hours_in >= min(params.phase2_hours_fallback,
                                           params.max_hold_hours * 0.75)):
                    _in_phase2 = True
                    pos['exit_phase'] = 2

        # ── Standard trailing (Phase 2 only) ─────────────
        if _in_phase2 and best_pnl >= trail_act and (best_pnl - pnl) >= trail_dd:
            return True, (f"AUTO_TRAIL: peak={best_pnl:+.2f}%, "
                         f"now={pnl:+.2f}%, drop={best_pnl - pnl:.2f}%")

        # ── TP-trail (Phase 2) ────────────────────────────
        tp_trail_active = pos.get('_tp_trail_activated', False)
        if tp_trail_active and _in_phase2:
            tp_peak = pos.get('_tp_trail_peak', best_pnl)
            actual_tp = max(tp_peak, best_pnl)
            pos['_tp_trail_peak'] = actual_tp
            _tp_dd = min(trail_dd, 0.5)
            if pnl > 0 and (actual_tp - pnl) >= _tp_dd:
                return True, (f"AUTO_TP_TRAIL: TP→trail, "
                             f"peak={actual_tp:+.2f}%, now={pnl:+.2f}%, "
                             f"drop={actual_tp - pnl:.2f}%")

        # ── F-007: TP in Phase 1 — close on drawdown ─────
        elif tp_trail_active and not _in_phase2:
            tp_peak = pos.get('_tp_trail_peak', best_pnl)
            actual_tp = max(tp_peak, best_pnl)
            pos['_tp_trail_peak'] = actual_tp
            if pnl > 0 and (actual_tp - pnl) >= 0.5:
                return True, (f"AUTO_TP_PHASE1: TP Phase 1, "
                             f"peak={actual_tp:+.2f}%, now={pnl:+.2f}%, "
                             f"drop={actual_tp - pnl:.2f}%")

    # ── 5. PNLSTOP (emergency safety net) ────────────────
    pnl_stop = pos.get('pnl_stop_pct', params.pnl_stop_pct)
    if pnl_stop > sl:
        pnl_stop = sl - 1.0  # must be wider than SL
    if pnl <= pnl_stop:
        return True, f"AUTO_PNLSTOP: P&L={pnl:+.2f}% ≤ {pnl_stop}%"

    # ── 5b. STALE EXIT — «мёртвые» сделки ─────────────────
    # [A16] Если сделка провела >= stale_exit_hours и ни разу не достигла
    # +stale_exit_min_best — mean reversion не работает. Закрываем раньше
    # таймаута, экономя 0.3-0.5% на каждой.
    # Phantom Dual TF: 7/12 TIMEOUT имели best=0, avg PnL=-0.72%.
    _stale_hours = params.stale_exit_hours
    _stale_min_best = params.stale_exit_min_best
    if (_stale_hours > 0
            and hours_in >= _stale_hours
            and pnl < 0
            and best_pnl < _stale_min_best):
        return True, (
            f"AUTO_STALE: {hours_in:.1f}ч, P&L={pnl:+.2f}%, "
            f"best={best_pnl:+.2f}% < {_stale_min_best}% — нет движения"
        )

    # ── 6. TIMEOUT ────────────────────────────────────────
    max_hours = pos.get('max_hold_hours') or params.max_hold_hours
    if hours_in > max_hours:
        return True, f"AUTO_TIMEOUT: {hours_in:.1f}ч > {max_hours}ч"

    return False, None


# ═══════════════════════════════════════════════════════
# HR DRIFT ASSESSMENT — AUTO_HR_DRIFT
# ═══════════════════════════════════════════════════════

@dataclass
class HRDriftParams:
    """Пороги для оценки дрейфа hedge ratio внутри позиции.

    Заполняется из CFG() один раз вызывающим кодом.
    Три исхода: HOLD / REBALANCE / EXIT.
    """
    warn_pct: float = 15.0                 # hr_drift_warn_pct:           drift ≥ warn → REBALANCE
    critical_pct: float = 40.0             # hr_drift_critical_pct:       drift ≥ crit → EXIT
    min_hold_hours: float = 4.0            # hr_kalman_update_hours:      grace period перед первым update
    rebalance_cooldown_hours: float = 4.0  # hr_rebalance_cooldown_hours: мин. интервал между ребалансами


def assess_hr_drift(
    pos: dict,
    new_hr: float,
    new_hr_std: float,
    drift_pct: float,
    hours_in: float,
    params: HRDriftParams,
) -> dict:
    """[A13] AUTO_HR_DRIFT: оценка дрейфа HR → три исхода: HOLD / REBALANCE / EXIT.

    Самостоятельный сигнал, независимый от Z-логики.

    HOLD      (drift < warn_pct):
        Записываем метрику, entry_hr не трогаем.

    REBALANCE (warn_pct ≤ drift < critical_pct):
        Обновляем entry_hr в позиции — нейтральная точка спреда сдвигается.
        На Bybit: если доступны bybit_qty1/bybit_qty2 — физически корректируем
        размер отстающей ноги через rebalance_leg(). Если нет — только метрика.
        PnL и Z после этого считаются от нового "нуля" (нового entry_hr).
        Защита от перерегулирования: cooldown rebalance_cooldown_hours.

    EXIT      (drift ≥ critical_pct):
        Позиция потеряла коинтеграцию, выходим через обычный close_position().

    Pure function: не мутирует pos, не пишет файлы, не вызывает CFG().

    Args:
        pos:        position dict (read-only)
        new_hr:     обновлённый HR от kalman_hr_update()
        new_hr_std: обновлённая неопределённость HR
        drift_pct:  % дрейфа от entry_hr (abs)
        hours_in:   часов в позиции
        params:     HRDriftParams с порогами

    Returns:
        dict:
            'action':       str  — 'HOLD' | 'REBALANCE' | 'EXIT'
            'reason':       str  — строка для лога/алерта
            'patch':        dict — поля для _patch_position() (пустой при HOLD без изменений)
            'should_close': bool — True только при EXIT
    """
    import time as _time

    entry_hr = float(pos.get('entry_hr', 1.0))
    pair = f"{pos.get('coin1', '?')}/{pos.get('coin2', '?')}"

    base_patch = {
        '_hr_drift_pct':   drift_pct,
        '_current_hr':     new_hr,
        '_current_hr_std': new_hr_std,
    }

    # Grace period — не трогать позицию до первого update
    if hours_in < params.min_hold_hours:
        return {
            'action': 'HOLD',
            'reason': '',
            'patch': base_patch,
            'should_close': False,
        }

    # ── EXIT: дрейф критический ──────────────────────────
    if drift_pct >= params.critical_pct:
        return {
            'action': 'EXIT',
            'should_close': True,
            'reason': (
                f"AUTO_HR_DRIFT EXIT: {pair} drift={drift_pct:.1f}% "
                f"≥ {params.critical_pct:.0f}% "
                f"(entry_hr={entry_hr:.4f} → new_hr={new_hr:.4f}, "
                f"hold={hours_in:.1f}ч)"
            ),
            'patch': {
                **base_patch,
                '_hr_drift_action': 'EXIT',
            },
        }

    # ── REBALANCE: дрейф средний ─────────────────────────
    if drift_pct >= params.warn_pct:
        last_rebal_ts = pos.get('_last_rebalance_ts', 0)
        hours_since_rebal = (
            (_time.time() - last_rebal_ts) / 3600.0 if last_rebal_ts else 99.0
        )

        # Cooldown: degradе до HOLD
        if hours_since_rebal < params.rebalance_cooldown_hours:
            return {
                'action': 'HOLD',
                'should_close': False,
                'reason': (
                    f"AUTO_HR_DRIFT: {pair} drift={drift_pct:.1f}% ≥ "
                    f"{params.warn_pct:.0f}% но cooldown "
                    f"{hours_since_rebal:.1f}ч < {params.rebalance_cooldown_hours:.0f}ч"
                ),
                'patch': base_patch,
            }

        # Рассчитываем target qty для физической ребалансировки на бирже.
        # Если bybit_qty1/bybit_qty2 известны — вычисляем дельту.
        # Иначе bybit_rebalance = None (только метрика, entry_hr обновляем).
        bybit_rebalance = None
        bybit_qty1 = float(pos.get('bybit_qty1') or 0)
        bybit_qty2 = float(pos.get('bybit_qty2') or 0)
        if bybit_qty1 > 0 and bybit_qty2 > 0:
            # Нейтральное условие: qty1 / qty2 = new_hr (в ценах это не совсем так,
            # но как первое приближение — ребалансируем leg2 под новый HR).
            # Меняем leg2: target_qty2 = qty1 / new_hr
            if new_hr > 0:
                target_qty2 = bybit_qty1 / new_hr
                direction = pos.get('direction', 'LONG')
                # leg2: LONG → SHORT coin2, SHORT → LONG coin2
                current_side2 = 'Sell' if direction == 'LONG' else 'Buy'
                bybit_rebalance = {
                    'coin': pos.get('coin2', ''),
                    'current_side': current_side2,
                    'current_qty': bybit_qty2,
                    'target_qty': target_qty2,
                }

        return {
            'action': 'REBALANCE',
            'should_close': False,
            'reason': (
                f"AUTO_HR_DRIFT REBALANCE: {pair} drift={drift_pct:.1f}% "
                f"(entry_hr={entry_hr:.4f} → new_hr={new_hr:.4f}, "
                f"hold={hours_in:.1f}ч) — обновляем нейтральную точку спреда"
            ),
            'patch': {
                # Ключевое: entry_hr → PnL и Z считаются от нового HR
                'entry_hr':            new_hr,
                'entry_hr_original':   pos.get('entry_hr_original', entry_hr),
                **base_patch,
                '_hr_drift_action':    'REBALANCE',
                '_last_rebalance_ts':  _time.time(),
                '_rebalance_count':    pos.get('_rebalance_count', 0) + 1,
            },
            # bybit_rebalance: None если нет qty данных, иначе kwargs для rebalance_leg()
            'bybit_rebalance': bybit_rebalance,
        }

    # ── HOLD: дрейф незначительный ───────────────────────
    return {
        'action': 'HOLD',
        'reason': '',
        'patch': base_patch,
        'should_close': False,
    }

