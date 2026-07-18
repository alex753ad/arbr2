#!/usr/bin/env python3
"""
monitor_daemon.py — Полный headless мониторинг (открытие + закрытие)
Совместим с monitor_v38_3.py (Фаза 1 + 2 + 3)

Запуск: python monitor_daemon.py

Переменные окружения (опционально):
  TG_TOKEN    — токен Telegram-бота
  TG_CHAT_ID  — chat_id
  EXCHANGE    — биржа (по умолчанию из config.yaml)
  INTERVAL    — интервал в секундах
"""
import sys, os, time, logging, traceback, json, signal, threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pages'))
os.environ['MONITOR_DAEMON'] = '1'

LOG_FILE = 'monitor_daemon.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('daemon')

# ── HEARTBEAT: отдельный поток пишет в лог каждые 30с ────────────────────────
# Если daemon завис — последняя запись heartbeat покажет что он ещё жив,
# а _current_op покажет на каком шаге застрял.
_current_op = 'init'          # текущая операция (обновляется перед каждым шагом)
_cycle_start_ts = 0.0         # время начала текущего цикла

def _heartbeat_worker():
    """Пишет в лог каждые 30с: жив ли daemon и на каком шаге."""
    import time as _hb_time
    while True:
        _hb_time.sleep(30)
        try:
            _elapsed = _hb_time.time() - _cycle_start_ts if _cycle_start_ts > 0 else 0
            log.info('[HB] alive | op="%s" | цикл идёт %.0fс', _current_op, _elapsed)
        except Exception:
            pass

_hb_thread = threading.Thread(target=_heartbeat_worker, daemon=True, name='heartbeat')
_hb_thread.start()

def _set_op(op: str):
    """Обновить текущую операцию для heartbeat."""
    global _current_op
    _current_op = op
    log.info('[TRACE] >> %s', op)

import unittest.mock as _mock

_st_mock = _mock.MagicMock()
_session_state_store = {}

def _ss_get(key, default=None):
    return _session_state_store.get(key, default)

def _ss_getitem(*args):
    return _session_state_store.get(args[-1])

def _ss_setitem(*args):
    k, v = (args[1], args[2]) if len(args) == 3 else (args[0], args[1])
    _session_state_store[k] = v

def _ss_contains(*args):
    return args[-1] in _session_state_store

_st_mock.session_state = _mock.MagicMock()
_st_mock.session_state.get          = _ss_get
_st_mock.session_state.__getitem__  = _ss_getitem
_st_mock.session_state.__setitem__  = _ss_setitem
_st_mock.session_state.__contains__ = _ss_contains

_st_mock.number_input.return_value       = 0.0
_st_mock.text_input.return_value         = ''
_st_mock.text_area.return_value          = ''
_st_mock.selectbox.return_value          = None
_st_mock.multiselect.return_value        = []
_st_mock.checkbox.return_value           = False
_st_mock.button.return_value             = False
_st_mock.form_submit_button.return_value = False
_st_mock.radio.return_value              = None
_st_mock.slider.return_value             = 0
_st_mock.date_input.return_value         = None
_st_mock.file_uploader.return_value      = None
_st_mock.color_picker.return_value       = '#000000'
_st_mock.cache_data  = lambda *a, **kw: (lambda f: f)
_st_mock.rerun       = lambda *a, **kw: None
_st_mock.toast       = lambda *a, **kw: None
_st_mock.spinner     = _mock.MagicMock(return_value=_mock.MagicMock(
    __enter__=lambda s, *a: None, __exit__=lambda s, *a: None))
_st_mock.expander    = _mock.MagicMock(return_value=_mock.MagicMock(
    __enter__=lambda s, *a: s,   __exit__=lambda s, *a: None))
_st_mock.form        = _mock.MagicMock(return_value=_mock.MagicMock(
    __enter__=lambda s, *a: s,   __exit__=lambda s, *a: None))
_st_mock.container   = _mock.MagicMock(return_value=_mock.MagicMock(
    __enter__=lambda s, *a: s,   __exit__=lambda s, *a: None))

def _st_columns(spec, *a, **kw):
    n = spec if isinstance(spec, int) else len(spec)
    return [_mock.MagicMock(__enter__=lambda s,*a:s, __exit__=lambda s,*a:None)
            for _ in range(n)]

def _st_tabs(labels, *a, **kw):
    return [_mock.MagicMock(__enter__=lambda s,*a:s, __exit__=lambda s,*a:None)
            for _ in labels]

_st_mock.columns = _st_columns
_st_mock.tabs    = _st_tabs

_st_mock.sidebar = _mock.MagicMock()
_st_mock.sidebar.number_input.return_value = 0.0
_st_mock.sidebar.text_input.return_value   = ''
_st_mock.sidebar.selectbox.return_value    = None
_st_mock.sidebar.checkbox.return_value     = False
_st_mock.sidebar.button.return_value       = False
_st_mock.sidebar.slider.return_value       = 0
_st_mock.sidebar.radio.return_value        = None
_st_mock.sidebar.multiselect.return_value  = []

sys.modules['streamlit'] = _st_mock

# ── Загрузка monitor_v38_3 через importlib (не exec!) ─────────────────────
# v46 FIX: exec() заменён на importlib.util.spec_from_file_location.
# Преимущества: (1) IDE видит symbols, (2) трассировки стека корректны,
# (3) нет зависимости от текстовых маркеров, (4) полный модуль загружается.
# _DAEMON_MODE=True → блок `if not _DAEMON_MODE:` пропускает весь UI код.
log.info('Загружаю monitor_v38_3 через importlib...')
try:
    import importlib.util

    _monitor_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'pages', 'monitor_v38_3.py')
    if not os.path.exists(_monitor_path):
        _monitor_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'monitor_v38_3.py')

    _spec = importlib.util.spec_from_file_location('monitor_v38_3', _monitor_path)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules['monitor_v38_3'] = _mod
    _spec.loader.exec_module(_mod)

    # Functions still coupled to monitor (monitor_position, add/close, entry filters)
    monitor_position      = _mod.monitor_position
    add_position          = _mod.add_position
    close_position        = _mod.close_position
    check_entry_filters   = _mod.check_entry_filters
    diag_log_refusal      = _mod.diag_log_refusal
    diag_log_attempt      = _mod.diag_log_attempt
    _positions_write_lock = getattr(_mod, '_positions_write_lock', None)

    # Path constants
    _MONITOR_IMPORT_DIR   = _mod._MONITOR_IMPORT_DIR
    _BASE_DIR             = _mod._BASE_DIR

    log.info('monitor_v38_3 загружен (importlib, %d строк)', 
             sum(1 for _ in open(_monitor_path, encoding='utf-8')))

except Exception as e:
    log.critical('Импорт monitor_v38_3 не удался: %s', e, exc_info=True)
    sys.exit(1)

# ── pairs_scanner: прямые импорты (не через exec) ─────────────────────────
# Функции, уже вынесенные в core/infra/engine, импортируются напрямую.
# Fallback на monitor модуль если pairs_scanner недоступен.
try:
    from pairs_scanner.core.utils import now_msk
    from pairs_scanner.core.position_manager import check_auto_exit, ExitParams
    from pairs_scanner.engine.monitor import build_exit_params, run_monitor_tick
    from pairs_scanner.engine.auto_entry import validate_pending, check_pending_ttl, load_filters_state
    from pairs_scanner.infra.notifications import send_telegram
    from pairs_scanner.infra.storage import (
        load_positions, save_positions, update_position as _storage_update,
        load_open_positions,
    )
    from pairs_scanner.infra.exchange import get_current_price, fetch_prices as _fetch_prices

    _PAIRS_SCANNER_OK = True
    log.info('pairs_scanner: прямой импорт OK (core + infra + engine)')
