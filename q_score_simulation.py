"""
q_score_simulation.py — Симуляция новой формулы Q-score (ANALYSIS-v46)

Читает quality_breakdown_log.jsonl и trade_history.csv,
пересчитывает Q по старой и новой формуле, сравнивает.

Запуск: python q_score_simulation.py
  или:  python q_score_simulation.py --log scan_exports/quality_breakdown_log.jsonl

Вывод:
  1. Таблица: pair | Q_old | Q_new | delta | signal | PnL (если торговалась)
  2. Статистика: WR по квантилям Q, корреляция Q-PnL
  3. Проверка 3 критериев принятия

ANALYSIS-v46 ИЗМЕНЕНИЯ (поверх v45 hybrid):
  Stability: 25 → 30 (ключевой предиктор SL — снижение в v45 было ошибкой)
  ADF:       15 → 10 (ADF не предсказывает скорость возврата Z, инфлировал Q)
  Итог:      суммарный вес 25+10=35 → 30+10=40 (Stability+ADF), без изменения итога 100
"""

import json
import csv
import os
import sys
from collections import defaultdict


# ═══════════════════════════════════════════════════════
# СТАРАЯ ФОРМУЛА (для сравнения)
# ═══════════════════════════════════════════════════════

def q_score_old(bd_raw):
    """Пересчитать Q по СТАРОЙ формуле из компонентов breakdown."""
    # Старая формула просто суммировала компоненты как есть
    return max(0, min(100, sum(bd_raw.values())))


# ═══════════════════════════════════════════════════════
# НОВАЯ ФОРМУЛА (v46 hybrid)
# ANALYSIS-v46: обновлено v45 → v46 hybrid
# Ключевые изменения: Stability 25→30, ADF 15→10
# ═══════════════════════════════════════════════════════

