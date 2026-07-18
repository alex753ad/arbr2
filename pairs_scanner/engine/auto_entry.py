"""
engine/auto_entry.py — Валидация и обработка pending входов.

Ноль Streamlit. Вызывается из daemon process_pending().
Извлечено: SEC-03 валидация, ERR-01 TTL, фильтры, duplicate check.
"""

from __future__ import annotations
import re
import os
import time
import logging

_logger = logging.getLogger("engine.auto_entry")


def validate_pending(data: dict) -> tuple[bool, str]:
    """SEC-03 FIX: валидация полей pending JSON.

    Returns: (valid: bool, reason: str)
    """
    if not isinstance(data, dict):
        return False, "не dict"
    if not data.get('coin1') or not data.get('coin2'):
        return False, "нет coin1/coin2"
    if data.get('direction', '') not in ('LONG', 'SHORT'):
        return False, f"невалидный direction={data.get('direction')}"
    try:
        ez = float(data.get('entry_z', 0))
        if not (0 < abs(ez) < 15):
            return False, f"entry_z={ez} вне [0,15]"
    except (ValueError, TypeError):
        return False, "entry_z невалиден"
    try:
        ehr = float(data.get('entry_hr', 0))
        if abs(ehr) > 100:
            return False, f"entry_hr={ehr} > 100"
    except (ValueError, TypeError):
        return False, "entry_hr невалиден"
    if not re.match(r'^[A-Za-z0-9]{2,10}$', str(data.get('coin1', ''))):
        return False, "coin1 недопустимые символы"
    if not re.match(r'^[A-Za-z0-9]{2,10}$', str(data.get('coin2', ''))):
        return False, "coin2 недопустимые символы"
    return True, "OK"


def check_pending_ttl(filepath: str, ttl_seconds: int = 7200) -> tuple[bool, str]:
    """ERR-01 FIX: проверить TTL pending файла.

    Returns: (expired: bool, reason: str)
    """
    try:
        age_sec = time.time() - os.path.getmtime(filepath)
        if age_sec > ttl_seconds:
            return True, f"TTL expired ({age_sec/60:.0f} мин > {ttl_seconds//60} мин)"
    except Exception:
        pass
    return False, ""


def check_entry_allowed(
    pair: str,
    direction: str,
    entry_label: str,
    open_pairs: set,
    cfg_fn=None,
) -> tuple[bool, str]:
    """Pre-entry checks: duplicates, limits, cooldowns, whitelist.

    Uses core/risk functions for cooldown/daily/cascade/memory checks.
    Returns: (blocked: bool, reason: str)
    """
    if cfg_fn is None:
        try:
            from ..infra.config import CFG
            cfg_fn = CFG
        except ImportError:
            from config_loader import CFG
            cfg_fn = CFG

    # 1. Duplicate
    if pair in open_pairs:
        return True, f"{pair} уже открыта"

    # 2. Max positions
    max_pos = int(cfg_fn('monitor', 'max_positions', 20))
    if len(open_pairs) >= max_pos:
        return True, f"лимит позиций: {len(open_pairs)}/{max_pos}"

    # 3. Daily loss
    try:
        from ..core.risk import check_daily_loss_limit
        from ..infra.storage import load_cooldowns, load_open_positions
        from ..core.utils import today_msk_str
        cd = load_cooldowns()
        live_pnls = [p.get('pnl_pct', 0) for p in load_open_positions()
                     if p.get('pnl_pct', 0) < 0]
        blocked, reason = check_daily_loss_limit(
            cd, live_pnls,
            daily_loss_limit_pct=cfg_fn('monitor', 'daily_loss_limit_pct', -10.0),
            today_str=today_msk_str(),
        )
        if blocked:
            return True, reason
    except (ImportError, Exception) as e:
        _logger.debug("daily_loss check skipped: %s", e)

    # 4. Pair cooldown
    try:
        from ..core.risk import check_pair_cooldown
        from ..infra.storage import load_cooldowns
        cd = load_cooldowns()
        blocked, reason = check_pair_cooldown(
            pair, cd, entry_label=entry_label,
            cooldown_after_sl_hours=cfg_fn('monitor', 'cooldown_after_sl_hours', 12),
            cooldown_after_2sl_hours=cfg_fn('monitor', 'cooldown_after_2sl_hours', 12),
            pair_cooldown_hours=cfg_fn('monitor', 'pair_cooldown_hours', 4),
        )
        if blocked:
            return True, reason
    except (ImportError, Exception) as e:
        _logger.debug("cooldown check skipped: %s", e)

    # 5. Cascade SL
    try:
        from ..core.risk import check_cascade_sl
        from ..infra.storage import load_cooldowns
        cd = load_cooldowns()
        blocked, reason = check_cascade_sl(
            cd,
            cascade_enabled=cfg_fn('monitor', 'cascade_sl_enabled', True),
            window_hours=cfg_fn('monitor', 'cascade_sl_window_hours', 2),
            threshold=int(cfg_fn('monitor', 'cascade_sl_threshold', 3)),
            pause_hours=cfg_fn('monitor', 'cascade_sl_pause_hours', 1),
        )
        if blocked:
            return True, reason
    except (ImportError, Exception) as e:
        _logger.debug("cascade check skipped: %s", e)

    # 6. Pair memory
    try:
        from ..core.risk import pair_memory_is_blocked
        from ..infra.storage import pair_memory_get
        mem = pair_memory_get(pair)
        blocked, reason = pair_memory_is_blocked(pair, mem)
        if blocked:
            return True, reason
    except (ImportError, Exception) as e:
        _logger.debug("pair_memory check skipped: %s", e)

    # 7. Whitelist
    try:
        from ..core.risk import is_whitelisted
        parts = pair.split('/')
        if len(parts) == 2:
            wl_enabled = cfg_fn('strategy', 'whitelist_enabled', True)
            if wl_enabled and not is_whitelisted(parts[0], parts[1], direction, None):
                return True, f"{pair} не в whitelist"
    except (ImportError, Exception) as e:
        _logger.debug("whitelist check skipped: %s", e)

    return False, ""


