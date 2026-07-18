"""
core/pair_analysis.py — Pipeline-функции анализа пар.

Извлечено из mean_reversion_analysis.py (Волна 4/5).
Содержит ВСЕ math-функции для анализа одной пары:
  Hurst (DFA, EMA, expanding), Z-score (adaptive, GARCH, rolling),
  OU parameters, Kalman HR, ADF, FDR, Johansen, cointegration stability,
  spread regime, CUSUM, crossing density, correlation, PCA.

НОЛЬ импортов Streamlit. НОЛЬ вызовов CFG().
Все параметры — явные аргументы.

Зависимости: numpy, scipy, statsmodels (optional для johansen/ADF).
"""

import numpy as np
from scipy import stats


def calculate_hurst_exponent(time_series, min_window=8):
    """DFA на инкрементах. Возвращает 0.5 при fallback.
    
    v10.4: min_window=8 (was 4). Меньшее значение давало нестабильные
    результаты на 100-300 свечах, что приводило к Hurst=0.085 на 35d
    и Hurst=0.5 (fallback) на 63d для одной и той же пары.
    """
    ts = np.array(time_series, dtype=float)
    n = len(ts)
    if n < 30:
        return 0.5

    increments = np.diff(ts)
    n_inc = len(increments)
    profile = np.cumsum(increments - np.mean(increments))

    max_window = n_inc // 4
    if max_window <= min_window:
        return 0.5

    num_points = min(20, max_window - min_window)
    if num_points < 4:
        return 0.5

    window_sizes = np.unique(
        np.logspace(np.log10(min_window), np.log10(max_window), num=num_points).astype(int)
    )
    window_sizes = window_sizes[window_sizes >= min_window]
    if len(window_sizes) < 4:
        return 0.5

    fluctuations = []
    for w in window_sizes:
        n_seg = n_inc // w
        if n_seg < 2:
            continue
        f2_sum, count = 0.0, 0
        for seg in range(n_seg):
            segment = profile[seg * w:(seg + 1) * w]
            x = np.arange(w, dtype=float)
            coeffs = np.polyfit(x, segment, 1)
            f2_sum += np.mean((segment - np.polyval(coeffs, x)) ** 2)
            count += 1
        for seg in range(n_seg):
            start = n_inc - (seg + 1) * w
            if start < 0:
                break
            segment = profile[start:start + w]
            x = np.arange(w, dtype=float)
            coeffs = np.polyfit(x, segment, 1)
            f2_sum += np.mean((segment - np.polyval(coeffs, x)) ** 2)
            count += 1
        if count > 0:
            f_n = np.sqrt(f2_sum / count)
            if f_n > 1e-15:
                fluctuations.append((w, f_n))

    if len(fluctuations) < 4:
        return 0.5

    log_n = np.log([f[0] for f in fluctuations])
    log_f = np.log([f[1] for f in fluctuations])

    try:
        slope, _, r_value, _, _ = stats.linregress(log_n, log_f)
        if r_value ** 2 < 0.70:
            return 0.5
        return round(max(0.01, min(0.99, slope)), 4)
    except Exception:
        return 0.5