except ImportError as _ps_err:
    # Fallback: всё из monitor модуля
    _PAIRS_SCANNER_OK = False
    log.warning('pairs_scanner недоступен (%s), fallback на monitor', _ps_err)
    load_positions    = _mod.load_positions
    save_positions    = _mod.save_positions
    check_auto_exit   = _mod.check_auto_exit
    get_current_price = _mod.get_current_price
    now_msk           = _mod.now_msk
    send_telegram     = _mod.send_telegram

    def load_open_positions():
        return [p for p in load_positions()
                if isinstance(p, dict) and p.get('status') == 'OPEN']

from config_loader import CFG, CFG_auto_reload
log.info('Импорт завершён')

# ── MEM-FIX-v2: Кешируем adaptive модули на уровне процесса (не перезагружаем каждый тик) ──
_aex_mod = None   # adaptive_exits
_aq_mod  = None   # adaptive_quality

def _load_adaptive_modules():
    """Загружает adaptive_exits и adaptive_quality один раз при старте.
    Повторно вызывается только при явной необходимости (не в каждом цикле)."""
    global _aex_mod, _aq_mod
    _base = os.path.dirname(os.path.abspath(__file__))

    if _aex_mod is None:
        try:
            import importlib.util as _ilu
            _p = os.path.join(_base, 'adaptive_exits.py')
            if os.path.exists(_p):
                _spec = _ilu.spec_from_file_location('adaptive_exits', _p)
                _m = _ilu.module_from_spec(_spec)
                sys.modules['adaptive_exits'] = _m
                _spec.loader.exec_module(_m)
                _aex_mod = _m
                log.info('adaptive_exits загружен (кеш)')
        except Exception as _e:
            log.debug('adaptive_exits load skip: %s', _e)

    if _aq_mod is None:
        try:
            import importlib.util as _ilu
            _p = os.path.join(_base, 'adaptive_quality.py')
            if os.path.exists(_p):
                _spec = _ilu.spec_from_file_location('adaptive_quality', _p)
                _m = _ilu.module_from_spec(_spec)
                sys.modules['adaptive_quality'] = _m
                _spec.loader.exec_module(_m)
                _aq_mod = _m
                log.info('adaptive_quality загружен (кеш)')
        except Exception as _e:
            log.debug('adaptive_quality load skip: %s', _e)

_load_adaptive_modules()

MSK = timezone(timedelta(hours=3))

EXCHANGE = os.environ.get('EXCHANGE', CFG('scanner', 'exchange', 'bybit'))
INTERVAL = int(os.environ.get('INTERVAL', CFG('monitor', 'refresh_interval_sec', 60)))
TG_TOKEN   = os.environ.get('TG_TOKEN',   '')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')

# Bybit executor (daemon-side) — читает из config.yaml напрямую
_bybit_exec = None
try:
    if CFG('bybit', 'enabled', False) and CFG('bybit', 'auto_execute', False):
        from bybit_executor import get_executor as _bybit_exec_fn
        _bybit_exec = _bybit_exec_fn()
        if _bybit_exec and _bybit_exec.enabled:
            log.info('Bybit executor: подключён (Demo Trading)')
        else:
            _bybit_exec = None
            log.info('Bybit executor: нет ключей')
    else:
        log.info('Bybit executor: отключён (bybit.auto_execute=false)')
except Exception as _be:
    _bybit_exec = None
    log.warning('Bybit executor init: %s', _be)

def _daemon_bybit_open(coin1, coin2, direction, size_usdt, p1=None, p2=None):
    if _bybit_exec is None:
        return None
    try:
        # [A25] Параметры исполнения из config:
        # limit_wait_sec=15 (было 5), limit_offset_pct=0.05% (aggressive),
        # entry_market_fallback=false (нет маркет fallback → экономия slippage).
        _lws = int(CFG('bybit', 'limit_wait_sec', 15))
        _lop = float(CFG('bybit', 'limit_offset_pct', 0.05))
        _emf = bool(CFG('bybit', 'entry_market_fallback', False))
        # BYBIT-THREAD-FIX: вызов в отдельном потоке с жёстким таймаутом.
        # open_pair_trade_smart_limit ждёт fill до limit_wait_sec секунд —
        # при нескольких pending это блокирует главный поток на limit_wait_sec × N.
        # Таймаут = limit_wait_sec + 30с (запас на HTTP round-trips).
        _hard_timeout = _lws + 30
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
        with ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(
                _bybit_exec.open_pair_trade_smart_limit,
                coin1, coin2, direction, size_usdt,
                expected_price1=p1, expected_price2=p2,
                limit_wait_sec=_lws,
                limit_offset_pct=_lop,
                entry_market_fallback=_emf,
            )
            try:
                return _fut.result(timeout=_hard_timeout)
            except _FuturesTimeout:
                log.error('bybit_open %s/%s: hard timeout %ds — Bybit вызов отменён',
                          coin1, coin2, _hard_timeout)
                return {'success': False, 'error': f'hard timeout {_hard_timeout}s'}
    except Exception as e:
        log.error('bybit_open %s/%s: %s', coin1, coin2, e)
        return {'success': False, 'error': str(e)}

def _daemon_bybit_close(coin1, coin2, direction, p1=None, p2=None):
    if _bybit_exec is None:
        return None
    try:
        # BYBIT-THREAD-FIX: аналогичный hard timeout для close
        _lws_close = 5  # close всегда 5с
        _hard_timeout_close = _lws_close + 30
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
        with ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(
                _bybit_exec.close_pair_trade_smart_limit,
                coin1, coin2, direction,
                expected_price1=p1, expected_price2=p2,
                limit_wait_sec=_lws_close,
            )
            try:
                return _fut.result(timeout=_hard_timeout_close)
            except _FuturesTimeout:
                log.error('bybit_close %s/%s: hard timeout %ds',
                          coin1, coin2, _hard_timeout_close)
                return {'success': False, 'error': f'hard timeout {_hard_timeout_close}s'}
    except Exception as e:
        log.error('bybit_close %s/%s: %s', coin1, coin2, e)
        return {'success': False, 'error': str(e)}

_TRAIL_KEYS = (
    '_z_trail_activated', '_z_trail_peak',
    '_tp_trail_activated', '_tp_trail_peak',
    '_recovery_trail_activated', '_recovery_trail_peak',
    'exit_phase',
    '_trail_params_locked', '_trail_act_locked', '_trail_dd_locked',
)


def alert(msg):
    """ERR-02 FIX: retry 3x с backoff при сетевой ошибке Telegram.
    Без retry критические уведомления (открытие/закрытие) терялись
    при кратковременной недоступности Telegram API."""
    log.info('[TG] %s', msg)
    if TG_TOKEN and TG_CHAT_ID:
        for _tg_attempt in range(3):
            try:
                send_telegram(TG_TOKEN, TG_CHAT_ID, msg)
                return  # success
            except Exception as e:
                log.warning('Telegram ошибка (попытка %d/3): %s', _tg_attempt + 1, e)
                if _tg_attempt < 2:
                    time.sleep(1.0 * (_tg_attempt + 1))  # 1s, 2s
        log.error('Telegram НЕДОСТУПЕН после 3 попыток: %s', msg[:100])


