"""
engine/scanner.py — Scan pipeline helpers.

Ноль Streamlit. Подготовка для вынесения scan_pairs() из app.py.

Текущая волна: только helpers и интерфейс.
scan_pairs() остаётся в app.py до Волны 5 (UI thinning).

Helpers:
  filter_coins()       — отсечь стейблкоины, wrapped
  build_pair_list()    — генерация пар из top-N монет
  should_skip_pair()   — pre-flight check по HR/whitelist/duplicates
  calc_scan_bars()     — lookback в барах из дней + timeframe
"""

from __future__ import annotations
from ..core.risk import is_hr_safe, is_whitelisted


EXCLUDE_COINS = {
    'USDC', 'USDT', 'USDG', 'DAI', 'TUSD', 'BUSD', 'FDUSD',
    'STETH', 'WSTETH', 'WETH', 'WBTC', 'CBETH', 'RETH',
    'OKSOL', 'JITOSOL', 'MSOL', 'BNSOL', 'BETH',
}


def filter_coins(coins: list[str]) -> list[str]:
    """Remove stablecoins, wrapped tokens, etc."""
    return [c for c in coins if c.upper() not in EXCLUDE_COINS]


def calc_scan_bars(lookback_days: int, timeframe: str) -> int:
    """Convert lookback in days → number of bars for fetch_ohlcv."""
    hours_per_bar = {'1h': 1, '4h': 4, '1d': 24}.get(timeframe, 4)
    return int(lookback_days * 24 / hours_per_bar)


def build_pair_list(coins: list[str], max_same_coin: int = 3) -> list[tuple[str, str]]:
    """Generate all unique pairs from coin list.

    Returns: [(coin1, coin2), ...] sorted alphabetically.
    """
    pairs = []
    for i, c1 in enumerate(coins):
        for c2 in coins[i+1:]:
            pairs.append((c1, c2))
    return pairs


def should_skip_pair(
    coin1: str,
    coin2: str,
    hedge_ratio: float | None,
    direction: str = "BOTH",
    wl_pairs: set | None = None,
    config_whitelist: list | None = None,
    min_hr: float = 0.05,
    max_hr: float = 5.0,
) -> tuple[bool, str]:
    """Pre-flight check: HR safety + whitelist.

    Returns: (skip: bool, reason: str)
    """
    # HR check
    if hedge_ratio is not None:
        hr_ok, hr_reason = is_hr_safe(hedge_ratio, min_hr=min_hr, max_hr=max_hr)
        if not hr_ok:
            return True, hr_reason

    # Whitelist check
    if wl_pairs is not None or config_whitelist is not None:
        if not is_whitelisted(coin1, coin2, direction, wl_pairs, config_whitelist):
            return True, f"{coin1}/{coin2} не в whitelist"

    return False, ""


def estimate_scan_time(n_coins: int, timeframe: str = '4h') -> str:
    """Estimate scan duration for user display.

    Based on empirical data: ~0.5s per pair (API + analysis).
    """
    n_pairs = n_coins * (n_coins - 1) // 2
    seconds = n_pairs * 0.5
    if seconds < 60:
        return f"~{seconds:.0f}с ({n_pairs} пар)"
    minutes = seconds / 60
    return f"~{minutes:.0f} мин ({n_pairs} пар)"