def q_score_new_from_breakdown(bd_raw, pvalue_adj=None, hedge_ratio=None,
                                hr_std=None, hurst_is_fallback=False,
                                stability_score=None, hurst=None, adf_passed=None):
    """Пересчитать Q по v46 HYBRID формуле (ANALYSIS-v46).
    
    ANALYSIS-v46 CHANGELOG (поверх v45):
      Stability: 25 → 30 (ключевой предиктор SL, снижение было ошибкой)
      ADF:       15 → 10 (не предсказывает скорость возврата, инфлировал Q)
    
    Если переданы сырые параметры (pvalue_adj, etc.) — считает с нуля.
    Если только bd_raw — реконструирует из старого breakdown.
    """
    bd = {}
    
    # FDR (25) — непрерывная шкала + cap (v45: было 20 в v44)
    if pvalue_adj is not None:
        bd['fdr'] = min(25, int(max(0.0, (0.15 - pvalue_adj) / 0.15) * 25))
    else:
        old_fdr = bd_raw.get('fdr', 0)
        if old_fdr >= 25:
            est_p = 0.005
        elif old_fdr >= 20:
            est_p = 0.02
        elif old_fdr >= 12:
            est_p = 0.04
        else:
            est_p = 0.20
        bd['fdr'] = min(25, int(max(0.0, (0.15 - est_p) / 0.15) * 25))
    
    # Stability (30) — ANALYSIS-v46: возвращён с 25 → 30 (было снижено в v45 необоснованно)
    old_stab = bd_raw.get('stability', 0)
    if stability_score is not None:
        bd['stability'] = min(30, max(0, int(stability_score * 30)))
    else:
        est_stab_ratio = old_stab / 25.0 if old_stab > 0 else 0
        bd['stability'] = min(30, max(0, int(est_stab_ratio * 30)))
    
    # Hurst (20), fallback=5 (v45: was fallback=10 in v44)
    if hurst is not None:
        if hurst_is_fallback:
            bd['hurst'] = 5   # v45: было 10 в v44
        elif hurst <= 0.30:
            bd['hurst'] = 20
        elif hurst <= 0.40:
            bd['hurst'] = 15
        elif hurst <= 0.48:
            bd['hurst'] = 10
        elif hurst < 0.50:
            bd['hurst'] = 4
        else:
            bd['hurst'] = 0
    else:
        old_h = bd_raw.get('hurst', 0)
        if old_h == 5:  # was fallback in old formula
            bd['hurst'] = 5
        else:
            bd['hurst'] = old_h
    
    # ADF (10) — ANALYSIS-v46: снижен с 15 → 10 (не предсказывает скорость возврата)
    if adf_passed is not None:
        bd['adf'] = 10 if adf_passed else 0
    else:
        old_adf = bd_raw.get('adf', 0)
        bd['adf'] = 10 if old_adf > 0 else 0
    
    # Hedge ratio (15/10/5) — v45: зоны v44 (≤3.5) + веса старой формулы (15/10/5)
    if hedge_ratio is not None:
        abs_hr = abs(hedge_ratio)
        if abs_hr == 0 or abs_hr > 30:
            bd['hedge_ratio'] = 0
        elif 0.2 <= abs_hr <= 3.5:
            bd['hedge_ratio'] = 15
        elif 0.1 <= abs_hr <= 7.0:
            bd['hedge_ratio'] = 10
        elif 0.05 <= abs_hr <= 15.0:
            bd['hedge_ratio'] = 5
        else:
            bd['hedge_ratio'] = 0
    else:
        old_hr = bd_raw.get('hedge_ratio', 0)
        if old_hr >= 15:
            bd['hedge_ratio'] = 15
        elif old_hr >= 10:
            bd['hedge_ratio'] = 10
        elif old_hr >= 5:
            bd['hedge_ratio'] = 5
        else:
            bd['hedge_ratio'] = 0
    
    # Модификаторы — берём из старого breakdown
    bd['crossing_penalty'] = bd_raw.get('crossing_penalty', 0)
    bd['data_penalty'] = bd_raw.get('data_penalty', 0)
    
    # HR uncertainty — градуированный штраф (v44/v45 совпадают)
    if hr_std is not None and hedge_ratio and abs(hedge_ratio) > 0:
        _hr_unc = hr_std / abs(hedge_ratio)
        if _hr_unc > 0.7:
            bd['hr_unc_penalty'] = -25
        elif _hr_unc > 0.5:
            bd['hr_unc_penalty'] = -15
        elif _hr_unc > 0.3:
            bd['hr_unc_penalty'] = -8
        else:
            bd['hr_unc_penalty'] = 0
    else:
        bd['hr_unc_penalty'] = bd_raw.get('hr_unc_penalty', 0)
    
    bd['ubt_penalty'] = bd_raw.get('ubt_penalty', 0)
    
    total = max(0, min(100, sum(bd.values())))
    return int(total), bd


# ═══════════════════════════════════════════════════════
# ЗАГРУЗКА ДАННЫХ
# ═══════════════════════════════════════════════════════

def load_breakdown_log(path="scan_exports/quality_breakdown_log.jsonl"):
    """Загрузить лог quality breakdown."""
    entries = []
    if not os.path.exists(path):
        print(f"⚠️  Файл {path} не найден. Запустите 2-3 скана для накопления данных.")
        return entries
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def load_trade_history(path="trade_history.csv"):
    """Загрузить историю сделок."""
    trades = {}
    if not os.path.exists(path):
        # Попробовать positions.json
        pos_path = "positions.json"
        if os.path.exists(pos_path):
            try:
                with open(pos_path, 'r', encoding='utf-8') as f:
                    positions = json.load(f)
                for p in positions:
                    if p.get('status') != 'CLOSED':
                        continue
                    reason = p.get('exit_reason', '')
                    # Фильтруем только автоматические сделки
                    if reason in ('MANUAL',):
                        continue
                    pair = f"{p.get('coin1', '')}/{p.get('coin2', '')}"
                    pnl = float(p.get('pnl_pct', 0) or 0)
                    if pair not in trades:
                        trades[pair] = []
                    trades[pair].append(pnl)
            except Exception:
                pass
        return trades
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                pair = f"{row.get('coin1', '')}/{row.get('coin2', '')}"
                pnl = float(row.get('pnl_pct', 0) or 0)
                reason = row.get('exit_reason', '')
                if reason in ('MANUAL',):
                    continue
                if pair not in trades:
                    trades[pair] = []
                trades[pair].append(pnl)
    except Exception:
        pass
    return trades