def _remove_pending_file(filepath: str) -> None:
    """[A14] Немедленное удаление pending-файла при отказе.

    Вместо ожидания TTL 2ч — удаляем сразу при явном reject.
    Активируется через config: monitor.pending_cleanup_on_reject=true.
    Daemon обрабатывал до 12 зависших файлов каждую минуту — этот
    helper устраняет накопление отклонённых pending в очереди.
    """
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            log.info('Pending удалён (rejected): %s', os.path.basename(filepath))
    except OSError as e:
        log.warning('Не удалось удалить pending %s: %s', filepath, e)


def _maybe_remove_pending(pf: str, imp: dict) -> None:
    """[A14] Вспомогательная обёртка: удаляет pending если включён cleanup_on_reject."""
    if CFG('monitor', 'pending_cleanup_on_reject', False):
        _remove_pending_file(pf)


def process_pending():
    """Читает pending_*.json из monitor_import/ и открывает позиции."""
    import glob

    log.info('[TRACE] process_pending: старт')
    if not os.path.isdir(_MONITOR_IMPORT_DIR):
        return

    pending_files = sorted(glob.glob(
        os.path.join(_MONITOR_IMPORT_DIR, 'pending_*.json')))
    if not pending_files:
        return

    log.info('Найдено %d pending файлов', len(pending_files))

    # MEM-FIX-v2: Импортируем один раз за вызов, а не на каждый файл в цикле
    _check_scanner_size = None
    _get_btc_z_from_file = None
    _check_btc_direction_filter = None
    _check_entry_z_min = None
    if _PAIRS_SCANNER_OK:
        try:
            log.info('[TRACE] process_pending: импорт pairs_scanner.engine.auto_entry')
            from pairs_scanner.engine.auto_entry import (
                check_scanner_size as _check_scanner_size,
                get_btc_z_from_file as _get_btc_z_from_file,
                check_btc_direction_filter as _check_btc_direction_filter,
                check_entry_z_min as _check_entry_z_min,
            )
            log.info('[TRACE] process_pending: импорт pairs_scanner OK')
        except ImportError:
            pass

    # ANALYSIS-v51: Прочитать текущий adaptive quality порог для логирования
    _adaptive_q_current = None
    try:
        if _aq_mod is not None:
            _adaptive_q_current = _aq_mod.get_current_q(_BASE_DIR, cfg_fn=CFG)
            log.info('Adaptive Quality Gate: текущий min_quality=%d', _adaptive_q_current)
    except Exception as _aq_e:
        log.debug('adaptive_quality load skip: %s', _aq_e)

    # MEM-FIX: загружаем позиции один раз, переиспользуем в цикле
    _all_positions_cache = load_positions()
    open_pairs = set()
    for p in _all_positions_cache:
        if p.get('status') == 'OPEN' and p.get('coin1') and p.get('coin2'):
            open_pairs.add(f"{p['coin1']}/{p['coin2']}")

    for pf in pending_files:
        try:
            with open(pf, 'r', encoding='utf-8') as f:
                imp = json.load(f)

            if isinstance(imp, list):
                imp = imp[0] if imp and isinstance(imp[0], dict) else None
            if not isinstance(imp, dict) or not imp.get('coin1') or not imp.get('coin2'):
                log.warning('Невалидный файл: %s', os.path.basename(pf))
                os.remove(pf)
                continue

            pair = f"{imp['coin1']}/{imp['coin2']}"

            # SEC-03: валидация pending JSON
            if _PAIRS_SCANNER_OK:
                _valid, _vreason = validate_pending(imp)
            else:
                _valid = True
                if imp.get('direction', '') not in ('LONG', 'SHORT'):
                    _valid = False
                    _vreason = f"невалидный direction={imp.get('direction')}"
            if not _valid:
                log.warning('%s: %s, удаляю: %s', pair, _vreason, os.path.basename(pf))
                diag_log_refusal(pair, imp.get('direction','?'), f'SEC-03: {_vreason}', source='daemon')
                _remove_pending_file(pf)  # [A14] SEC-03 reject → немедленно
                continue

            # ERR-01: TTL pending-файлов (2 часа)
            if _PAIRS_SCANNER_OK:
                _expired, _ttl_reason = check_pending_ttl(pf, ttl_seconds=7200)
            else:
                try:
                    _expired = (time.time() - os.path.getmtime(pf)) > 7200
                    _ttl_reason = 'TTL expired'
                except Exception:
                    _expired = False
                    _ttl_reason = ''
            if _expired:
                log.warning('%s: %s, удаляю', pair, _ttl_reason)
                diag_log_refusal(pair, imp.get('direction','?'), _ttl_reason, source='daemon')
                _remove_pending_file(pf)  # [A14] TTL reject → немедленно
                continue

            if pair in open_pairs:
                log.info('%s: уже открыта, удаляю pending', pair)
                os.remove(pf)
                continue

            # === SCANNER SIZE CHECK: размер ТОЛЬКО от сканера ===
            if _PAIRS_SCANNER_OK and _check_scanner_size is not None:
                _sz_ok, _sz_val, _sz_reason = _check_scanner_size(imp)
            else:
                _sz_val = float(imp.get('risk_size_usdt', imp.get('recommended_size', 0)))
                _sz_ok = _sz_val > 0
                _sz_reason = '' if _sz_ok else 'нет risk_size_usdt'
            if not _sz_ok:
                log.warning('%s: отклонён — %s', pair, _sz_reason)
                diag_log_refusal(pair, imp.get('direction','?'),
                                 f'SIZE: {_sz_reason}', source='daemon')
                _remove_pending_file(pf)  # [A14] SIZE reject → немедленно
                continue

            # === BTC Z DIRECTIONAL FILTER ===
            _direction = imp.get('direction', '')
            if _PAIRS_SCANNER_OK and _get_btc_z_from_file is not None:
                _btc_z = _get_btc_z_from_file(_BASE_DIR)
            else:
                _btc_z = 0.0
                try:
                    _rally_path = os.path.join(_BASE_DIR, 'rally_state.json')
                    if os.path.exists(_rally_path):
                        with open(_rally_path, 'r', encoding='utf-8') as _rf:
                            _btc_z = float(json.load(_rf).get('btc_z', 0))
                except Exception:
                    pass
            if _PAIRS_SCANNER_OK and _check_btc_direction_filter is not None:
                _btc_blocked, _btc_reason = _check_btc_direction_filter(_btc_z, _direction)
            else:
                _btc_blocked = False
                if _direction == 'LONG' and _btc_z > 2.0:
                    _btc_blocked = True
                    _btc_reason = f'BTC Z={_btc_z:+.2f} > +2.0, LONG заблокирован'
                elif _direction == 'SHORT' and _btc_z < -2.0:
                    _btc_blocked = True
                    _btc_reason = f'BTC Z={_btc_z:+.2f} < -2.0, SHORT заблокирован'
            if _btc_blocked:
                log.warning('%s: %s', pair, _btc_reason)
                diag_log_refusal(pair, _direction, _btc_reason, source='daemon')
                _remove_pending_file(pf)  # [A14] BTC-Z reject → немедленно
                continue

            # === [A51] Adaptive Quality Gate — проверка min_quality из adaptive_quality_state.json ===
            if _adaptive_q_current is not None:
                _pair_q = float(imp.get('quality_score', imp.get('q_score', 0)) or 0)
                if _pair_q > 0 and _pair_q < _adaptive_q_current:
                    _aq_reason = (
                        f"Q={_pair_q:.0f} < adaptive_min_quality={_adaptive_q_current} "
                        f"(ANALYSIS-v51: adaptive gate)"
                    )
                    log.info('%s: %s', pair, _aq_reason)
                    diag_log_refusal(pair, imp.get('direction','?'), _aq_reason, source='daemon')
                    _remove_pending_file(pf)
                    continue

            # === [A12] entry_z_min — минимальный |Z| для ВСЕХ направлений ===
            # LONG и SHORT с |Z| < 2.5 убыточны: avg=-0.32% до v45, avg=-0.60% v45 hybrid.
            # Работает параллельно с min_z_long (тот блокирует только LONG с |Z| < 3.0).
            if _PAIRS_SCANNER_OK and _check_entry_z_min is not None:
                _ez_ok, _ez_reason = _check_entry_z_min(imp, cfg_fn=CFG)
            else:
                _entry_z_min = CFG('strategy', 'entry_z_min', 0.0)
                _abs_z_val = abs(float(imp.get('entry_z', 0) or 0))
                _ez_ok = (_entry_z_min <= 0 or _abs_z_val >= _entry_z_min)
                _ez_reason = (
                    f"{_direction} |Z|={_abs_z_val:.2f} < entry_z_min={_entry_z_min:.1f} "
                    f"(ANALYSIS-v47: сделки с |Z|<{_entry_z_min:.1f} убыточны во всех фазах)"
                ) if not _ez_ok else ""
            if not _ez_ok:
                log.warning('%s: %s', pair, _ez_reason)
                diag_log_refusal(pair, _direction, _ez_reason, source='daemon')
                _remove_pending_file(pf)  # [A14] entry_z_min reject → немедленно
                continue

            # === ANALYSIS-v46 [4]: MIN_Z_LONG — ограничение LONG входов по Z ===
            # LONG avg=-0.55% WR=38.9% vs SHORT avg=-0.12% WR=46.7%
            # LONG пропускаем только при экстремальном Z (min_z_long из config)
            _min_z_long = CFG('strategy', 'min_z_long', 0.0)
            if _min_z_long > 0 and _direction == 'LONG':
                _entry_z_abs = abs(float(imp.get('entry_z', 0)))
                if _entry_z_abs < _min_z_long:
                    _z_reason = (f'LONG |Z|={_entry_z_abs:.2f} < min_z_long={_min_z_long:.1f} '
                                 f'(ANALYSIS-v46: LONG ограничен, только экстремальные отклонения)')
                    log.warning('%s: %s', pair, _z_reason)
                    diag_log_refusal(pair, _direction, _z_reason, source='daemon')
                    _remove_pending_file(pf)  # [A14] min_z_long reject → немедленно
                    continue

            # === [A17] max_z_entry — верхний порог Z (breakout filter) ===
            # Dual TF: |Z| 3.0-4.0 WR=27% avg=-1.09% — худшая зона.
            # При |Z|>4 пара скорее ломает коинтеграцию, чем возвращается.
            _max_z_entry = float(CFG('strategy', 'max_z_entry', 0.0))
            if _max_z_entry > 0:
                _abs_z_entry = abs(float(imp.get('entry_z', 0) or 0))
                if _abs_z_entry > _max_z_entry:
                    _mz_reason = (
                        f'{_direction} |Z|={_abs_z_entry:.2f} > max_z_entry={_max_z_entry:.1f} '
                        f'(ANALYSIS-v48: |Z|>{_max_z_entry:.0f} = breakout, не mean-reversion)'
                    )
                    log.warning('%s: %s', pair, _mz_reason)
                    diag_log_refusal(pair, _direction, _mz_reason, source='daemon')
                    _remove_pending_file(pf)
                    continue

            # === [A19] min_entry_correlation — фильтр корреляции пары ===
            # Пары с ρ < 0.3 не имеют работающего хеджа.
            # Скан: ASTER/XPL ρ=0.16, DOGE/TRIA ρ=0.12, APT/AXS ρ=-0.11.
            _min_corr = float(CFG('strategy', 'min_entry_correlation', 0.0))
            if _min_corr > 0:
                _pair_corr = float(imp.get('correlation', imp.get('corr', 0)) or 0)
                if 0 < abs(_pair_corr) < _min_corr:
                    _corr_reason = (
                        f'{_direction} ρ={_pair_corr:.3f} < min={_min_corr:.1f} '
                        f'(ANALYSIS-v48: хедж не работает при низкой корреляции)'
                    )
                    log.warning('%s: %s', pair, _corr_reason)
                    diag_log_refusal(pair, _direction, _corr_reason, source='daemon')
                    _remove_pending_file(pf)
                    continue

            p1 = float(imp.get('entry_price1', 0) or 0)
            p2 = float(imp.get('entry_price2', 0) or 0)
            if p1 == 0 or p2 == 0:
                log.info('%s: получаю цены (get_current_price)...', pair)
                log.info('[TRACE] %s: вызов get_current_price %s', pair, imp['coin1'])
                p1 = get_current_price(EXCHANGE, imp['coin1']) or 0
                log.info('[TRACE] %s: get_current_price %s = %s', pair, imp['coin1'], p1)
                log.info('[TRACE] %s: вызов get_current_price %s', pair, imp['coin2'])
                p2 = get_current_price(EXCHANGE, imp['coin2']) or 0
                log.info('[TRACE] %s: get_current_price %s = %s', pair, imp['coin2'], p2)

            if p1 <= 0 or p2 <= 0:
                reason = f'нет цен: p1={p1} p2={p2}'
                log.warning('%s: %s', pair, reason)
                diag_log_refusal(pair, imp.get('direction','?'), reason,
                                 source='daemon', details={'p1':p1,'p2':p2})
                # ERR-01 FIX: счётчик неудачных попыток получения цены.
                # После 10 неудачных попыток (10 мин) — удаляем pending.
                imp['_price_attempts'] = imp.get('_price_attempts', 0) + 1
                if imp['_price_attempts'] >= 10:
                    log.warning('%s: 10 неудачных попыток цены, удаляю pending', pair)
                    os.remove(pf)
                else:
                    try:
                        with open(pf, 'w', encoding='utf-8') as f:
                            json.dump(imp, f, indent=2, ensure_ascii=False)
                    except Exception:
                        pass
                continue

            notes = imp.get('notes', '')
            label = imp.get('entry_label', '')
            if label and label not in notes:
                notes = f'{label} | {notes}'

            # BUG-10: filters_state from file (UI saves checkboxes)
            if _PAIRS_SCANNER_OK:
                filters_state = load_filters_state(_BASE_DIR)
            else:
                _FILTER_KEYS = [
                    'block_green','block_green_bt_fail','block_green_bt_warn',
                    'block_yellow','block_yellow_bt_fail','block_yellow_bt_warn',
                    'block_wl_warn','block_wl_fail','block_nk_warn','block_nk_fail',
                    'block_long','block_short',
                ]
                filters_state = {k: False for k in _FILTER_KEYS}
                _fs_path = os.path.join(_BASE_DIR, 'filters_state.json')
                try:
                    if os.path.exists(_fs_path):
                        with open(_fs_path, 'r', encoding='utf-8') as _fsf:
                            _fs_data = json.load(_fsf)
                        if isinstance(_fs_data, dict):
                            for k in _FILTER_KEYS:
                                if k in _fs_data:
                                    filters_state[k] = bool(_fs_data[k])
                except Exception:
                    pass
            filter_blocked, filter_reason = check_entry_filters(
                notes, imp.get('direction',''), filters_state)
            if filter_blocked:
                log.info('%s: заблокировано фильтром: %s', pair, filter_reason)
                diag_log_refusal(pair, imp.get('direction','?'),
                                 filter_reason, source='daemon')
                imp['_attempts'] = imp.get('_attempts', 0) + 1
                if imp['_attempts'] >= 3:
                    os.remove(pf)
                else:
                    with open(pf, 'w', encoding='utf-8') as f:
                        json.dump(imp, f, indent=2, ensure_ascii=False)
                continue

            # RACE-01 FIX: перечитываем open позиции непосредственно перед add_position.
            # Защита от двойного открытия: за время проверки фильтров (выше) другой
            # pending файл для этой же пары мог быть обработан.
            _fresh_open = set()
            for _fp in _all_positions_cache:  # MEM-FIX: reuse cached positions
                if _fp.get('status') == 'OPEN' and _fp.get('coin1') and _fp.get('coin2'):
                    _fresh_open.add(f"{_fp['coin1']}/{_fp['coin2']}")
            if pair in _fresh_open:
                log.info('%s: уже открыта (re-check), удаляю pending', pair)
                os.remove(pf)
                continue

            log.info('[TRACE] %s: вызов add_position...', pair)
            pos = add_position(
                imp['coin1'], imp['coin2'],
                imp['direction'],
                imp['entry_z'], imp['entry_hr'],
                p1, p2,
                imp.get('timeframe', '4h'),
                notes,
                entry_intercept=float(imp.get('entry_intercept',
                                               imp.get('intercept', 0.0))),
                recommended_size=_sz_val,  # SCANNER SIZE ONLY (validated above)
                z_window=imp.get('z_window'),
                auto_opened=True,
                bt_verdict=imp.get('bt_verdict'),
                bt_pnl=imp.get('bt_pnl'),
                mu_bt_wr=imp.get('mu_bt_wr'),
                v_quality=imp.get('v_quality'),
                max_hold_hours=imp.get('max_hold_hours'),
                pnl_stop_pct=imp.get('pnl_stop_pct'),
            )
            log.info('[TRACE] %s: add_position вернул %s', pair, 'OK' if pos else 'None')

            if pos:
                log.info('%s: ОТКРЫТА #%d | Z=%.2f HR=%.4f p1=%.4f p2=%.4f',
                         pair, pos['id'], imp['entry_z'], imp['entry_hr'], p1, p2)
                open_pairs.add(pair)
                # RACE-01 FIX-v2: обновляем кеш позиций чтобы следующий pending
                # в этом же цикле увидел только что открытую позицию
                _all_positions_cache.append(pos)
                diag_log_attempt(pair, imp['direction'], notes, source='daemon')
                # Зеркалирование на Bybit Demo
                _rec_size = float(pos.get('recommended_size', 100))
                log.info('[TRACE] %s: вызов _daemon_bybit_open...', pair)
                _bb = _daemon_bybit_open(imp['coin1'], imp['coin2'],
                                         imp['direction'], _rec_size, p1, p2)
                log.info('[TRACE] %s: _daemon_bybit_open вернул %s', pair, _bb.get('success') if _bb else 'None')
                if _bb is not None:
                    if _bb.get('success'):
                        _ot = _bb.get('order_types', 'market+market')
                        log.info('%s: Bybit OPEN OK | type=%s slippage=%.4f%%',
                                 pair, _ot, _bb.get('total_slippage_pct', 0))
                        _patch_position(pos['id'], {'bybit_open_type': _ot})
                    else:
                        log.warning('%s: Bybit OPEN FAIL: %s', pair, _bb.get('error','?'))
                alert(
                    f'\U0001f7e2 ОТКРЫТА (daemon)\n'
                    f'#{pos["id"]} {pair} {imp["direction"]}\n'
                    f'Z={imp["entry_z"]:.2f} | Size=${_rec_size:.0f}\n'
                    f'{label}'
                )
                os.remove(pf)
            else:
                err = _session_state_store.get('_last_add_pos_err',
                                               'лимит/cooldown/фильтр')
                log.warning('%s: не открыта: %s', pair, err)
                diag_log_refusal(pair, imp.get('direction','?'), str(err),
                                 source='daemon',
                                 details={'notes': notes[:120]})
                imp['_attempts'] = imp.get('_attempts', 0) + 1
                if imp['_attempts'] >= 3:
                    log.warning('%s: 3 попытки исчерпаны, удаляю', pair)
                    os.remove(pf)
                else:
                    with open(pf, 'w', encoding='utf-8') as f:
                        json.dump(imp, f, indent=2, ensure_ascii=False)

        except Exception:
            log.error('Ошибка %s:\n%s',
                      os.path.basename(pf), traceback.format_exc())


