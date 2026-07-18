"""
infra/exchange.py — Единые CCXT-обёртки: fetch_ohlcv, get_current_price.

Извлечено из monitor_v38_3.py и app.py (Волна 3).
Ранее fetch_prices дублировался в обоих файлах.
НОЛЬ импортов Streamlit — кеширование через TTL dict, не st.cache_data.

BUG-N16 FIX: TTL по таймфрейму (1h=60s, 4h=180s, 1d=600s).
P1-FIX: timeout 8s, retry 2x без sleep.
"""

import time
import logging
import threading

_logger = logging.getLogger("infra.exchange")

EXCHANGE_FALLBACK = ['okx', 'kucoin', 'bybit', 'binance']
EXCLUDE_COINS = {
    'USDC', 'USDT', 'USDG', 'DAI', 'TUSD', 'BUSD', 'FDUSD',
    'STETH', 'WSTETH', 'WETH', 'WBTC', 'CBETH', 'RETH',
    'OKSOL', 'JITOSOL', 'MSOL', 'BNSOL', 'BETH',
}

# TTL per timeframe (BUG-N16 FIX)
_TTL_MAP = {'1h': 60, '4h': 180, '1d': 600}
_DEFAULT_TTL = 120


# ═══════════════════════════════════════════════════════
# EXCHANGE CACHE
# ═══════════════════════════════════════════════════════

_exchange_cache = {}
_exchange_lock = threading.Lock()


def get_exchange(exchange_name: str):
    """Get CCXT exchange object with fallback chain and caching.
    
    Returns: (exchange_obj, actual_name) or (None, None).
    Cache TTL: 5 minutes.
    """
    with _exchange_lock:
        if exchange_name in _exchange_cache:
            cached = _exchange_cache[exchange_name]
            if time.time() - cached['ts'] < 300:
                return cached['ex'], cached['name']

    try:
        import ccxt
    except ImportError:
        _logger.error("ccxt not installed: pip install ccxt")
        return None, None

    tried = set()
    chain = [exchange_name] + [e for e in EXCHANGE_FALLBACK if e != exchange_name]
    for exch in chain:
        if exch in tried:
            continue
        tried.add(exch)
        try:
            ex = getattr(ccxt, exch)({
                'enableRateLimit': True,
                'timeout': 8000,   # P1-FIX: 8s timeout
            })
            ex.load_markets()
            with _exchange_lock:
                _exchange_cache[exchange_name] = {'ex': ex, 'name': exch, 'ts': time.time()}
            if exch != exchange_name:
                _logger.warning("%s недоступен → %s (fallback)", exchange_name.upper(), exch.upper())
            return ex, exch
        except Exception:
            continue

    return None, None


# ═══════════════════════════════════════════════════════
# PRICE CACHE (replaces st.cache_data)
# ═══════════════════════════════════════════════════════

_price_cache = {}
_price_cache_lock = threading.Lock()
_MAX_CACHE_ENTRIES = 200


def _cache_key(exchange_name, coin, timeframe, lookback_bars):
    return f"{exchange_name}:{coin}:{timeframe}:{lookback_bars}"


def _get_cached(key, ttl):
    with _price_cache_lock:
        entry = _price_cache.get(key)
        if entry and (time.time() - entry['ts']) < ttl:
            return entry['data']
    return None


def _set_cached(key, data):
    with _price_cache_lock:
        # Evict oldest if cache is too large
        if len(_price_cache) >= _MAX_CACHE_ENTRIES:
            oldest_key = min(_price_cache, key=lambda k: _price_cache[k]['ts'])
            del _price_cache[oldest_key]
        _price_cache[key] = {'data': data, 'ts': time.time()}


def invalidate_cache():
    """Clear all cached prices (useful after config change)."""
    with _price_cache_lock:
        _price_cache.clear()


# ═══════════════════════════════════════════════════════
# FETCH PRICES
# ═══════════════════════════════════════════════════════

def _fetch_prices_impl(exchange_name, coin, timeframe, lookback_bars=300):
    """Core fetch: CCXT → DataFrame. No caching — caller handles TTL.
    P1-FIX: retry 2x without sleep, futures symbol first."""
    try:
        import ccxt as _ccxt
        import pandas as pd
    except ImportError:
        _logger.error("ccxt/pandas not installed")
        return None

    symbols = [f"{coin}/USDT:USDT", f"{coin}/USDT"]
    for symbol in symbols:
        for _attempt in range(2):
            try:
                ex, actual = get_exchange(exchange_name)
                if ex is None:
                    return None
                ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=lookback_bars)
                df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
                df['ts'] = pd.to_datetime(df['ts'], unit='ms')
                return df
            except (_ccxt.NetworkError, _ccxt.RequestTimeout, _ccxt.ExchangeNotAvailable):
                continue
            except Exception:
                break
    return None


def fetch_prices(exchange_name, coin, timeframe, lookback_bars=300):
    """Fetch OHLCV with TTL cache per timeframe (BUG-N16 FIX).
    
    1h  → 60s cache
    4h  → 180s cache
    1d  → 600s cache
    other → 120s cache
    """
    ttl = _TTL_MAP.get(timeframe, _DEFAULT_TTL)
    key = _cache_key(exchange_name, coin, timeframe, lookback_bars)
    
    cached = _get_cached(key, ttl)
    if cached is not None:
        return cached

    data = _fetch_prices_impl(exchange_name, coin, timeframe, lookback_bars)
    if data is not None:
        _set_cached(key, data)
    return data


def get_current_price(exchange_name, coin):
    """Get last price for coin. Retry 2x, futures first.
    P1-FIX: no sleep between retries."""
    try:
        import ccxt as _ccxt
    except ImportError:
        return None

    symbols = [f"{coin}/USDT:USDT", f"{coin}/USDT"]
    for symbol in symbols:
        for _attempt in range(2):
            try:
                ex, actual = get_exchange(exchange_name)
                if ex is None:
                    return None
                ticker = ex.fetch_ticker(symbol)
                return ticker['last']
            except (_ccxt.NetworkError, _ccxt.RequestTimeout, _ccxt.ExchangeNotAvailable):
                continue
            except Exception:
                break
    return None


def get_top_coins(exchange_name, n=70, quote='USDT'):
    """Get top N coins by 24h volume from exchange.
    Returns: list[str] of coin tickers (e.g. ['BTC', 'ETH', ...])"""
    try:
        import ccxt as _ccxt
    except ImportError:
        return []

    ex, _ = get_exchange(exchange_name)
    if ex is None:
        return []

    try:
        tickers = ex.fetch_tickers()
        pairs = []
        for sym, t in tickers.items():
            if f'/{quote}' not in sym:
                continue
            coin = sym.split('/')[0]
            if coin in EXCLUDE_COINS:
                continue
            vol = float(t.get('quoteVolume', 0) or 0)
            pairs.append((coin, vol))
        pairs.sort(key=lambda x: -x[1])
        return [p[0] for p in pairs[:n]]
    except Exception as e:
        _logger.error("get_top_coins failed: %s", e)
        return []