# ═══════════════════════════════════════════════════════
# СИМУЛЯЦИЯ
# ═══════════════════════════════════════════════════════

def run_simulation(log_path="scan_exports/quality_breakdown_log.jsonl"):
    entries = load_breakdown_log(log_path)
    if not entries:
        print("Нет данных для симуляции. Запустите сканер для накопления quality_breakdown_log.jsonl")
        return
    
    trades = load_trade_history()
    
    # Дедупликация по паре (берём последний скан)
    latest = {}
    for e in entries:
        pair = e.get('pair', '')
        if pair:
            latest[pair] = e
    
    print(f"\n{'='*80}")
    print(f"Q-SCORE SIMULATION: {len(latest)} уникальных пар из {len(entries)} записей")
    print(f"{'='*80}\n")
    
    # Таблица сравнения
    results = []
    for pair, e in sorted(latest.items()):
        bd_raw = e.get('quality_bd', {})
        q_old = e.get('quality', q_score_old(bd_raw))
        
        q_new, bd_new = q_score_new_from_breakdown(
            bd_raw,
            pvalue_adj=e.get('pvalue_adj'),
            hedge_ratio=e.get('hedge_ratio'),
            hurst=e.get('hurst'),
        )
        
        delta = q_new - q_old
        pair_trades = trades.get(pair, [])
        avg_pnl = sum(pair_trades) / len(pair_trades) if pair_trades else None
        
        results.append({
            'pair': pair,
            'q_old': q_old,
            'q_new': q_new,
            'delta': delta,
            'signal': e.get('signal', ''),
            'entry': e.get('entry_label', ''),
            'n_trades': len(pair_trades),
            'avg_pnl': avg_pnl,
        })
    
    # Печать таблицы
    print(f"{'Пара':<16} {'Q_old':>5} {'Q_new':>5} {'Δ':>4} {'Signal':<8} {'Trades':>6} {'Avg PnL':>8}")
    print("-" * 65)
    
    status_changes = []
    for r in results:
        pnl_str = f"{r['avg_pnl']:+.2f}%" if r['avg_pnl'] is not None else "  —"
        delta_str = f"{r['delta']:+d}"
        print(f"{r['pair']:<16} {r['q_old']:>5} {r['q_new']:>5} {delta_str:>4} {r['signal']:<8} {r['n_trades']:>6} {pnl_str:>8}")
        
        # Проверка смены статуса
        threshold = 65
        was_above = r['q_old'] >= threshold
        now_above = r['q_new'] >= threshold
        if was_above != now_above:
            status_changes.append(r)
    
    # Статистика
    print(f"\n{'='*80}")
    print("СТАТИСТИКА")
    print(f"{'='*80}\n")
    
    deltas = [r['delta'] for r in results]
    print(f"Средний Δ(Q): {sum(deltas)/len(deltas):+.1f}")
    print(f"Макс рост:    {max(deltas):+d}")
    print(f"Макс падение: {min(deltas):+d}")
    
    # WR по квантилям нового Q
    traded = [r for r in results if r['n_trades'] > 0]
    if traded:
        print(f"\nWin Rate по квантилям Q_new ({len(traded)} торгованных пар):")
        for lo, hi in [(0, 50), (50, 65), (65, 80), (80, 101)]:
            bucket = [r for r in traded if lo <= r['q_new'] < hi]
            if bucket:
                wins = sum(1 for r in bucket if r['avg_pnl'] and r['avg_pnl'] > 0)
                avg = sum(r['avg_pnl'] for r in bucket if r['avg_pnl']) / len(bucket)
                print(f"  Q {lo:>3}-{hi-1:<3}: {len(bucket)} пар, WR={wins/len(bucket)*100:.0f}%, avg={avg:+.2f}%")
    
    # Смена статуса
    if status_changes:
        print(f"\n⚠️  СМЕНА СТАТУСА (порог {threshold}):")
        for r in status_changes:
            direction = "↗ выше порога" if r['q_new'] >= threshold else "↘ ниже порога"
            pnl_str = f"PnL={r['avg_pnl']:+.2f}%" if r['avg_pnl'] is not None else "не торговалась"
            print(f"  {r['pair']}: {r['q_old']}→{r['q_new']} ({direction}) [{pnl_str}]")
    
    # Проверка 3 критериев
    print(f"\n{'='*80}")
    print("ПРОВЕРКА КРИТЕРИЕВ ПРИНЯТИЯ")
    print(f"{'='*80}\n")
    
    # Критерий 1: корреляция Q с PnL
    if traded:
        q_new_vals = [r['q_new'] for r in traded]
        pnl_vals = [r['avg_pnl'] for r in traded]
        high_q = [r for r in traded if r['q_new'] >= 65]
        mid_q = [r for r in traded if 55 <= r['q_new'] < 65]
        if high_q and mid_q:
            wr_high = sum(1 for r in high_q if r['avg_pnl'] and r['avg_pnl'] > 0) / len(high_q)
            wr_mid = sum(1 for r in mid_q if r['avg_pnl'] and r['avg_pnl'] > 0) / len(mid_q)
            c1_pass = wr_high > wr_mid
            print(f"1. WR(Q≥65)={wr_high*100:.0f}% vs WR(Q 55-64)={wr_mid*100:.0f}% → {'✅ PASS' if c1_pass else '❌ FAIL'}")
        else:
            print(f"1. Недостаточно данных для сравнения квантилей → ⏳ SKIP")
    else:
        print("1. Нет торгованных пар → ⏳ SKIP")
    
    # Критерий 2: нет регрессии
    profitable_below_50 = [r for r in traded if r['avg_pnl'] and r['avg_pnl'] > 0 and r['q_new'] < 50]
    total_profitable = [r for r in traded if r['avg_pnl'] and r['avg_pnl'] > 0]
    if total_profitable:
        pct_below = len(profitable_below_50) / len(total_profitable) * 100
        c2_pass = pct_below <= 5
        print(f"2. Прибыльных пар с Q_new<50: {len(profitable_below_50)}/{len(total_profitable)} ({pct_below:.0f}%) → {'✅ PASS' if c2_pass else '❌ FAIL'}")
    else:
        print("2. Нет прибыльных пар → ⏳ SKIP")
    
    # Критерий 3: распределение (двугорбость)
    import statistics
    q_new_all = [r['q_new'] for r in results]
    if len(q_new_all) >= 10:
        stdev = statistics.stdev(q_new_all)
        median = statistics.median(q_new_all)
        # Простая проверка: если stdev > 15 и есть пары в диапазоне 40-90 — нормально
        in_range = sum(1 for q in q_new_all if 40 <= q <= 90)
        pct_in_range = in_range / len(q_new_all) * 100
        c3_pass = pct_in_range >= 60
        print(f"3. Q_new в диапазоне 40-90: {in_range}/{len(q_new_all)} ({pct_in_range:.0f}%), stdev={stdev:.1f} → {'✅ PASS' if c3_pass else '❌ FAIL'}")
    else:
        print(f"3. Мало данных ({len(q_new_all)} пар) → ⏳ SKIP")
    
    print(f"\n{'='*80}")
    print("Для полноценной симуляции накопите данные за 5-7 дней (10+ сканов).")
    print(f"{'='*80}\n")