def _patch_position(pos_id, patch):
    """PERF-03: Atomic UPDATE one position.
    v46: pairs_scanner → infra/storage.update_position() first."""
    if _PAIRS_SCANNER_OK:
        try:
            log.info('[TRACE] _patch_position #%s: storage_update...', pos_id)
            if _storage_update(pos_id, patch):
                log.info('[TRACE] _patch_position #%s: storage_update OK', pos_id)
                return
        except Exception:
            pass
    # Fallback: db_store → JSON
    try:
        from db_store import db_update_position
        log.info('[TRACE] _patch_position #%s: db_update...', pos_id)
        if db_update_position(pos_id, patch):
            log.info('[TRACE] _patch_position #%s: db_update OK', pos_id)
            return
    except (ImportError, Exception):
        pass
    try:
        import contextlib
        log.info('[TRACE] _patch_position #%s: JSON fallback — ожидаю лок...', pos_id)
        ctx = _positions_write_lock() if _positions_write_lock else contextlib.nullcontext()
        with ctx:
            log.info('[TRACE] _patch_position #%s: лок получен, читаю позиции...', pos_id)
            all_pos = load_positions()
            for p in all_pos:
                if p.get('id') == pos_id:
                    p.update(patch)
                    break
            log.info('[TRACE] _patch_position #%s: save_positions...', pos_id)
            save_positions(all_pos)
            log.info('[TRACE] _patch_position #%s: save_positions OK', pos_id)
    except Exception:
        log.warning('_patch_position(%s):\n%s', pos_id, traceback.format_exc())


