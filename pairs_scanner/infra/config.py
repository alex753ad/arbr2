"""
infra/config.py — Единый конфиг для всей системы.

Извлечено из config_loader.py (Волна 3).
Содержит ТОЛЬКО загрузку/хранение конфига: _DEFAULTS, CFG(), CFG_reload(), CFG_auto_reload().
Бизнес-логика (pair_memory, pattern_analysis, ml_score и т.д.) остаётся в config_loader.py.

Потокобезопасность: RLock (BUG-006 FIX).
Автоперезагрузка: mtime check при каждом вызове CFG_auto_reload().
"""

import os
import threading
import logging

_logger = logging.getLogger("infra.config")

# ═══════════════════════════════════════════════════════
# DEFAULTS — единственный источник дефолтных значений.
# Синхронизированы с config.yaml (BUG-04, BUG-07 FIX).
# ═══════════════════════════════════════════════════════

_DEFAULTS = {
    'strategy': {
        'entry_z': 2.5, 'exit_z': 0.5, 'stop_z_offset': 2.0, 'min_stop_z': 4.0,
        'take_profit_pct': 2.0, 'stop_loss_pct': -5.0, 'max_hold_hours': 16,
        'micro_bt_max_bars': 6, 'min_hurst': 0.45, 'warn_hurst': 0.48,
        'min_correlation': 0.20, 'hr_naked_threshold': 0.15, 'max_pvalue': 0.15,
        'max_hr_threshold': 5.0, 'min_hr_threshold': 0.05,
        'commission_pct': 0.10, 'slippage_pct': 0.05,
        'bt_filter_mode': 'HARD',
        'wf_filter_mode': 'SOFT',
        'ubt_filter_mode': 'HARD',
        'adaptive_tp': True,
        'whitelist_enabled': True,
        'short_only': False,
        'use_hurst_ema_fallback': True,
        'entry_label_hard_block': True,
        'entry_label_block_list': ['ЖДАТЬ'],
        'entry_label_allow_manual': True,
        'conviction_gate_enabled': True,
        'conviction_gate_min': 0.5,
    },
    'scanner': {
        'coins_limit': 100, 'timeframe': '4h', 'lookback_days': 50,
        'exchange': 'bybit', 'refresh_interval_min': 10,
        'min_quality': 60,
        'pass_bt_metrics': True, 'max_same_coin_signals': 3,
        'top_n_coins': 70,
        'max_halflife_hours': 28,
    },
    'monitor': {
        'refresh_interval_sec': 60, 'exit_z_target': 0.5,
        'pnl_stop_pct': -10.0,
        'trailing_z_bounce': 0.8, 'time_warning_ratio': 1.0, 'time_exit_ratio': 1.5,
        'time_critical_ratio': 2.0, 'overshoot_deep_z': 1.0,
        'pnl_trailing_threshold': 0.5, 'pnl_trailing_fraction': 0.4,
        'hurst_critical': 0.50, 'hurst_warning': 0.48, 'hurst_border': 0.45,
        'pvalue_warning': 0.10, 'correlation_warning': 0.20,
        'auto_exit_enabled': True, 'auto_tp_pct': 2.0, 'auto_sl_pct': -3.0,
        'auto_exit_z': 0.3, 'auto_exit_z_min_pnl': 0.5,
        'auto_exit_z_mode': 'TRAIL',
        'trailing_enabled': True, 'trailing_activate_pct': 1.5, 'trailing_drawdown_pct': 0.7,
        'auto_flip_enabled': True, 'pair_cooldown_hours': 4,
        'pair_loss_limit_pct': -2.5, 'coin_loss_warn_pct': -3.0,
        'daily_loss_limit_pct': -10.0,
        'cooldown_after_sl_hours': 12,
        'cooldown_after_2sl_hours': 12,
        'cascade_sl_enabled': True,
        'cascade_sl_window_hours': 2,
        'cascade_sl_threshold': 3,
        'cascade_sl_pause_hours': 1,
        'phase2_pnl_fallback': 1.2,
        'phase2_hours_fallback': 6.0,
        'z_trail_activate': 0.3,
        'z_trail_drawdown': 0.5,
        'hr_drift_warn_pct': 15,
        'hr_drift_critical_pct': 40,
        'max_positions': 20, 'max_coin_exposure': 4,
        'max_coin_positions': 2,
        'entry_grace_minutes': 5,
        'phantom_track_hours': 12,
        'max_hold_hours': 16,
        'two_phase_exit': True,
        'phase1_z_threshold': 0.5,
        'phase2_trail_activate': 0.8,
        'phase2_trail_drawdown': 0.4,
        'phantom_autocalibrate': True,
        'phantom_autocalibrate_min_trades': 5,
        'phantom_autocalibrate_left_threshold': 2.0,
        'recovery_trail_threshold': 0.5,
        'recovery_trail_drawdown': 0.3,
    },
    'position_sizing': {
        'enabled': True, 'base_size': 100, 'max_multiplier': 2.0,
        'min_size': 50, 'max_size': 250,
        'signal_entry_bonus': 0.5, 'high_z_bonus': 0.3,
        'bt_pass_bonus': 0.2, 'good_volume_bonus': 0.2,
        'bt_fail_penalty': -0.5,
    },
    'volatility_regime': {
        'enabled': True, 'btc_atr_window': 14, 'btc_atr_timeframe': '1h',
        'normal_atr_max': 2.5, 'elevated_atr_max': 4.0,
        'elevated_sl_multiplier': 1.3, 'elevated_size_multiplier': 0.7,
        'extreme_block_entries': True,
    },
    'risk': {
        'max_positions': 5,
        'max_per_trade_pct': 20,
        'min_per_trade_pct': 5,
        'max_total_exposure_pct': 80,
        'portfolio_usdt': 1000,
    },
    'bybit': {
        'enabled': False,
        'api_key': '',
        'api_secret': '',
        'base_url': 'https://api-demo.bybit.com',
        'demo_mirror': False,
        'leverage': 1,
        'size_pct': 100,
        'proxy': '',
    },
    'backtester': {
        'n_bars': 300, 'max_bars': 50, 'min_bars': 2,
        'n_folds_wf': 3, 'train_pct': 0.65,
    },
    'z_velocity': {
        'lookback': 5, 'excellent_min_vel': 0.1, 'decel_threshold': 0.05,
    },
    'rally_filter': {
        'warning_z': 2.0, 'block_z': 2.5, 'exit_z': 0.0, 'cooldown_bars': 2,
    },
}