# ═══════════════════════════════════════════════════════
# PRE-FILTER DRY-RUN АНАЛИЗ
# ═══════════════════════════════════════════════════════

def run_prefilter_analysis(log_path="scan_exports/quality_breakdown_log.jsonl",
                           pairs_path=None,
                           threshold=0.10):
    """
    Читает quality_breakdown_log.jsonl, ищет поля prefilter_raw_pvalue и
    prefilter_would_block, выводит статистику:

      - Сколько % пар было бы заблокировано
      - Какие конкретно пары были бы отрезаны
      - Есть ли среди них прибыльные (если передан pairs_path → pair_memory.json)

    Запуск:
      python q_score_simulation.py --prefilter
      python q_score_simulation.py --prefilter --log path/to/log.jsonl
      python q_score_simulation.py --prefilter --log log.jsonl --pairs pair_memory.json
    """
    if not os.path.exists(log_path):
        print(f"[prefilter] Файл не найден: {log_path}")
        print("  Передайте путь через --log  или убедитесь что бот пишет prefilter-поля в лог.")
        return

    # ── Читаем лог ──────────────────────────────────────────────────────────
    records = []
    has_prefilter = 0
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            bd = obj.get('quality_bd') or obj.get('quality_breakdown') or {}
            raw_p = bd.get('prefilter_raw_pvalue')
            would_block = bd.get('prefilter_would_block')
            if raw_p is not None:
                has_prefilter += 1
            records.append({
                'ts':           obj.get('ts', ''),
                'pair':         obj.get('pair', ''),
                'quality':      obj.get('quality', 0),
                'raw_pvalue':   raw_p,
                'would_block':  would_block,
                'signal':       obj.get('signal', ''),
            })

    total = len(records)
    print(f"\n{'='*72}")
    print("PRE-FILTER DRY-RUN — анализ quality_breakdown_log.jsonl")
    print(f"{'='*72}")
    print(f"Записей в логе:              {total}")
    print(f"Записей с prefilter-данными: {has_prefilter}")

    if has_prefilter == 0:
        print("\n⚠️  Поля prefilter_raw_pvalue отсутствуют в логе.")
        print("   Убедитесь что:")
        print("   1. mean_reversion_analysis.py обновлён (v45 + prefilter)")
        print("   2. Бот передаёт pvalue_raw= в calculate_quality_score()")
        print("   3. Бот записывает quality_bd в quality_breakdown_log.jsonl")
        return

    with_data = [r for r in records if r['raw_pvalue'] is not None]
    blocked   = [r for r in with_data if r['would_block']]
    passed    = [r for r in with_data if not r['would_block']]

    pct_blocked = len(blocked) / len(with_data) * 100 if with_data else 0

    print(f"\nПорог pre-filter:    raw p-value > {threshold}")
    print(f"Всего с raw p:       {len(with_data)}")
    print(f"Прошли бы фильтр:    {len(passed)} ({100-pct_blocked:.1f}%)")
    print(f"Были бы заблокированы: {len(blocked)} ({pct_blocked:.1f}%)")

    # ── Распределение Q среди заблокированных ───────────────────────────────
    if blocked:
        blocked_q = [r['quality'] for r in blocked]
        avg_q_blocked = sum(blocked_q) / len(blocked_q)
        above_threshold = [q for q in blocked_q if q >= 65]
        print(f"\nСреди заблокированных:")
        print(f"  Средний Q:          {avg_q_blocked:.1f}")
        print(f"  С Q≥65 (опасно!):   {len(above_threshold)} из {len(blocked)}")
        print(f"  С сигналом SIGNAL:  {sum(1 for r in blocked if r['signal'] == 'SIGNAL')}")

    # ── Топ заблокированных по Q ────────────────────────────────────────────
    if blocked:
        top_blocked = sorted(blocked, key=lambda r: r['quality'], reverse=True)
        seen = {}
        unique_blocked = []
        for r in top_blocked:
            if r['pair'] not in seen:
                seen[r['pair']] = r
                unique_blocked.append(r)

        print(f"\nТоп-20 пар которые были бы заблокированы (по Q):")
        print(f"  {'Пара':<18} {'Q':>4}  {'raw_p':>8}  {'Сигнал'}")
        print(f"  {'-'*18} {'-'*4}  {'-'*8}  {'-'*10}")
        for r in unique_blocked[:20]:
            flag = " ⚠️ Q≥65!" if r['quality'] >= 65 else ""
            print(f"  {r['pair']:<18} {r['quality']:>4}  {r['raw_pvalue']:>8.4f}  {r['signal']}{flag}")

    # ── Сопоставление с pair_memory (если передан) ──────────────────────────
    if pairs_path and os.path.exists(pairs_path):
        with open(pairs_path) as f:
            memory = json.load(f)

        print(f"\nСопоставление с pair_memory ({pairs_path}):")
        print(f"  {'Пара':<18} {'Trades':>6}  {'WR%':>5}  {'AvgPnL':>7}  {'Q':>4}  {'Вердикт'}")
        print(f"  {'-'*18} {'-'*6}  {'-'*5}  {'-'*7}  {'-'*4}  {'-'*12}")

        blocked_pairs = {r['pair'] for r in blocked}
        risky = []
        for pair in blocked_pairs:
            if pair in memory:
                m = memory[pair]
                trades = m.get('trades', 0)
                wins = m.get('wins', 0)
                total_pnl = m.get('total_pnl', 0)
                if trades > 0:
                    wr = wins / trades * 100
                    avg_pnl = total_pnl / trades
                    q_val = seen.get(pair, {}).get('quality', 0)
                    verdict = "✅ прибыльная" if avg_pnl > 0 else "❌ убыточная"
                    risky.append((pair, trades, wr, avg_pnl, q_val, verdict))

        risky.sort(key=lambda x: x[3], reverse=True)
        for pair, trades, wr, avg_pnl, q_val, verdict in risky:
            flag = " ← ОСТОРОЖНО" if avg_pnl > 0 else ""
            print(f"  {pair:<18} {trades:>6}  {wr:>5.0f}  {avg_pnl:>+7.2f}%  {q_val:>4}  {verdict}{flag}")

        profitable_blocked = [x for x in risky if x[3] > 0]
        print(f"\nИтог: {len(risky)} пар из pair_memory попали бы под блокировку,")
        print(f"      из них прибыльных: {len(profitable_blocked)}")
        if profitable_blocked:
            print("  ⚠️  Перед включением жёсткого фильтра проверьте эти пары!")
        else:
            print("  ✅ Все заблокированные пары убыточны или не торговались — фильтр безопасен.")
    else:
        if pairs_path:
            print(f"\n[prefilter] pair_memory не найден: {pairs_path} — пропускаю сопоставление.")
        else:
            print(f"\nПодсказка: передайте --pairs pair_memory.json для сопоставления с историей торговли.")

    print(f"\n{'='*72}")
    print("Вывод: это DRY-RUN — пары НЕ блокируются, только логируются.")
    print("Накопите 3-5 дней данных, затем решайте о жёстком включении.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    # Разбор аргументов
    args = sys.argv[1:]

    if "--prefilter" in args:
        log_path = "scan_exports/quality_breakdown_log.jsonl"
        pairs_path = None
        i = 0
        while i < len(args):
            if args[i] == "--log" and i + 1 < len(args):
                log_path = args[i + 1]; i += 2
            elif args[i] == "--pairs" and i + 1 < len(args):
                pairs_path = args[i + 1]; i += 2
            else:
                i += 1
        run_prefilter_analysis(log_path, pairs_path)
    else:
        path = "scan_exports/quality_breakdown_log.jsonl"
        if len(args) > 1 and args[0] == "--log":
            path = args[1]
        run_simulation(path)