def calculate_hurst_ema(spread, n_subwindows=8, ema_span=5):
    """
    v16.0: Smoothed Hurst via EMA of sub-window DFA estimates.
    
    Problem: Single DFA on 300 bars is unstable — one large candle shifts result.
    Solution: Calculate Hurst on N overlapping sub-windows, then EMA-smooth.
    
    Algorithm:
      1. Split spread into N overlapping windows (75% overlap)
      2. Calculate DFA Hurst on each sub-window
      3. Apply EMA(span=5) to smooth
      4. Return: current EMA value + raw series for diagnostics
    
    Args:
        spread: full spread array (300+ bars)
        n_subwindows: number of sub-windows (default 8)
        ema_span: EMA smoothing period (default 5)
    
    Returns:
        dict: {
            'hurst_ema': float (smoothed current Hurst),
            'hurst_raw': float (un-smoothed current Hurst),
            'hurst_series': list of raw Hurst values per sub-window,
            'hurst_std': float (std of raw values — stability indicator),
            'is_stable': bool (std < 0.08 = stable)
        }
    """
    spread = np.array(spread, float)
    n = len(spread)
    
    if n < 80:
        h_raw = calculate_hurst_exponent(spread)
        return {
            'hurst_ema': h_raw, 'hurst_raw': h_raw,
            'hurst_series': [h_raw], 'hurst_std': 0.0, 'is_stable': True
        }
    
    # Sub-windows: size = 60% of total, step = 40/n_subwindows of total
    win_size = max(60, int(n * 0.60))
    step = max(1, (n - win_size) // max(1, n_subwindows - 1))
    
    hurst_values = []
    for i in range(n_subwindows):
        start = min(i * step, n - win_size)
        sub = spread[start:start + win_size]
        if len(sub) >= 50:
            h = calculate_hurst_exponent(sub)
            hurst_values.append(h)
    
    if not hurst_values:
        h_raw = calculate_hurst_exponent(spread)
        return {
            'hurst_ema': h_raw, 'hurst_raw': h_raw,
            'hurst_series': [h_raw], 'hurst_std': 0.0, 'is_stable': True
        }
    
    # EMA smoothing
    h_raw = hurst_values[-1]
    alpha = 2.0 / (ema_span + 1)
    ema_val = hurst_values[0]
    for h in hurst_values[1:]:
        ema_val = alpha * h + (1 - alpha) * ema_val
    
    h_std = float(np.std(hurst_values))
    
    return {
        'hurst_ema': round(ema_val, 4),
        'hurst_raw': round(h_raw, 4),
        'hurst_series': [round(h, 4) for h in hurst_values],
        'hurst_std': round(h_std, 4),
        'is_stable': h_std < 0.08,
    }


def calculate_hurst_expanding(spread, scales=None):
    """
    v19.1: Expanding Window Hurst — multi-scale analysis.
    
    Calculates Hurst at increasing window sizes to detect regime changes:
      H₁₀₀ → H₁₅₀ → H₂₀₀ → H₂₅₀ → H₃₀₀
    
    Interpretation:
      H₁₀₀ ≈ H₃₀₀ → stable mean-reversion (safe to trade)
      H₁₀₀ >> H₃₀₀ → MR weakening over time (DANGER)
      H₁₀₀ << H₃₀₀ → MR strengthening (ideal entry)
      hurst_slope > +0.1 → regime shifting toward trending
      hurst_slope < -0.1 → regime shifting toward mean-reversion
    
    Returns: dict with scale results, slope, and assessment
    """
    spread = np.array(spread, float)
    n = len(spread)
    
    if scales is None:
        # Default: 5 scales from 60 to full length
        min_scale = max(60, n // 5)
        scales = list(range(min_scale, n + 1, max(1, (n - min_scale) // 4)))
        if scales[-1] != n:
            scales.append(n)
        # Ensure at least 3 scales
        if len(scales) < 3:
            scales = [max(60, n // 3), max(80, n * 2 // 3), n]
    
    # Calculate Hurst at each scale
    scale_results = []
    for s in scales:
        if s < 40:
            continue
        # Use the LAST s bars (most recent data)
        sub = spread[-s:]
        h = calculate_hurst_exponent(sub)
        scale_results.append({'bars': s, 'hurst': round(h, 4)})
    
    if len(scale_results) < 2:
        h_full = calculate_hurst_exponent(spread)
        return {
            'scales': [{'bars': n, 'hurst': round(h_full, 4)}],
            'hurst_slope': 0.0,
            'hurst_short': round(h_full, 4),
            'hurst_long': round(h_full, 4),
            'assessment': 'INSUFFICIENT_DATA',
            'mr_strengthening': False,
            'mr_weakening': False,
        }
    
    # Linear regression: Hurst vs normalized scale index
    hursts = np.array([r['hurst'] for r in scale_results])
    x = np.linspace(0, 1, len(hursts))
    
    if len(hursts) >= 2:
        slope = float(np.polyfit(x, hursts, 1)[0])
    else:
        slope = 0.0
    
    h_short = scale_results[0]['hurst']  # Shortest scale (recent)
    h_long = scale_results[-1]['hurst']  # Full scale
    
    # Assessment
    mr_strengthening = slope < -0.05 and h_short < h_long
    mr_weakening = slope > 0.05 and h_short > h_long
    
    if abs(slope) < 0.03:
        assessment = 'STABLE'
    elif mr_strengthening:
        assessment = 'MR_STRENGTHENING'
    elif mr_weakening:
        assessment = 'MR_WEAKENING'
    elif slope > 0.1:
        assessment = 'TRENDING_SHIFT'
    else:
        assessment = 'MIXED'
    
    return {
        'scales': scale_results,
        'hurst_slope': round(slope, 4),
        'hurst_short': h_short,
        'hurst_long': h_long,
        'assessment': assessment,
        'mr_strengthening': mr_strengthening,
        'mr_weakening': mr_weakening,
    }


# =============================================================================
# ROLLING Z-SCORE
# =============================================================================

def calculate_rolling_zscore(spread, window=30):
    """Rolling Z-score без lookahead bias. LEGACY — используйте adaptive версию."""
    spread = np.array(spread, dtype=float)
    n = len(spread)
    if n < window + 1:
        mean, std = np.mean(spread), np.std(spread)
        if std < 1e-10:
            return 0.0, np.zeros(n)
        zs = (spread - mean) / std
        return float(zs[-1]), zs

    zscore_series = np.full(n, np.nan)
    for i in range(window, n):
        lb = spread[i - window:i]
        m, s = np.mean(lb), np.std(lb)
        zscore_series[i] = (spread[i] - m) / s if s > 1e-10 else 0.0

    cz = zscore_series[-1]
    return float(0.0 if np.isnan(cz) else cz), zscore_series


def calculate_adaptive_robust_zscore(spread, halflife_bars=None, min_w=10, max_w=60):
    """
    Адаптивный робастный Z-score.

    Два улучшения над calculate_rolling_zscore:
      1. Адаптивное окно: Window = clip(2.5 × HL_bars, min_w, max_w)
         Синхронизирует Z-score с ритмом конкретной пары.
      2. MAD вместо std: устойчив к выбросам (fat tails крипто).
         MAD * 1.4826 ≈ sigma для нормального распределения.

    Args:
        spread: массив спреда
        halflife_bars: HL в барах (не часах!). None → default window.
        min_w: минимальное окно (10 — порог стабильности)
        max_w: максимальное окно (60 — не слишком далеко)

    Returns:
        (current_z, z_series, window_used)
    """
    spread = np.array(spread, dtype=float)
    n = len(spread)

    # 1. Адаптивное окно
    if halflife_bars is not None and 0 < halflife_bars < 500:
        window = int(np.clip(round(2.5 * halflife_bars), min_w, max_w))
    else:
        window = 30  # fallback

    if n < window + 1:
        # Мало данных — простой Z
        med = np.median(spread)
        mad = np.median(np.abs(spread - med)) * 1.4826
        if mad < 1e-10:
            return 0.0, np.zeros(n), window
        zs = (spread - med) / mad
        return float(zs[-1]), zs, window

    # 2. Rolling MAD Z-score
    zscore_series = np.full(n, np.nan)
    for i in range(window, n):
        lb = spread[i - window:i]
        med = np.median(lb)
        mad = np.median(np.abs(lb - med)) * 1.4826

        if mad < 1e-10:
            # fallback на std если MAD = 0 (стейблкоины)
            s = np.std(lb)
            zscore_series[i] = (spread[i] - np.mean(lb)) / s if s > 1e-10 else 0.0
        else:
            zscore_series[i] = (spread[i] - med) / mad

    cz = zscore_series[-1]
    return float(0.0 if np.isnan(cz) else cz), zscore_series, window


def calculate_garch_zscore(spread, halflife_bars=None, lam=0.94, min_w=10, max_w=60):
    """
    v18.0: GARCH-like Z-score using EWMA volatility (RiskMetrics λ=0.94).
    
    Problem: Standard Z uses fixed-window σ. When σ suddenly increases,
    Z collapses toward 0 even though spread hasn't converged (SUI/AVAX bug).
    
    Solution: EWMA volatility adapts to recent regime:
      σ²(t) = λ·σ²(t-1) + (1-λ)·(spread(t) - μ(t))²
      Z_garch(t) = (spread(t) - μ_rolling(t)) / σ_ewma(t)
    
    When σ increases (vol spike), Z_garch adjusts immediately, preventing
    false convergence signals. When σ is stable, Z_garch ≈ Z_standard.
    
    Also returns: vol_ratio = σ_ewma / σ_rolling. If > 1.5 → variance collapse warning.
    
    Args:
        spread: spread array
        halflife_bars: for rolling median window
        lam: EWMA decay factor (0.94 = RiskMetrics standard)
        min_w, max_w: window bounds for rolling median
    
    Returns:
        dict: {
            'z_garch': float (current GARCH Z),
            'z_standard': float (standard MAD Z for comparison),
            'vol_ratio': float (σ_ewma / σ_mad, >1.5 = regime shift),
            'sigma_ewma': float (current EWMA volatility),
            'z_garch_series': array,
            'z_divergence': float (|z_standard - z_garch|),
            'variance_expanding': bool (σ_ewma growing > 20% in last 10 bars),
        }
    """
    spread = np.array(spread, float)
    n = len(spread)
    
    # Window from halflife
    if halflife_bars and 0 < halflife_bars < 500:
        window = int(np.clip(round(2.5 * halflife_bars), min_w, max_w))
    else:
        window = 30
    
    if n < window + 5:
        return {
            'z_garch': 0.0, 'z_standard': 0.0, 'vol_ratio': 1.0,
            'sigma_ewma': 0.0, 'z_garch_series': np.zeros(n),
            'z_divergence': 0.0, 'variance_expanding': False,
        }
    
    # 1. Rolling median (center)
    med_series = np.full(n, np.nan)
    for i in range(window, n):
        med_series[i] = np.median(spread[i - window:i])
    
    # 2. EWMA variance
    sigma2_ewma = np.full(n, np.nan)
    # Initialize with first window variance
    init_var = np.var(spread[:window]) if window <= n else np.var(spread)
    sigma2_ewma[window] = max(init_var, 1e-12)
    
    for i in range(window + 1, n):
        residual = spread[i] - med_series[i] if not np.isnan(med_series[i]) else 0
        sigma2_ewma[i] = lam * sigma2_ewma[i-1] + (1 - lam) * residual**2
    
    # 3. GARCH Z-score
    z_garch_series = np.full(n, np.nan)
    for i in range(window + 1, n):
        sigma = np.sqrt(max(sigma2_ewma[i], 1e-12))
        if sigma > 1e-10 and not np.isnan(med_series[i]):
            z_garch_series[i] = (spread[i] - med_series[i]) / sigma
    
    # 4. Standard MAD Z for comparison
    z_std, z_std_series, _ = calculate_adaptive_robust_zscore(
        spread, halflife_bars=halflife_bars, min_w=min_w, max_w=max_w)
    
    # Current values
    z_g = z_garch_series[-1] if not np.isnan(z_garch_series[-1]) else 0.0
    sigma_now = np.sqrt(sigma2_ewma[-1]) if not np.isnan(sigma2_ewma[-1]) else 0
    
    # MAD for comparison
    lb = spread[-window:]
    mad_now = np.median(np.abs(lb - np.median(lb))) * 1.4826
    vol_ratio = sigma_now / mad_now if mad_now > 1e-10 else 1.0
    
    # Variance expanding? Check if σ_ewma grew > 20% over last 10 bars
    lookback = min(10, n - window - 2)
    variance_expanding = False
    if lookback > 2:
        s_old = np.sqrt(sigma2_ewma[-lookback]) if not np.isnan(sigma2_ewma[-lookback]) else sigma_now
        if s_old > 1e-10:
            variance_expanding = (sigma_now / s_old - 1.0) > 0.20
    
    return {
        'z_garch': round(float(z_g), 4),
        'z_standard': round(float(z_std), 4),
        'vol_ratio': round(float(vol_ratio), 3),
        'sigma_ewma': round(float(sigma_now), 6),
        'z_garch_series': z_garch_series,
        'z_divergence': round(abs(float(z_std) - float(z_g)), 3),
        'variance_expanding': variance_expanding,
    }


def calculate_crossing_density(zscore_series, window=None):
    """
    Частота пересечений нуля Z-score.

    Показывает как часто спред реально переходит через mean.
    Высокая плотность → пара активно mean-reverting.
    Низкая плотность → спред "застрял" на одной стороне.

    Args:
        zscore_series: массив Z-scores
        window: количество последних баров для анализа

    Returns:
        float: плотность (0.0–1.0). 0.05 = 5% баров содержат кроссинг.
    """
    z = np.array(zscore_series, dtype=float)
    z = z[~np.isnan(z)]

    if len(z) < 10:
        return 0.0

    if window is not None and len(z) > window:
        z = z[-window:]

    # Считаем смены знака
    signs = np.sign(z)
    # Убираем нули (на нуле — не считается сменой)
    signs = signs[signs != 0]
    if len(signs) < 2:
        return 0.0

    crossings = np.sum(np.abs(np.diff(signs)) > 0)
    return float(crossings / len(signs))


def calculate_rolling_correlation(series1, series2, window=30):
    """
    Rolling корреляция Пирсона между двумя ценовыми рядами.

    НЕ используется как фильтр (коинтегрированные пары могут
    временно раскоррелироваться — это момент входа).
    Показывается в UI как информационный индикатор.

    Returns:
        (current_corr, corr_series)
    """
    s1 = np.array(series1, dtype=float)
    s2 = np.array(series2, dtype=float)
    n = min(len(s1), len(s2))

    if n < window + 1:
        if n > 5:
            return float(np.corrcoef(s1[:n], s2[:n])[0, 1]), np.array([])
        return 0.0, np.array([])

    s1, s2 = s1[:n], s2[:n]
    corr_series = np.full(n, np.nan)

    for i in range(window, n):
        x = s1[i - window:i]
        y = s2[i - window:i]
        sx, sy = np.std(x), np.std(y)
        if sx > 1e-10 and sy > 1e-10:
            corr_series[i] = np.corrcoef(x, y)[0, 1]

    cc = corr_series[-1]
    return float(0.0 if np.isnan(cc) else cc), corr_series


# =============================================================================
# OU PARAMETERS
# =============================================================================

def calculate_ou_parameters(spread, dt=1.0):
    """OU: dX = θ(μ - X)dt + σdW"""
    try:
        if len(spread) < 20:
            return None
        spread = np.array(spread, dtype=float)
        y, x = np.diff(spread), spread[:-1]
        n = len(x)
        sx, sy = np.sum(x), np.sum(y)
        sxy, sx2 = np.sum(x * y), np.sum(x ** 2)
        denom = n * sx2 - sx ** 2
        if abs(denom) < 1e-10:
            return None
        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n
        theta = max(0.001, min(10.0, -b / dt))
        mu = a / theta if theta > 0 else 0.0
        y_pred = a + b * x
        sigma = np.std(y - y_pred)
        halflife = np.log(2) / theta if theta > 0 else 999.0
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        ss_res = np.sum((y - y_pred) ** 2)
        r_sq = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        return {
            'theta': float(theta), 'mu': float(mu), 'sigma': float(sigma),
            'halflife_ou': float(halflife), 'r_squared': float(r_sq),
            'equilibrium_time': float(-np.log(0.05) / theta if theta > 0 else 999.0)
        }
    except Exception:
        return None


# =============================================================================
# KALMAN FILTER для адаптивного HEDGE RATIO
# =============================================================================

def kalman_hedge_ratio(series1, series2, delta=1e-4, ve=1e-3):
    """
    Kalman Filter для динамического hedge ratio.

    Модель:
      State:  β_t = [intercept_t, hedge_ratio_t]
      Transition: β_t = β_{t-1} + w_t,  w ~ N(0, Q)
      Observation: price1_t = intercept_t + hedge_ratio_t * price2_t + v_t

    Args:
        series1, series2: ценовые ряды (np.array или pd.Series)
        delta: дисперсия перехода (процесс случайного блуждания для β).
               Маленький delta = гладкий HR, большой = быстрая адаптация.
               Default 1e-4 — хороший баланс для 4h крипто.
        ve: начальная дисперсия наблюдения (measurement noise).

    Returns:
        dict:
            hedge_ratios:  np.array — HR на каждом баре
            intercepts:    np.array — intercept на каждом баре
            spread:        np.array — адаптивный спред
            hr_final:      float — текущий (последний) HR
            intercept_final: float
            hr_std:        float — uncertainty текущего HR
            sqrt_Q:        np.array — серия measurement prediction errors
    """
    s1 = np.array(series1, dtype=float)
    s2 = np.array(series2, dtype=float)
    n = min(len(s1), len(s2))

    if n < 10:
        return None

    s1, s2 = s1[:n], s2[:n]

    # State: [intercept, hedge_ratio]
    # Начальная оценка через OLS на первых 30 барах
    init_n = min(30, n // 3)
    try:
        X_init = np.column_stack([np.ones(init_n), s2[:init_n]])
        beta_init = np.linalg.lstsq(X_init, s1[:init_n], rcond=None)[0]
    except Exception:
        beta_init = np.array([0.0, 1.0])

    # Kalman state
    beta = beta_init.copy()          # [2,] state estimate
    P = np.eye(2) * 1.0              # [2,2] state covariance
    Q = np.eye(2) * delta            # [2,2] transition noise
    R = ve                            # scalar observation noise

    # Storage
    hedge_ratios = np.zeros(n)
    intercepts = np.zeros(n)
    innovations = np.zeros(n)    # Kalman innovations (≈ белый шум)
    trading_spread = np.zeros(n) # Торговый спред для Z-score
    sqrt_Q_series = np.zeros(n)

    for t in range(n):
        # Observation vector: x_t = [1, price2_t]
        x_t = np.array([1.0, s2[t]])

        # Predict
        # beta = beta (random walk)
        P = P + Q

        # Update
        y_hat = x_t @ beta                  # predicted price1
        e_t = s1[t] - y_hat                 # innovation
        S_t = x_t @ P @ x_t + R            # innovation variance
        K_t = P @ x_t / S_t                 # Kalman gain [2,]

        beta = beta + K_t * e_t             # state update
        P = P - np.outer(K_t, x_t) @ P     # covariance update

        # Ensure P stays positive definite
        P = (P + P.T) / 2
        np.fill_diagonal(P, np.maximum(np.diag(P), 1e-10))

        # Store
        intercepts[t] = beta[0]
        hedge_ratios[t] = beta[1]
        innovations[t] = e_t
        # Торговый спред: price1 - HR_t * price2 - intercept_t
        trading_spread[t] = s1[t] - beta[1] * s2[t] - beta[0]
        sqrt_Q_series[t] = np.sqrt(max(S_t, 1e-10))

    return {
        'hedge_ratios': hedge_ratios,
        'intercepts': intercepts,
        'spread': trading_spread,       # ← для Z-score и DFA
        'innovations': innovations,     # ← innovations (≈ белый шум)
        'hr_final': float(hedge_ratios[-1]),
        'intercept_final': float(intercepts[-1]),
        'hr_std': float(np.sqrt(P[1, 1])),
        'sqrt_Q': sqrt_Q_series,
        'P_final': P,
    }


def kalman_hr_update(
    coin1_prices,
    coin2_prices,
    current_hr: float,
    current_hr_std: float | None = None,
    n_recent: int = 60,
) -> dict:
    """[A13] Пересчёт hedge ratio на свежих данных внутри открытой позиции.

    Решает проблему HR-drift: Z возвращается к 0, но PnL остаётся
    отрицательным из-за смещения нейтральной точки спреда.

    Пример: HBAR/SEI 30.03 — Z дошёл до -0.03, PnL=-0.49%.
    HR на входе ≠ HR через 10 часов → спред "ноль" сместился.

    Чистая функция, numpy only, без внешних зависимостей.
    Модель: простой Kalman с одним state (hedge_ratio), без intercept.
    Для полного обновления intercept используйте kalman_hedge_ratio().

    Args:
        coin1_prices: array-like — последние цены coin1 (минимум 20, рекомендуется 60)
        coin2_prices: array-like — последние цены coin2
        current_hr:   float — текущий hedge ratio позиции (entry_hr)
        current_hr_std: float | None — текущая неопределённость HR (hr_std из входа)
        n_recent:     int — сколько последних баров использовать (default 60 = 1ч на 1min)

    Returns:
        dict:
            'new_hr':       float  — обновлённый hedge ratio
            'new_hr_std':   float  — обновлённая неопределённость (sqrt posterior variance)
            'hr_drift_pct': float  — % изменения HR от входа (abs)
            'should_warn':  bool   — True если drift > 15% (hr_drift_warn_pct)
            'should_close': bool   — True если drift > 40% (hr_drift_critical_pct)
    """
    result = {
        'new_hr': current_hr,
        'new_hr_std': current_hr_std or 0.0,
        'hr_drift_pct': 0.0,
        'should_warn': False,
        'should_close': False,
    }

    p1 = np.array(coin1_prices[-n_recent:], dtype=float)
    p2 = np.array(coin2_prices[-n_recent:], dtype=float)

    if len(p1) < 20 or len(p2) < 20 or len(p1) != len(p2):
        return result

    # --- Kalman filter: scalar state [hedge_ratio], random-walk model ---
    # Observation: price1[t] = hedge_ratio * price2[t] + noise
    # State transition: hr[t] = hr[t-1] + process_noise

    hr = float(current_hr)
    # Posterior variance: инициализируем из переданного hr_std, или 10% от |hr|
    P = float((current_hr_std or abs(current_hr) * 0.1 or 0.01) ** 2)
    Q_noise = 1e-5   # process noise (HR меняется медленно внутри позиции)
    R_obs   = 1e-4   # observation noise

    for t in range(len(p2)):
        x2 = p2[t]
        if x2 == 0:
            continue

        # Predict step (random walk: hr unchanged, variance grows)
        P = P + Q_noise

        # Update step
        H = x2                        # observation matrix (scalar)
        S = H * P * H + R_obs         # innovation variance
        K = P * H / S                 # Kalman gain
        innovation = p1[t] - hr * H
        hr = hr + K * innovation
        P = (1.0 - K * H) * P

    new_hr_std = float(np.sqrt(max(P, 1e-10)))

    # Drift calculation vs entry HR
    if current_hr != 0:
        drift_pct = abs(hr - current_hr) / abs(current_hr) * 100.0
    else:
        drift_pct = 0.0

    result['new_hr']       = round(float(hr), 6)
    result['new_hr_std']   = round(new_hr_std, 6)
    result['hr_drift_pct'] = round(drift_pct, 2)
    result['should_warn']  = drift_pct > 15.0   # hr_drift_warn_pct из config
    result['should_close'] = drift_pct > 40.0   # hr_drift_critical_pct из config

    return result


def kalman_select_delta(series1, series2, deltas=None):
    """
    Автоподбор delta по максимизации log-likelihood.

    Перебирает несколько значений delta и выбирает лучший.
    Используется если нет уверенности в default delta=1e-4.

    Returns:
        best_delta, best_result, all_likelihoods
    """
    if deltas is None:
        deltas = [1e-5, 5e-5, 1e-4, 5e-4, 1e-3]

    s1 = np.array(series1, dtype=float)
    s2 = np.array(series2, dtype=float)
    n = min(len(s1), len(s2))

    best_ll = -np.inf
    best_delta = 1e-4
    best_result = None
    all_ll = {}

    for d in deltas:
        res = kalman_hedge_ratio(s1, s2, delta=d)
        if res is None:
            continue

        # Log-likelihood: sum of log N(e_t; 0, S_t)
        sq = res['sqrt_Q']
        innov = res['innovations']  # innovations, not trading spread

        # Ignore first 30 bars (warmup)
        warmup = min(30, n // 3)
        ll_valid = -0.5 * np.sum(
            np.log(2 * np.pi * sq[warmup:]**2 + 1e-10) +
            innov[warmup:]**2 / (sq[warmup:]**2 + 1e-10)
        )

        all_ll[d] = float(ll_valid)
        if ll_valid > best_ll:
            best_ll = ll_valid
            best_delta = d
            best_result = res

    return best_delta, best_result, all_ll




# =============================================================================
# ADF-ТЕСТ СПРЕДА
# =============================================================================

def adf_test_spread(spread, significance=0.05):
    """ADF тест на стационарность спреда."""
    from statsmodels.tsa.stattools import adfuller
    try:
        spread = np.array(spread, dtype=float)
        if len(spread) < 20:
            return {'adf_stat': 0, 'adf_pvalue': 1.0, 'is_stationary': False, 'critical_values': {}}
        result = adfuller(spread, autolag='AIC')
        return {
            'adf_stat': float(result[0]), 'adf_pvalue': float(result[1]),
            'is_stationary': result[1] < significance,
            'critical_values': {k: float(v) for k, v in result[4].items()}
        }
    except Exception:
        return {'adf_stat': 0, 'adf_pvalue': 1.0, 'is_stationary': False, 'critical_values': {}}


# =============================================================================
# FDR-КОРРЕКЦИЯ
# =============================================================================

def apply_fdr_correction(pvalues, alpha=0.05):
    """Benjamini-Hochberg FDR. Передавайте ВСЕ p-values!"""
    pvalues = np.array(pvalues, dtype=float)
    n = len(pvalues)
    if n == 0:
        return np.array([]), np.array([], dtype=bool)

    sorted_idx = np.argsort(pvalues)
    sorted_p = pvalues[sorted_idx]

    adjusted = np.empty(n)
    for i in range(n):
        adjusted[i] = sorted_p[i] * n / (i + 1)
    for i in range(n - 2, -1, -1):
        adjusted[i] = min(adjusted[i], adjusted[i + 1])
    adjusted = np.minimum(adjusted, 1.0)

    result = np.empty(n)
    result[sorted_idx] = adjusted
    return result, result <= alpha


# =============================================================================
# COINTEGRATION STABILITY
# =============================================================================

def johansen_test(series1, series2, det_order=0, k_ar_diff=1):
    """
    v13.0: Johansen cointegration test (symmetric, multi-equation).
    
    Unlike Engle-Granger (asymmetric OLS), Johansen tests a VECM system
    and doesn't require choosing dependent/independent variable.
    
    Args:
        series1, series2: price arrays
        det_order: -1=no const, 0=const (default), 1=const+trend
        k_ar_diff: number of lagged differences (default 1)
    
    Returns:
        dict: {
            'trace_stat': float (test statistic),
            'trace_cv_5pct': float (5% critical value),
            'is_cointegrated': bool,
            'eigen_stat': float,
            'eigen_cv_5pct': float,
            'hedge_ratio': float (from eigenvector),
            'method': 'johansen'
        }
        or None on failure
    """
    try:
        from statsmodels.tsa.vector_ar.vecm import coint_johansen
    except ImportError:
        return None
    
    s1, s2 = np.array(series1, float), np.array(series2, float)
    n = min(len(s1), len(s2))
    if n < 50:
        return None
    s1, s2 = s1[:n], s2[:n]
    
    try:
        data = np.column_stack([s1, s2])
        result = coint_johansen(data, det_order=det_order, k_ar_diff=k_ar_diff)
        
        # r=0: test for "no cointegration" (reject = cointegrated)
        trace_stat = float(result.lr1[0])   # trace statistic for r=0
        trace_cv = float(result.cvt[0, 1])  # 5% critical value
        eigen_stat = float(result.lr2[0])   # max-eigenvalue for r=0
        eigen_cv = float(result.cvm[0, 1])  # 5% critical value
        
        is_coint = trace_stat > trace_cv  # reject H0: no cointegration
        
        # Hedge ratio from first eigenvector
        evec = result.evec[:, 0]
        hr = float(-evec[1] / evec[0]) if abs(evec[0]) > 1e-10 else 1.0
        
        return {
            'trace_stat': round(trace_stat, 3),
            'trace_cv_5pct': round(trace_cv, 3),
            'is_cointegrated': is_coint,
            'eigen_stat': round(eigen_stat, 3),
            'eigen_cv_5pct': round(eigen_cv, 3),
            'hedge_ratio': round(hr, 6),
            'method': 'johansen',
        }
    except Exception:
        return None


def check_cointegration_stability(series1, series2, window_fraction=0.6):
    """4 подокна: полное, начало, конец, середина."""
    from statsmodels.tsa.stattools import coint
    s1, s2 = np.array(series1, dtype=float), np.array(series2, dtype=float)
    n = min(len(s1), len(s2))
    if n < 30:
        return {'is_stable': False, 'windows_passed': 0, 'total_windows': 0,
                'stability_score': 0.0, 'pvalues': []}
    ws = max(20, int(n * window_fraction))
    mid = (n - ws) // 2
    windows = [(0, n), (0, ws), (n - ws, n), (mid, mid + ws)]
    pvalues, passed = [], 0
    for start, end in windows:
        end = min(end, n)
        if end - start < 20:
            continue
        try:
            _, pval, _ = coint(s1[start:end], s2[start:end])
            pvalues.append(float(pval))
            if pval < 0.05:
                passed += 1
        except Exception:
            pvalues.append(1.0)
    total = len(pvalues)
    return {
        'is_stable': passed >= 3, 'windows_passed': passed,
        'total_windows': total,
        'stability_score': round(passed / total if total > 0 else 0.0, 3),
        'pvalues': pvalues
    }


# =============================================================================
# CONFIDENCE
# =============================================================================



def detect_spread_regime(spread, window=20):
    """
    v11.2: Spread-based regime detection (ADX analog for pairs trading).
    
    Вычисляет:
      1. Spread ADX: directional movement of spread (0-100)
      2. Variance Ratio: short/long variance (>1.5 = trending)
      3. Trend persistence: % баров в одном направлении
    
    Returns:
      dict: adx, variance_ratio, trend_pct, regime ('MEAN_REVERT', 'NEUTRAL', 'TRENDING')
    """
    import pandas as _pd
    
    if len(spread) < window * 3:
        return {'adx': 0, 'variance_ratio': 1.0, 'trend_pct': 0.5, 'regime': 'UNKNOWN'}
    
    spread = np.array(spread)
    n = len(spread)
    
    # 1. Spread-based ADX
    diff = np.diff(spread)
    pos_dm = np.maximum(diff, 0)
    neg_dm = np.maximum(-diff, 0)
    
    pos_smooth = _pd.Series(pos_dm).rolling(window, min_periods=1).mean().values
    neg_smooth = _pd.Series(neg_dm).rolling(window, min_periods=1).mean().values
    atr = _pd.Series(np.abs(diff)).rolling(window, min_periods=1).mean().values
    
    di_plus = pos_smooth / (atr + 1e-10) * 100
    di_minus = neg_smooth / (atr + 1e-10) * 100
    dx = np.abs(di_plus - di_minus) / (di_plus + di_minus + 1e-10) * 100
    adx = float(_pd.Series(dx).rolling(window, min_periods=1).mean().iloc[-1])
    
    # 2. Variance Ratio (short vs long window)
    short_w = min(window, n // 4)
    long_w = min(window * 3, n - 1)
    
    short_returns = np.diff(spread[-short_w:])
    long_returns = np.diff(spread[-long_w:])
    
    var_short = np.var(short_returns) if len(short_returns) > 2 else 0
    var_long = np.var(long_returns) if len(long_returns) > 2 else 1e-10
    variance_ratio = var_short / (var_long + 1e-10)
    
    # 3. Trend persistence
    recent = spread[-window:]
    diffs = np.diff(recent)
    pos_count = np.sum(diffs > 0)
    trend_pct = max(pos_count, len(diffs) - pos_count) / len(diffs) if len(diffs) > 0 else 0.5
    
    # Classify
    if adx > 30 and variance_ratio > 1.5:
        regime = 'TRENDING'
    elif adx > 25 or variance_ratio > 2.0 or trend_pct > 0.75:
        regime = 'TRENDING'
    elif adx < 15 and variance_ratio < 1.2:
        regime = 'MEAN_REVERT'
    else:
        regime = 'NEUTRAL'
    
    return {
        'adx': round(adx, 1),
        'variance_ratio': round(variance_ratio, 2),
        'trend_pct': round(trend_pct, 2),
        'regime': regime,
    }


def check_hr_magnitude(hedge_ratio, threshold=5.0):
    """v11.2: HR magnitude warning if |HR| > threshold."""
    abs_hr = abs(hedge_ratio)
    if abs_hr > threshold:
        return (f"⚠️ HR={hedge_ratio:.2f} — капитальный дисбаланс! "
                f"На $1 первой монеты нужно ${abs_hr:.1f} второй.")
    return None


def check_minimum_bars(n_bars, timeframe='1d', min_bars_map=None):
    """v11.2: Minimum bars gate (1d needs ≥200 for reliable DFA)."""
    if min_bars_map is None:
        min_bars_map = {'1d': 200, '4h': 100, '1h': 150, '2h': 120, '15m': 200}
    min_required = min_bars_map.get(timeframe, 100)
    if n_bars < min_required:
        return (f"⚠️ {n_bars} баров < {min_required} мин для {timeframe}. "
                f"DFA/Kalman ненадёжны.")
    return None


def cusum_structural_break(spread, threshold_sigma=3.0, min_tail=30, zscore=None):
    """
    v13.0: CUSUM test with Z-magnitude amplifier.
    
    If |Z| > 5 AND CUSUM > 2.0 → break (catches FIL/CRV Z=9.36)
    
    Returns:
        dict with: has_break, cusum_score, tail_drift, tail_trend,
                   risk_level ('LOW'/'MEDIUM'/'HIGH'/'CRITICAL'),
                   position_advice (str), warning
    """
    spread = np.array(spread, dtype=float)
    n = len(spread)
    
    if n < min_tail * 2:
        return {'has_break': False, 'break_index': None, 
                'cusum_score': 0.0, 'tail_drift': 0.0, 'tail_trend': 0.0,
                'risk_level': 'LOW', 'position_advice': '', 'warning': None}
    
    ref_n = int(n * 0.70)
    ref_mean = np.mean(spread[:ref_n])
    ref_std = np.std(spread[:ref_n])
    if ref_std < 1e-10:
        return {'has_break': False, 'break_index': None,
                'cusum_score': 0.0, 'tail_drift': 0.0, 'tail_trend': 0.0,
                'risk_level': 'LOW', 'position_advice': '', 'warning': None}
    
    residuals = (spread - ref_mean) / ref_std
    cusum = np.cumsum(residuals - np.mean(residuals[:ref_n]))
    cusum_norm = cusum / np.sqrt(np.arange(1, n + 1))
    max_cusum = float(np.max(np.abs(cusum_norm)))
    
    cusum_diff = np.abs(np.diff(cusum_norm))
    if len(cusum_diff) > min_tail:
        recent_diff = cusum_diff[-min_tail * 2:]
        break_offset = int(np.argmax(recent_diff))
        break_index = n - min_tail * 2 + break_offset
    else:
        break_index = None
    
    tail = residuals[-min_tail:]
    tail_drift = float(np.mean(tail))
    tail_trend = float(np.polyfit(np.arange(len(tail)), tail, 1)[0])
    
    # v13.0: Z-magnitude amplifier
    abs_z = abs(zscore) if zscore is not None else 0
    
    has_break = (
        (max_cusum > threshold_sigma and abs(tail_drift) > 1.5) or
        (abs(tail_drift) > 2.5) or
        (abs(tail_trend) > 0.08 and abs(tail_drift) > 1.0) or
        # NEW: extreme Z + elevated CUSUM = structural break
        (abs_z > 5.0 and max_cusum > 2.0) or
        (abs_z > 7.0 and max_cusum > 1.5)
    )
    
    # v13.0: Risk classification for position sizing
    if has_break or abs_z > 6.0:
        risk_level = 'CRITICAL'
    elif max_cusum > 2.5 or (abs_z > 4.0 and max_cusum > 2.0):
        risk_level = 'HIGH'
    elif max_cusum > 2.0 or abs(tail_drift) > 1.0:
        risk_level = 'MEDIUM'
    else:
        risk_level = 'LOW'
    
    # v13.0: Position sizing advice
    advice_map = {
        'CRITICAL': '🚫 НЕ ВХОДИТЬ. Коинтеграция разрушена. Спред в тренде.',
        'HIGH': '⚠️ Макс 25% позиции. Зарезервируйте 75% на усреднение или стоп. Высокий риск продолжения тренда.',
        'MEDIUM': '💡 Макс 50% позиции. Зарезервируйте 50% на добавление при откате Z к среднему.',
        'LOW': '✅ Полная позиция допустима. Стандартный risk management.',
    }
    position_advice = advice_map[risk_level]
    
    warning = None
    if risk_level == 'CRITICAL':
        warning = (f"🚨 CRITICAL: CUSUM={max_cusum:.1f}σ, Z={abs_z:.1f}, "
                   f"drift={tail_drift:+.2f}σ — НЕ ВХОДИТЬ!")
    elif risk_level == 'HIGH':
        warning = (f"🔴 HIGH RISK: CUSUM={max_cusum:.1f}σ, "
                   f"drift={tail_drift:+.2f}σ — макс 25% позиции")
    elif risk_level == 'MEDIUM':
        warning = (f"⚠️ Возможный сдвиг: CUSUM={max_cusum:.1f}σ, "
                   f"drift={tail_drift:+.2f}σ — макс 50% позиции")
    
    return {
        'has_break': has_break,
        'break_index': break_index,
        'cusum_score': round(max_cusum, 2),
        'tail_drift': round(tail_drift, 2),
        'tail_trend': round(tail_trend, 4),
        'risk_level': risk_level,
        'position_advice': position_advice,
        'warning': warning,
    }


# =============================================================================
# v14.0: COST-AWARE THRESHOLD (рассуждение #3)
# =============================================================================

def cost_aware_min_z(spread_std, commission_pct=0.10, slippage_pct=0.05, 
                     min_profit_ratio=3.0):
    """
    Minimum Z for profitable entry. 
    Z_min = total_costs / spread_pnl_per_z * min_profit_ratio
    
    If expected profit at Z=threshold doesn't exceed costs by min_profit_ratio,
    the trade is not worth taking.
    
    Returns: float (minimum Z threshold)
    """
    total_costs_pct = (commission_pct + slippage_pct) * 2  # 2 legs × (comm + slip)
    # Rough estimate: 1 Z of spread movement ≈ spread_std in price terms
    # PnL per Z ≈ spread_std / price ≈ proportional to Z movement
    # We need Z × pnl_per_z > costs × min_profit_ratio
    # Simplified: min_Z ≈ costs * ratio / typical_pnl_per_z
    # For crypto altcoins, typical pnl_per_z ≈ 0.3-0.5% per Z unit
    pnl_per_z = max(0.15, min(0.8, spread_std * 100)) if spread_std > 0 else 0.3
    min_z = total_costs_pct * min_profit_ratio / pnl_per_z
    return max(1.5, round(min_z, 2))


# =============================================================================
# v14.0: DOLLAR EXPOSURE CHECK (рассуждение #4)
# =============================================================================

def check_dollar_exposure(price1, price2, hedge_ratio, capital=1000):
    """
    Check dollar neutrality of the pair position.
    
    Beta-neutral (HR-adjusted) != Dollar-neutral.
    If HR=3, you sell $1000 coin1 and buy $3000 coin2 → $2000 net exposure!
    
    Returns:
        dict: {
            'leg1_dollars': float,
            'leg2_dollars': float,
            'net_exposure': float (absolute),
            'exposure_pct': float (% of capital),
            'is_balanced': bool,
            'warning': str or None
        }
    """
    leg1 = capital
    leg2 = capital * abs(hedge_ratio) * (price2 / price1) if price1 > 0 else capital
    net = abs(leg1 - leg2)
    exposure_pct = net / max(leg1, leg2) * 100 if max(leg1, leg2) > 0 else 0
    
    is_balanced = exposure_pct < 50
    warning = None
    if exposure_pct > 100:
        warning = f"🚨 Dollar exposure {exposure_pct:.0f}%: leg1=${leg1:.0f} vs leg2=${leg2:.0f}"
    elif exposure_pct > 50:
        warning = f"⚠️ Dollar exposure {exposure_pct:.0f}%: позиция не доллар-нейтральна"
    
    return {
        'leg1_dollars': round(leg1, 2),
        'leg2_dollars': round(leg2, 2),
        'net_exposure': round(net, 2),
        'exposure_pct': round(exposure_pct, 1),
        'is_balanced': is_balanced,
        'warning': warning,
    }


# =============================================================================
# v14.0: PnL/Z DISAGREEMENT (рассуждение #1)
# =============================================================================



def calc_halflife_from_spread(spread, dt=1/6):
    """Единый расчёт half-life из OU-регрессии. dt=1/6 для 4h."""
    spread = np.array(spread, dtype=float)
    if len(spread) < 10:
        return 999
    dS = np.diff(spread)
    S_lag = spread[:-1]
    S_lag_c = S_lag - np.mean(S_lag)
    if np.std(S_lag_c) < 1e-12:
        return 999
    theta = -float(np.polyfit(S_lag_c, dS, 1)[0])
    if theta <= 0:
        return 999
    hl = np.log(2) / theta * dt
    return max(0.01, min(hl, 999))


# =============================================================================
# P5: PCA FACTOR CLUSTERING
# =============================================================================

def pca_factor_clustering(returns_dict, n_components=3):
    """
    v21: PCA Factor Clustering — identifies hidden market factors.
    
    Problem: All crypto altcoins correlate with BTC/ETH.
    When BTC drops 5%, all pairs with BTC-exposure break.
    
    Solution:
    1. Build returns matrix from coin price series
    2. PCA → 3 components (Market/BTC-beta, ALT-premium, Sector-factor)
    3. Calculate factor_exposure for each coin
    4. For pairs: compute net factor exposure (should be near zero for good pairs)
    
    Args:
        returns_dict: {coin_name: np.array of log returns} for all coins
        n_components: number of PCA components (default 3)
    
    Returns:
        dict with loadings, explained variance, coin clusters, factor names
    """
    from sklearn.decomposition import PCA
    from sklearn.cluster import KMeans
    
    coins = sorted(returns_dict.keys())
    if len(coins) < 5:
        return {'error': 'Need at least 5 coins', 'coins': coins}
    
    # Build returns matrix: align lengths
    min_len = min(len(returns_dict[c]) for c in coins)
    if min_len < 30:
        return {'error': f'Insufficient data: {min_len} bars', 'coins': coins}
    
    R = np.column_stack([returns_dict[c][-min_len:] for c in coins])
    
    # Remove coins with zero variance
    valid_mask = np.std(R, axis=0) > 1e-10
    valid_coins = [c for c, v in zip(coins, valid_mask) if v]
    R = R[:, valid_mask]
    
    if len(valid_coins) < 5:
        return {'error': 'Too few valid coins after filtering', 'coins': valid_coins}
    
    # Standardize
    R_std = (R - R.mean(axis=0)) / (R.std(axis=0) + 1e-10)
    
    # PCA
    n_comp = min(n_components, len(valid_coins) - 1)
    pca = PCA(n_components=n_comp)
    scores = pca.fit_transform(R_std)  # (T x n_comp)
    loadings = pca.components_          # (n_comp x N_coins)
    explained = pca.explained_variance_ratio_
    
    # Name factors heuristically
    factor_names = []
    for i in range(n_comp):
        abs_load = np.abs(loadings[i])
        mean_abs = abs_load.mean()
        spread_load = abs_load.max() - abs_load.min()
        
        if i == 0:
            factor_names.append("Market (BTC-beta)")
        elif spread_load > 2 * mean_abs:
            # High dispersion: sector factor
            top_coins = [valid_coins[j] for j in np.argsort(-abs_load)[:3]]
            factor_names.append(f"Sector ({'/'.join(top_coins[:2])})")
        else:
            factor_names.append(f"Factor_{i+1}")
    
    # Coin loadings dict
    coin_loadings = {}
    for idx, coin in enumerate(valid_coins):
        coin_loadings[coin] = {
            f'PC{i+1}': round(float(loadings[i, idx]), 4)
            for i in range(n_comp)
        }
    
    # K-Means clustering on loadings
    n_clusters = min(4, len(valid_coins) // 3)
    n_clusters = max(2, n_clusters)
    
    loading_matrix = loadings.T  # (N_coins x n_comp)
    kmeans = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    cluster_labels = kmeans.fit_predict(loading_matrix)
    
    coin_clusters = {}
    for idx, coin in enumerate(valid_coins):
        coin_clusters[coin] = int(cluster_labels[idx])
    
    # Cluster summary
    cluster_summary = {}
    for cl in range(n_clusters):
        members = [c for c, l in coin_clusters.items() if l == cl]
        if members:
            avg_loadings = {
                f'PC{i+1}': round(float(np.mean([
                    coin_loadings[c][f'PC{i+1}'] for c in members
                ])), 4) for i in range(n_comp)
            }
            cluster_summary[cl] = {
                'members': members,
                'n': len(members),
                'avg_loadings': avg_loadings,
            }
    
    return {
        'coins': valid_coins,
        'n_components': n_comp,
        'explained_variance': [round(float(e), 4) for e in explained],
        'total_explained': round(float(explained.sum()), 4),
        'factor_names': factor_names,
        'coin_loadings': coin_loadings,
        'coin_clusters': coin_clusters,
        'cluster_summary': cluster_summary,
        'loadings_raw': loadings,
        'scores': scores,
    }


def pair_factor_exposure(pca_result, coin1, coin2, hedge_ratio=1.0):
    """
    Calculate net factor exposure for a pair trade.
    
    LONG coin1 / SHORT coin2 with hedge_ratio:
      net_exposure_PCi = loading_coin1_PCi - HR * loading_coin2_PCi
    
    Good pair: net exposure ≈ 0 for PC1 (market-neutral)
    Bad pair: large |net_PC1| means correlated with market
    """
    loadings = pca_result.get('coin_loadings', {})
    
    if coin1 not in loadings or coin2 not in loadings:
        return None
    
    l1 = loadings[coin1]
    l2 = loadings[coin2]
    n_comp = pca_result.get('n_components', 3)
    
    net = {}
    total_exposure = 0
    for i in range(n_comp):
        key = f'PC{i+1}'
        net_val = l1.get(key, 0) - hedge_ratio * l2.get(key, 0)
        net[key] = round(net_val, 4)
        total_exposure += net_val ** 2
    
    # Same cluster?
    clusters = pca_result.get('coin_clusters', {})
    same_cluster = clusters.get(coin1) == clusters.get(coin2)
    
    # Market neutrality score (0=neutral, 1=fully exposed)
    market_exposure = abs(net.get('PC1', 0))
    neutrality = 1.0 - min(1.0, market_exposure)
    
    return {
        'net_exposure': net,
        'total_exposure': round(float(np.sqrt(total_exposure)), 4),
        'market_neutrality': round(neutrality, 4),
        'same_cluster': same_cluster,
        'cluster_coin1': clusters.get(coin1, -1),
        'cluster_coin2': clusters.get(coin2, -1),
        'factor_names': pca_result.get('factor_names', []),
    }


def check_pnl_z_disagreement(entry_z, current_z, pnl_pct, direction):
    """
    Detect false Z-convergence caused by variance expansion (not price convergence).
    
    If Z fell from 3.0 to 0.0 but PnL ≈ 0%, the spread didn't actually converge —
    the standard deviation expanded, making Z shrink artificially.
    
    Returns:
        dict: {
            'z_moved': float (how much Z moved toward zero),
            'pnl_expected_pct': float (expected PnL for this Z move),
            'disagreement': bool,
            'severity': str ('NONE'|'MILD'|'SEVERE'),
            'warning': str or None
        }
    """
    z_delta = abs(entry_z) - abs(current_z)  # positive = Z moved toward zero
    
    # Expected rough PnL for Z movement (typically 0.2-0.5% per Z unit)
    pnl_per_z = 0.3  # conservative estimate
    pnl_expected = z_delta * pnl_per_z
    
    # Disagreement: Z says "converged" but PnL says "no profit"
    has_disagreement = (z_delta > 1.0 and pnl_pct < pnl_expected * 0.3)
    
    if has_disagreement and z_delta > 2.0 and pnl_pct < 0.1:
        severity = 'SEVERE'
        warning = (f"🚨 Ложное схождение! Z сместился на {z_delta:+.1f}, "
                   f"но P&L={pnl_pct:+.2f}%. Причина: рост σ спреда, а не возврат цен.")
    elif has_disagreement:
        severity = 'MILD'
        warning = (f"⚠️ Z/PnL расхождение: Z Δ={z_delta:+.1f}, P&L={pnl_pct:+.2f}% "
                   f"(ожидалось ~{pnl_expected:+.1f}%)")
    else:
        severity = 'NONE'
        warning = None
    
    return {
        'z_moved': round(z_delta, 2),
        'pnl_expected_pct': round(pnl_expected, 2),
        'disagreement': has_disagreement,
        'severity': severity,
        'warning': warning,
    }


# =============================================================================
# v17.0: MINI-BACKTEST for Scanner Integration (P1 Roadmap)
# =============================================================================