# ═══════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════

_config_data = None
_config_path = None
_config_mtime = 0
_config_lock = threading.RLock()


# ═══════════════════════════════════════════════════════
# LOADING
# ═══════════════════════════════════════════════════════

def _merge(user_cfg):
    """Merge user config over defaults."""
    for section, vals in user_cfg.items():
        if isinstance(vals, dict) and section in _config_data:
            _config_data[section].update(vals)
        elif isinstance(vals, dict):
            _config_data[section] = vals


def _parse_simple(path):
    """Parse YAML without PyYAML (handles simple flat sections)."""
    result = {}
    section = None
    _depth_warned = False
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.rstrip()
            if not s or s.lstrip().startswith('#'):
                continue
            indent = len(s) - len(s.lstrip())
            s = s.strip()
            if indent >= 8 and not _depth_warned and ':' in s and not s.endswith(':'):
                _logger.warning(
                    "⚠️ %s содержит YAML с вложенностью >2 уровней. "
                    "Установите PyYAML: pip install pyyaml", path)
                _depth_warned = True
            if indent == 0 and s.endswith(':'):
                section = s[:-1]
                result[section] = {}
            elif ':' in s and section is not None:
                k, v = s.split(':', 1)
                k, v = k.strip(), v.strip()
                if '#' in v:
                    v = v[:v.index('#')].strip()
                if v.startswith('[') and v.endswith(']'):
                    inner = v[1:-1].strip()
                    v = [x.strip().strip("'\"") for x in inner.split(',') if x.strip()] if inner else []
                elif v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                elif v.startswith("'") and v.endswith("'"):
                    v = v[1:-1]
                elif v.lower() in ('true', 'yes'):
                    v = True
                elif v.lower() in ('false', 'no'):
                    v = False
                else:
                    try:
                        v = int(v)
                    except ValueError:
                        try:
                            v = float(v)
                        except ValueError:
                            pass
                result[section][k] = v
    return result


def _load():
    """Load config once (lazy singleton). Thread-safe."""
    global _config_data, _config_path, _config_mtime
    if _config_data is not None:
        return
    with _config_lock:
        if _config_data is not None:
            return
        _config_data = {}
        for section, vals in _DEFAULTS.items():
            _config_data[section] = dict(vals)

        _dir = os.path.dirname(os.path.abspath(__file__))
        # Search in infra/, then parent (project root)
        _project_root = os.path.dirname(os.path.dirname(_dir))
        paths = [
            'config.yaml',
            os.path.join(_project_root, 'config.yaml'),
            os.path.join(_dir, '..', '..', 'config.yaml'),
            'config_streamlit.yaml',
            'config_local.yaml',
        ]
        for path in paths:
            if os.path.exists(path):
                # A-03 FIX: permissions
                try:
                    import stat
                    _mode = os.stat(path).st_mode
                    if _mode & stat.S_IROTH:
                        try:
                            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                        except OSError:
                            pass
                except Exception:
                    pass
                try:
                    import yaml
                    with open(path, 'r', encoding='utf-8') as f:
                        user = yaml.safe_load(f) or {}
                    _merge(user)
                    _config_path = path
                    try:
                        _config_mtime = os.path.getmtime(path)
                    except OSError:
                        pass
                    return
                except ImportError:
                    _logger.warning(
                        "⚠️ PyYAML не установлен — fallback-парсер для %s. "
                        "pip install pyyaml", path)
                    _merge(_parse_simple(path))
                    _config_path = path
                    try:
                        _config_mtime = os.path.getmtime(path)
                    except OSError:
                        pass
                    return
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════

def CFG(section, key=None, default=None):
    """Get config value. Thread-safe, lazy-loading."""
    _load()
    if key is None:
        return _config_data.get(section, {})
    return _config_data.get(section, {}).get(key, default)


def CFG_path():
    """Return path to loaded config file (or None)."""
    _load()
    return _config_path


def _reload_unlocked():
    global _config_data
    _config_data = None


def CFG_reload():
    """Force reload from disk."""
    with _config_lock:
        _reload_unlocked()
    _load()


def CFG_auto_reload():
    """Reload config if file changed on disk. Returns True if reloaded."""
    global _config_mtime
    _load()
    if _config_path is None:
        return False
    try:
        current_mtime = os.path.getmtime(_config_path)
        _need_reload = False
        with _config_lock:
            if current_mtime != _config_mtime:
                _config_mtime = current_mtime
                _reload_unlocked()
                _need_reload = True
        if _need_reload:
            _load()
            return True
    except OSError:
        pass
    return False


def get_defaults():
    """Return a copy of _DEFAULTS for inspection/testing."""
    return {k: dict(v) for k, v in _DEFAULTS.items()}