def monitor_open_positions():
    _set_op('monitor_open_positions: CFG_auto_reload')
    CFG_auto_reload()
    _set_op('monitor_open_positions: загрузка adaptive_exits')

    # ANALYSIS-v53: Загрузить адаптивные параметры выхода и подставить в CFG
    _aex_params = {}
    try:
        if _aex_mod is not None:
            _aex_params = _aex_mod.get_current_params(_BASE_DIR)
            log.debug('Adaptive exits: %s', _aex_mod.get_status_str(_BASE_DIR))
    except Exception as _aex_e:
        log.debug('adaptive_exits load skip: %s', _aex_e)

    # v46: pairs_scanner → infra/storage.load_open_positions() first.
    _set_op('monitor_open_positions: load_open_positions')
    if _PAIRS_SCANNER_OK:
        open_pos = load_open_positions()
    else:
        try:
            from db_store import db_get_open_positions
            open_pos = db_get_open_positions()
        except (ImportError, Exception):
            positions = load_positions()
            open_pos = [p for p in positions
                        if isinstance(p, dict) and p.get('status') == 'OPEN']
    log.info('[TRACE] monitor_open_positions: загружено %d открытых позиций', len(open_pos))

    if not open_pos:
        log.info('Нет открытых позиций')
        return

    log.info('Мониторинг %d позиций...', len(open_pos))

    # WATCHDOG-FIX: таймаут на обработку одной позиции.
    # monitor_position делает 2 HTTP-запроса (fetch_ohlcv × 2 монеты).
    # При деградации сети один вызов может занять минуты.
    # При 10 позициях без таймаута = десятки минут блокировки.
    _pos_timeout = max(60, INTERVAL)  # не более одного интервала на позицию

    from concurrent.futures import ThreadPoolExecutor as _POS_TPE, TimeoutError as _POS_Timeout

    for pos in open_pos:
        pair = f"{pos.get('coin1','?')}/{pos.get('coin2','?')}"
        try:
            # Запускаем monitor_position в отдельном потоке с таймаутом
            _set_op(f'monitor_position: {pair} (fetch prices + Z-score)')
            with _POS_TPE(max_workers=1) as _pos_pool:
                _pos_fut = _pos_pool.submit(monitor_position, pos, EXCHANGE)
                try:
                    mon = _pos_fut.result(timeout=_pos_timeout)
                    log.info('[TRACE] %s: monitor_position OK pnl=%s',
                              pair, f"{mon['pnl_pct']:+.2f}%" if mon else 'None')
                except _POS_Timeout:
                    log.warning('%s: monitor_position таймаут %ds — пропускаю позицию', pair, _pos_timeout)
                    _set_op(f'monitor_position TIMEOUT: {pair} — пропуск')
                    continue
            if mon is None:
                log.warning('%s: нет данных', pair)
                continue

            pnl      = mon['pnl_pct']
            z        = mon.get('z_static', mon.get('z_now', 0))
            hours_in = mon.get('hours_in', 0)

            new_best = max(mon.get('best_pnl', pnl), pnl)
            if new_best > pos.get('best_pnl_during_trade', 0):
                _set_op(f'_patch_position best_pnl: {pair}')
                _patch_position(pos['id'], {
                    'best_pnl_during_trade': new_best,
                    'best_pnl': new_best,
                })
                log.info('[TRACE] %s: _patch_position best_pnl OK', pair)
                pos['best_pnl_during_trade'] = new_best
                pos['best_pnl'] = new_best

            # === ANALYSIS-v46 [6]: FORCE_TRAIL ===
            # Если best_during >= force_trail_best_pct И hours_in >= force_trail_hours
            # → принудительно активируем trailing независимо от trailing_activate_pct.
            # Предотвращает уход в TIMEOUT сделок, где движение уже было, но trail не успел.
            _ft_best_thr  = CFG('monitor', 'force_trail_best_pct', 0.93)
            _ft_hours_thr = CFG('monitor', 'force_trail_hours', 4.0)
            if (new_best >= _ft_best_thr
                    and hours_in >= _ft_hours_thr
                    and not pos.get('_force_trail_applied')
                    and not pos.get('trailing_active')
                    and not pos.get('_trail_params_locked')):
                _ft_patch = {
                    '_force_trail_applied': True,
                    'trailing_active': True,
                    'trail_peak_pnl': new_best,
                    'trailing_activate_pct': _ft_best_thr,  # переопределяем порог
                }
                _patch_position(pos['id'], _ft_patch)
                pos.update(_ft_patch)
                log.info('%s: FORCE_TRAIL активирован (best=%.2f%% >= %.2f%%, hold=%.1fч >= %.1fч)',
                         pair, new_best, _ft_best_thr, hours_in, _ft_hours_thr)
                alert(
                    f'🔒 FORCE_TRAIL (daemon)\n'
                    f'{pair} | best={new_best:.2f}% hold={hours_in:.1f}ч\n'
                    f'Trailing принудительно активирован (best>={_ft_best_thr}%, hold>={_ft_hours_thr}ч)'
                )

            # ANALYSIS-v53: Применить адаптивные параметры выхода через CFG override
            def _cfg_with_adaptive(section, key, default=None):
                if section == 'monitor' and key == 'stale_exit_hours' and _aex_params.get('stale_exit_hours'):
                    return _aex_params['stale_exit_hours']
                if section == 'monitor' and key == 'trailing_drawdown_pct' and _aex_params.get('trailing_drawdown_pct'):
                    return _aex_params['trailing_drawdown_pct']
                return CFG(section, key, default)

            _cfg_for_tick = _cfg_with_adaptive if _aex_params else CFG

            # BUG-03 FIX: убран _patch_position(trail_pre) ПЕРЕД check_auto_exit.
            # v46: при наличии pairs_scanner → run_monitor_tick (222 теста).
            # Fallback → check_auto_exit из monitor модуля.
            _set_op(f'check_auto_exit / run_monitor_tick: {pair}')
            if _PAIRS_SCANNER_OK:
                log.info('[TRACE] %s: вызов run_monitor_tick...', pair)
                _tick = run_monitor_tick(pos, mon, cfg_fn=_cfg_for_tick)
                log.info('[TRACE] %s: run_monitor_tick OK, should_close=%s reason=%s',
                          pair, _tick['should_close'], _tick.get('reason',''))
                should_close = _tick['should_close']
                reason = _tick['reason']
                trail_post = _tick['trail_patch']
                if _tick['best_pnl_patch']:
                    log.info('[TRACE] %s: _patch_position best_pnl_patch...', pair)
                    _patch_position(pos['id'], _tick['best_pnl_patch'])
                    log.info('[TRACE] %s: _patch_position best_pnl_patch OK', pair)
            else:
                log.info('[TRACE] %s: вызов check_auto_exit...', pair)
                should_close, reason = check_auto_exit(pos, mon)
                log.info('[TRACE] %s: check_auto_exit OK, should_close=%s', pair, should_close)
                trail_post = {k: pos[k] for k in _TRAIL_KEYS if k in pos}

            # === [A13] AUTO_HR_DRIFT: HOLD / REBALANCE / EXIT ===
            # Kalman пересчитывает HR каждые hr_kalman_update_hours.
            # assess_hr_drift() выбирает действие по трём порогам:
            #   drift < warn_pct          → HOLD    (только метрика)
            #   warn ≤ drift < critical   → REBALANCE (обновить entry_hr + скорр. ногу на бирже)
            #   drift ≥ critical_pct      → EXIT    (закрыть: пара потеряла коинтеграцию)
            # Цены: exchange.py fetch_prices() → ccxt 1m candles (60 баров, TTL 60s cache).
            _hr_update_hours = float(CFG('monitor', 'hr_kalman_update_hours', 4.0))
            if _hr_update_hours > 0 and not should_close:
                _last_hr_upd = pos.get('_last_hr_update_ts', 0)
                _now_ts = time.time()
                _hours_since_upd = (
                    (_now_ts - _last_hr_upd) / 3600.0 if _last_hr_upd else hours_in
                )
                if _hours_since_upd >= _hr_update_hours and hours_in >= _hr_update_hours:
                    try:
                        from pairs_scanner.core.pair_analysis import kalman_hr_update
                        from pairs_scanner.core.position_manager import (
                            assess_hr_drift, HRDriftParams,
                        )

                        _c1, _c2 = pos.get('coin1'), pos.get('coin2')

                        # [A13] Получаем последние 60 close-цен через exchange.py
                        log.info('[TRACE] %s: _fetch_prices для HR drift (%s, %s)...', pair, _c1, _c2)
                        _df1 = _fetch_prices(EXCHANGE, _c1, '1m', 60) if _PAIRS_SCANNER_OK else None
                        _df2 = _fetch_prices(EXCHANGE, _c2, '1m', 60) if _PAIRS_SCANNER_OK else None
                        log.info('[TRACE] %s: _fetch_prices OK df1=%s df2=%s',
                                  pair, len(_df1) if _df1 is not None else None,
                                  len(_df2) if _df2 is not None else None)
                        _prices1 = _df1['c'].tolist() if _df1 is not None and len(_df1) >= 20 else None
                        _prices2 = _df2['c'].tolist() if _df2 is not None and len(_df2) >= 20 else None

                        if _prices1 is not None and _prices2 is not None and len(_prices1) >= 20:
                            _hr_kalman = kalman_hr_update(
                                _prices1, _prices2,
                                current_hr=pos.get('entry_hr', 1.0),
                                current_hr_std=pos.get('hr_std'),
                                n_recent=60,
                            )

                            _drift_params = HRDriftParams(
                                warn_pct=float(CFG('monitor', 'hr_drift_warn_pct', 15.0)),
                                critical_pct=float(CFG('monitor', 'hr_drift_critical_pct', 40.0)),
                                min_hold_hours=_hr_update_hours,
                                rebalance_cooldown_hours=float(
                                    CFG('monitor', 'hr_rebalance_cooldown_hours', 4.0)),
                            )

                            _drift = assess_hr_drift(
                                pos,
                                new_hr=_hr_kalman['new_hr'],
                                new_hr_std=_hr_kalman['new_hr_std'],
                                drift_pct=_hr_kalman['hr_drift_pct'],
                                hours_in=hours_in,
                                params=_drift_params,
                            )

                            # Всегда сохраняем метрику + timestamp
                            _base_patch = {
                                **_drift['patch'],
                                '_last_hr_update_ts': _now_ts,
                            }

                            if _drift['action'] == 'EXIT':
                                # Закрываем позицию через стандартный механизм
                                log.warning(_drift['reason'])
                                alert(
                                    f"🚨 AUTO_HR_DRIFT EXIT (daemon)\n"
                                    f"#{pos['id']} {pair} {pos['direction']}\n"
                                    f"HR drift={_hr_kalman['hr_drift_pct']:.1f}% "
                                    f"≥ {_drift_params.critical_pct:.0f}%\n"
                                    f"entry_hr={pos.get('entry_hr', 0):.4f} → "
                                    f"new_hr={_hr_kalman['new_hr']:.4f} | hold={hours_in:.1f}ч"
                                )
                                _patch_position(pos['id'], _base_patch)
                                close_position(
                                    pos['id'],
                                    mon['price1_now'], mon['price2_now'],
                                    z, _drift['reason'],
                                    exit_z_static=mon.get('z_static'),
                                )
                                should_close = True
                                reason = _drift['reason']

                            elif _drift['action'] == 'REBALANCE':
                                log.warning(_drift['reason'])
                                _patch_position(pos['id'], _base_patch)
                                pos.update(_base_patch)  # синхронизировать entry_hr в памяти

                                # Физическая корректировка ноги на Bybit (если есть qty)
                                _rb = _drift.get('bybit_rebalance')
                                if _rb and _bybit_exec is not None:
                                    try:
                                        _rb_res = _bybit_exec.rebalance_leg(
                                            coin=_rb['coin'],
                                            current_side=_rb['current_side'],
                                            current_qty=_rb['current_qty'],
                                            target_qty=_rb['target_qty'],
                                        )
                                        if _rb_res['action'] != 'skip':
                                            log.info(
                                                '%s: Bybit rebalance_leg %s delta=%+.4f fill=%.4f',
                                                pair, _rb_res['action'],
                                                _rb_res['delta_qty'],
                                                _rb_res.get('fill_price') or 0,
                                            )
                                        if not _rb_res['success']:
                                            log.warning('%s: rebalance_leg FAIL: %s',
                                                        pair, _rb_res.get('error'))
                                    except Exception as _rbe:
                                        log.warning('%s: rebalance_leg exception: %s', pair, _rbe)

                                alert(
                                    f"🔄 AUTO_HR_DRIFT REBALANCE (daemon)\n"
                                    f"#{pos['id']} {pair} {pos['direction']}\n"
                                    f"HR drift={_hr_kalman['hr_drift_pct']:.1f}% | "
                                    f"entry_hr={pos.get('entry_hr_original', pos.get('entry_hr', 0)):.4f}"
                                    f" → new_hr={_hr_kalman['new_hr']:.4f}\n"
                                    f"Ребалансов: {pos.get('_rebalance_count', 1)} | hold={hours_in:.1f}ч"
                                )

                            else:  # HOLD
                                if _drift['reason']:
                                    log.info('%s: AUTO_HR_DRIFT: %s', pair, _drift['reason'])
                                _patch_position(pos['id'], _base_patch)

                    except Exception as _hr_exc:
                        log.debug('AUTO_HR_DRIFT error %s: %s', pair, _hr_exc)
            if trail_post:
                log.info('[TRACE] %s: _patch_position trail_post...', pair)
                _patch_position(pos['id'], trail_post)
                log.info('[TRACE] %s: _patch_position trail_post OK', pair)

            if should_close:
                log.info('%s: AUTO-EXIT %s | P&L=%+.2f%% hold=%.1fч',
                         pair, reason, pnl, hours_in)
                _set_op(f'close_position: {pair} ({reason})')
                log.info('[TRACE] %s: вызов close_position...', pair)
                close_position(
                    pos['id'],
                    mon['price1_now'], mon['price2_now'],
                    z, reason,
                    exit_z_static=mon.get('z_static'),
                )
                log.info('[TRACE] %s: close_position OK', pair)
                # Зеркалирование закрытия на Bybit Demo
                _set_op(f'bybit_close: {pair}')
                log.info('[TRACE] %s: вызов _daemon_bybit_close...', pair)
                _bb_cl = _daemon_bybit_close(
                    pos['coin1'], pos['coin2'], pos['direction'],
                    mon.get('price1_now'), mon.get('price2_now'))
                log.info('[TRACE] %s: _daemon_bybit_close вернул %s',
                          pair, _bb_cl.get('success') if _bb_cl else 'None')
                if _bb_cl is not None:
                    if _bb_cl.get('success'):
                        _ct = _bb_cl.get('order_types', 'market+market')
                        log.info('%s: Bybit CLOSE OK | type=%s slippage=%.4f%%',
                                 pair, _ct, _bb_cl.get('total_slippage_pct', 0))
                        _patch_position(pos['id'], {'bybit_close_type': _ct})
                    else:
                        log.warning('%s: Bybit CLOSE FAIL: %s', pair, _bb_cl.get('error','?'))
                emoji = '\u2705' if pnl > 0 else '\u274c'
                alert(
                    f'{emoji} AUTO-CLOSE (daemon)\n'
                    f'#{pos["id"]} {pair} {pos["direction"]}\n'
                    f'P&L: {pnl:+.2f}% | Best: {new_best:+.2f}%\n'
                    f'Reason: {reason}\n'
                    f'Hold: {hours_in:.1f}\u0447'
                )

                # ANALYSIS-v53: Обновить adaptive_exits после закрытия
                try:
                    if _aex_mod is not None:
                        _recent_closed = [
                            {
                                'exit_reason': p.get('exit_reason', ''),
                                'pnl_pct': float(p.get('pnl_pct', 0) or 0),
                                'best_pnl': float(p.get('best_pnl_during_trade',
                                               p.get('best_pnl', 0)) or 0),
                                'hours_in_trade': float(
                                    (p.get('exit_time') and p.get('entry_time') and
                                     (__import__('datetime').datetime.fromisoformat(
                                         p['exit_time']).timestamp() -
                                      __import__('datetime').datetime.fromisoformat(
                                         p['entry_time']).timestamp()) / 3600)
                                    or 0),
                            }
                            for p in load_positions()
                            if p.get('status') == 'CLOSED'
                            and p.get('pnl_pct') is not None
                        ][-30:]
                        _aex_mod.update_from_closed_positions(_BASE_DIR, _recent_closed)
                except Exception as _aex_upd_e:
                    log.debug('adaptive_exits update skip: %s', _aex_upd_e)
            else:
                log.info('%s: OK | Z=%.2f P&L=%+.2f%% hold=%.1fч',
                         pair, z, pnl, hours_in)

        except Exception:
            log.error('%s:\n%s', pair, traceback.format_exc())


