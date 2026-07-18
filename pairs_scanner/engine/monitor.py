"""
engine/monitor.py — Мониторинг позиций: daemon-side orchestration.

Ноль Streamlit. Daemon импортирует build_exit_params + run_monitor_tick.
Заменяет exec()-загрузку monitor_v38_3.py для auto-exit логики.
"""

from __future__ import annotations
from ..core.position_manager import check_auto_exit, ExitParams

TRAIL_KEYS = (
    '_z_trail_activated', '_z_trail_peak',
    '_tp_trail_activated', '_tp_trail_peak',
    '_recovery_trail_activated', '_recovery_trail_peak',
    'exit_phase',
    '_trail_params_locked', '_trail_act_locked', '_trail_dd_locked',
)


def build_exit_params(pos: dict, cfg_fn=None,
                      pair_tp: float | None = None,
                      pair_sl: float | None = None,
                      phantom_cal: dict | None = None) -> ExitParams:
    """Construct ExitParams from CFG() + per-pair overrides.

    cfg_fn: callable CFG(section, key, default). Default: infra.config.CFG.
    """
    if cfg_fn is None:
        try:
            from ..infra.config import CFG
            cfg_fn = CFG
        except ImportError:
            from config_loader import CFG
            cfg_fn = CFG

    def c(section, key, default):
        return cfg_fn(section, key, default)

    _tp = pair_tp or pos.get('pair_tp_pct') or c('monitor', 'auto_tp_pct', 2.0)
    _sl = pair_sl or pos.get('pair_sl_pct') or c('monitor', 'auto_sl_pct', -3.0)

    _ph_act = None
    _ph_dd = None
    if phantom_cal and phantom_cal.get('adjusted'):
        _ph_act = phantom_cal.get('trail_activate')
        _ph_dd = phantom_cal.get('trail_drawdown')

    return ExitParams(
        entry_grace_minutes=c('monitor', 'entry_grace_minutes', 5.0),
        default_tp_pct=float(_tp),
        default_sl_pct=float(_sl),
        trailing_enabled=c('monitor', 'trailing_enabled', True),
        trailing_activate_pct=c('monitor', 'trailing_activate_pct', 1.5),
        trailing_drawdown_pct=c('monitor', 'trailing_drawdown_pct', 0.7),
        auto_exit_z=c('monitor', 'auto_exit_z', 0.3),
        auto_exit_z_min_pnl=c('monitor', 'auto_exit_z_min_pnl', 0.5),
        auto_exit_z_mode=c('monitor', 'auto_exit_z_mode', 'TRAIL'),
        z_trail_drawdown=c('monitor', 'z_trail_drawdown', 0.5),
        recovery_trail_threshold=c('monitor', 'recovery_trail_threshold', 0.5),
        recovery_trail_drawdown=c('monitor', 'recovery_trail_drawdown', 0.3),
        two_phase_exit=c('monitor', 'two_phase_exit', True),
        phase1_z_threshold=c('monitor', 'phase1_z_threshold', 0.5),
        phase2_trail_activate=c('monitor', 'phase2_trail_activate', 0.8),
        phase2_trail_drawdown=c('monitor', 'phase2_trail_drawdown', 0.4),
        phase2_pnl_fallback=c('monitor', 'phase2_pnl_fallback', 1.5),
        phase2_hours_fallback=c('monitor', 'phase2_hours_fallback', 6.0),
        pnl_stop_pct=pos.get('pnl_stop_pct', c('monitor', 'pnl_stop_pct', -10.0)),
        max_hold_hours=float(pos.get('max_hold_hours', c('strategy', 'max_hold_hours', 16))),
        phantom_trail_activate=_ph_act,
        phantom_trail_drawdown=_ph_dd,
    )


def build_trail_patch(pos: dict) -> dict:
    """Extract trail state keys that check_auto_exit may have mutated."""
    return {k: pos[k] for k in TRAIL_KEYS if k in pos}


def run_monitor_tick(
    pos: dict,
    mon: dict,
    params: ExitParams | None = None,
    cfg_fn=None,
    pair_tp: float | None = None,
    pair_sl: float | None = None,
    phantom_cal: dict | None = None,
) -> dict:
    """Run one monitoring tick for one position.

    Returns: {
        'should_close': bool,
        'reason': str | None,
        'pnl': float,
        'best_pnl': float,
        'trail_patch': dict,
        'best_pnl_patch': dict | None,
    }
    """
    pnl = mon.get('pnl_pct', 0)
    best_pnl = mon.get('best_pnl', pnl)

    # 1. best_pnl
    new_best = max(best_pnl, pnl)
    best_patch = None
    if new_best > pos.get('best_pnl_during_trade', 0):
        pos['best_pnl_during_trade'] = new_best
        pos['best_pnl'] = new_best
        best_patch = {'best_pnl_during_trade': new_best, 'best_pnl': new_best}

    # 2. params
    if params is None:
        params = build_exit_params(pos, cfg_fn, pair_tp, pair_sl, phantom_cal)

    # 3. check_auto_exit
    should_close, reason = check_auto_exit(pos, mon, params,
                                            pair_tp=pair_tp, pair_sl=pair_sl)
    # 4. trail patch
    trail_patch = build_trail_patch(pos)

    return {
        'should_close': should_close,
        'reason': reason,
        'pnl': pnl,
        'best_pnl': new_best,
        'trail_patch': trail_patch,
        'best_pnl_patch': best_patch,
    }