def load_filters_state(base_dir: str) -> dict:
    """BUG-10 FIX: load filters_state.json (written by UI).
    Returns: dict of filter flags, default all False.
    """
    import json
    _keys = [
        'block_green', 'block_green_bt_fail', 'block_green_bt_warn',
        'block_yellow', 'block_yellow_bt_fail', 'block_yellow_bt_warn',
        'block_wl_warn', 'block_wl_fail', 'block_nk_warn', 'block_nk_fail',
        'block_long', 'block_short',
    ]
    state = {k: False for k in _keys}
    path = os.path.join(base_dir, 'filters_state.json')
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in _keys:
                    if k in data:
                        state[k] = bool(data[k])
    except Exception:
        pass
    return state


# ═══════════════════════════════════════════════════════
# BTC Z-SCORE DIRECTIONAL FILTER
# ═══════════════════════════════════════════════════════

def get_btc_z_from_file(base_dir: str) -> float:
    """Read current BTC Z-score from rally_state.json.
    Returns 0.0 if file doesn't exist or can't be read.
    """
    import json
    path = os.path.join(base_dir, 'rally_state.json')
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return float(data.get('btc_z', 0))
    except Exception:
        pass
    return 0.0


def check_btc_direction_filter(
    btc_z: float,
    direction: str,
    long_block_z: float = 2.0,
    short_block_z: float = -2.0,
) -> tuple[bool, str]:
    """Block LONGs when BTC Z > threshold (BTC rally),
    block SHORTs when BTC Z < threshold (BTC dump).

    Логика: при сильном движении BTC пары теряют коинтеграцию.
    LONG при BTC Z>2 → рынок перегрет, пары расходятся.
    SHORT при BTC Z<-2 → рынок в панике, пары расходятся.

    Returns: (blocked: bool, reason: str)
    """
    if direction == 'LONG' and btc_z > long_block_z:
        return True, (
            f"🚫 BTC Z={btc_z:+.2f} > {long_block_z:+.1f} — "
            f"LONG заблокирован (BTC rally, пары расходятся)"
        )
    if direction == 'SHORT' and btc_z < short_block_z:
        return True, (
            f"🚫 BTC Z={btc_z:+.2f} < {short_block_z:+.1f} — "
            f"SHORT заблокирован (BTC dump, пары расходятся)"
        )
    return False, ""


def check_entry_z_min(pending_data: dict, cfg_fn=None) -> tuple[bool, str]:
    """[A12] Блокирует LONG и SHORT с |Entry Z| < entry_z_min (default 2.5).

    Работает параллельно с min_z_long:
      - min_z_long=3.0  → блокирует только LONG с |Z| < 3.0
      - entry_z_min=2.5 → блокирует ВСЁ (LONG и SHORT) с |Z| < 2.5

    Сделки с |Z| < 2.5 убыточны во всех фазах:
      - До v45: avg=-0.32%
      - v45 hybrid: avg=-0.60%

    Args:
        pending_data: dict с полями 'entry_z' и 'direction'
        cfg_fn: callable CFG (если None — импортируется автоматически)

    Returns:
        (passed: bool, reason: str)
        passed=True  → вход разрешён
        passed=False → вход заблокирован, reason содержит причину
    """
    if cfg_fn is None:
        try:
            from ..infra.config import CFG
            cfg_fn = CFG
        except ImportError:
            from config_loader import CFG
            cfg_fn = CFG

    entry_z_min = float(cfg_fn('strategy', 'entry_z_min', 0.0))

    if entry_z_min <= 0:
        return True, ""

    try:
        entry_z = float(pending_data.get('entry_z', 0) or 0)
    except (ValueError, TypeError):
        return True, ""  # невалидный z уже поймает validate_pending

    abs_z = abs(entry_z)
    direction = str(pending_data.get('direction', '')).upper()

    if abs_z < entry_z_min:
        reason = (
            f"{direction} |Z|={abs_z:.2f} < entry_z_min={entry_z_min:.1f} "
            f"(ANALYSIS-v47: сделки с |Z|<{entry_z_min:.1f} убыточны во всех фазах)"
        )
        return False, reason

    return True, ""


def check_scanner_size(imp: dict) -> tuple[bool, float, str]:
    """Verify pending has scanner-recommended size (not default fallback).

    Returns: (valid: bool, size: float, reason: str)
    """
    size = imp.get('risk_size_usdt', imp.get('recommended_size', 0))
    try:
        size = float(size)
    except (TypeError, ValueError):
        size = 0

    if size <= 0:
        return False, 0, "нет risk_size_usdt от сканера — размер не определён"
    if size < 10:
        return False, size, f"risk_size_usdt=${size:.0f} < $10 — слишком мало"

    return True, size, "OK"