# ── P1.5 FIX: Graceful shutdown ─────────────────────────────────────────────
# Без обработки SIGTERM/SIGINT daemon может прервать цикл посередине
# записи positions.json, повредив файл. Event.wait() позволяет прервать
# sleep между циклами без ожидания полного INTERVAL.
_shutdown_event = threading.Event()

def _sigterm_handler(signum, frame):
    _sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    log.info('Получен сигнал %s — завершаю текущий цикл...', _sig_name)
    _shutdown_event.set()

signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT, _sigterm_handler)


def _run_startup_reconciliation():
    """BUG-01 FIX: При старте daemon сравнить позиции Bybit с positions.json.

    Обнаруживает 'сиротские' позиции на бирже, оставшиеся после аварийного
    завершения daemon (OOM kill, SIGKILL, сбой питания).
    Сиротская позиция = есть на Bybit, нет в positions.json → голая позиция
    без SL/TP/trailing, неограниченный убыток.

    Действие: алерт в Telegram + запись в лог. НЕ закрывает автоматически —
    оператор должен решить (закрыть или импортировать).
    """
    if _bybit_exec is None:
        log.info('Reconciliation: Bybit executor отключён — пропуск')
        return

    try:
        bybit_positions = _bybit_exec.get_all_positions()
        if not bybit_positions:
            log.info('Reconciliation: нет позиций на Bybit Demo — OK')
            return

        # Собрать символы всех OPEN позиций из positions.json
        local_pos = [p for p in load_positions()
                     if isinstance(p, dict) and p.get('status') == 'OPEN']
        local_syms = set()
        for p in local_pos:
            c1 = p.get('coin1', '').upper()
            c2 = p.get('coin2', '').upper()
            if c1:
                local_syms.add(f"{c1}USDT")
            if c2:
                local_syms.add(f"{c2}USDT")

        orphans = []
        for bp in bybit_positions:
            sym = bp.get('symbol', '')
            if sym and sym not in local_syms:
                orphans.append(bp)

        if orphans:
            for orph in orphans:
                msg = (
                    f"🚨 ORPHAN на Bybit Demo: {orph['symbol']} "
                    f"{orph['side']} qty={orph['size']} "
                    f"avgPrice={orph.get('avgPrice', 0):.4f} "
                    f"unrealisedPnl={orph.get('unrealisedPnl', 0):.4f}\n"
                    f"Нет соответствия в positions.json — закройте вручную!"
                )
                log.error(msg)
                alert(msg)
            log.warning('Reconciliation: найдено %d сиротских позиций!', len(orphans))
        else:
            log.info('Reconciliation: %d Bybit позиций, все совпадают с %d локальными — OK',
                     len(bybit_positions), len(local_pos))

    except Exception as e:
        log.warning('Reconciliation ошибка: %s', e)


def main():
    log.info('=' * 55)
    log.info('Monitor Daemon запущен (открытие + закрытие)')
    log.info('Exchange: %s | Интервал: %ds', EXCHANGE, INTERVAL)
    log.info('Import dir: %s', _MONITOR_IMPORT_DIR)
    log.info('=' * 55)

    # DEADLOCK-FIX: удаляем зависший .lock файл от предыдущего запуска.
    # Если daemon был убит (SIGKILL, OOM) держа файловый лок — следующий
    # запуск будет висеть на _positions_write_lock бесконечно.
    _lock_path = os.path.join(_BASE_DIR, 'positions.json.lock')
    if os.path.exists(_lock_path):
        try:
            os.remove(_lock_path)
            log.info('DEADLOCK-FIX: удалён старый lock-файл %s', _lock_path)
        except OSError as _le:
            log.warning('Не удалось удалить lock-файл: %s', _le)

    # BUG-01 FIX: проверить сиротские позиции на Bybit при старте
    _run_startup_reconciliation()

    alert('Monitor Daemon запущен (открытие + закрытие)')

    # WATCHDOG-FIX: максимальное время одного цикла = INTERVAL * 3 (но не менее 180с).
    # Если process_pending/monitor_open_positions зависнут (сеть, лок, ccxt) —
    # ThreadPoolExecutor прервёт ожидание и daemon продолжит следующий цикл.
    _cycle_hard_timeout = max(180, INTERVAL * 3)
    log.info('Watchdog таймаут цикла: %ds', _cycle_hard_timeout)

    from concurrent.futures import ThreadPoolExecutor as _WD_TPE, TimeoutError as _WD_Timeout

    while not _shutdown_event.is_set():
        t0 = time.monotonic()
        global _cycle_start_ts
        _cycle_start_ts = time.time()
        try:
            # WATCHDOG-FIX: запускаем цикл в отдельном потоке с жёстким таймаутом.
            # Основной поток сохраняет способность обработать SIGTERM через _shutdown_event.
            def _run_cycle():
                _set_op('process_pending: старт')
                process_pending()
                _set_op('monitor_open_positions: старт')
                monitor_open_positions()
                _set_op('цикл завершён')

            with _WD_TPE(max_workers=1) as _wd_pool:
                _wd_fut = _wd_pool.submit(_run_cycle)
                try:
                    _wd_fut.result(timeout=_cycle_hard_timeout)
                except _WD_Timeout:
                    log.critical(
                        'WATCHDOG: цикл превысил %ds — возможное зависание! '
                        'Проверьте сеть / файловый лок / ccxt. Продолжаю следующий цикл.',
                        _cycle_hard_timeout
                    )
                    alert(f'⚠️ Daemon WATCHDOG: цикл завис (>{_cycle_hard_timeout}с), пропускаю')
                except Exception:
                    log.critical('Критическая ошибка:\n%s', traceback.format_exc())
                    alert(f'Daemon ОШИБКА (см. {LOG_FILE})')
        except Exception:
            log.critical('Критическая ошибка вне цикла:\n%s', traceback.format_exc())
            alert(f'Daemon ОШИБКА (см. {LOG_FILE})')


        # MEM-FIX-v2: периодическая сборка мусора каждые 60 циклов (~1ч при INTERVAL=60)
        _daemon_cycle = globals().get("_daemon_cycle_count", 0) + 1
        globals()["_daemon_cycle_count"] = _daemon_cycle
        if _daemon_cycle % 60 == 0:
            import gc as _gc_daemon
            _gc_daemon.collect()
            # ВАЖНО: _session_state_store НЕ очищаем — там runtime-состояния между циклами.
            # Очищаем только заведомо временные ключи с префиксом '_tmp_':
            _tmp_keys = [k for k in _session_state_store if k.startswith('_tmp_')]
            for _k in _tmp_keys:
                del _session_state_store[_k]
            # Перезагружаем adaptive модули раз в час чтобы подхватить обновлённые параметры
            global _aex_mod, _aq_mod
            _aex_mod = None
            _aq_mod = None
            _load_adaptive_modules()
            log.debug("Периодическая очистка памяти (цикл %d)", _daemon_cycle)

        elapsed = time.monotonic() - t0
        sleep   = max(0, INTERVAL - elapsed)
        log.debug('Цикл %.1fс, следующий через %.0fс', elapsed, sleep)
        # P1.5 FIX: wait с возможностью прерывания (вместо time.sleep)
        _shutdown_event.wait(timeout=sleep)

    log.info('Daemon остановлен корректно')
    alert('Monitor Daemon остановлен')


if __name__ == '__main__':
    main()
