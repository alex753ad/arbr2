"""
engine/adaptive_exits.py — Автокалибровка параметров выхода (ANALYSIS-v53)

Адаптирует два параметра на основе накопленной phantom-статистики:

  stale_exit_hours   — сколько часов ждать перед выходом «мёртвой» сделки.
                       Снижается если dead-пары (best=0) в среднем уходят
                       глубже в минус со временем.

  trailing_drawdown_pct — допустимый откат от пика при trail-выходе.
                          Снижается если реальные дропы стабильно меньше порога
                          (оставляем прибыль на столе), растёт если trail срабатывает
                          слишком рано (отрезает хвост движения).

Алгоритм (вызывается daemon'ом после каждого закрытия позиции):
  1. Читает последние N закрытых phantom-записей из phantom-файла или state.
  2. Считает rolling-медиану нужного показателя.
  3. Корректирует параметр с шагом step, не выходя за [min, max].
  4. Сохраняет результат в adaptive_exits_state.json.
  5. Daemon читает state перед каждым monitor-тиком и перегружает CFG.

State-файл (adaptive_exits_state.json):
{
  "stale_exit_hours": 4.5,
  "trailing_drawdown_pct": 0.40,
  "last_updated": "2026-04-11T14:00:00+03:00",
  "sample_n": 12,
  "change_history": [...]
}
"""

from __future__ import annotations
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

_logger = logging.getLogger("engine.adaptive_exits")

STATE_FILENAME = "adaptive_exits_state.json"
MSK = timezone(timedelta(hours=3))

# ─── Defaults ────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "stale_exit_hours":       6.0,
    "trailing_drawdown_pct":  0.50,
}

# ─── Limits (hard floor/ceiling) ─────────────────────────────────────────────
_LIMITS = {
    "stale_exit_hours":      (2.0,  8.0),
    "trailing_drawdown_pct": (0.25, 0.65),
}

# ─── Step per adjustment ─────────────────────────────────────────────────────
_STEPS = {
    "stale_exit_hours":      0.5,
    "trailing_drawdown_pct": 0.05,
}

# Минимальная выборка для коррекции (игнорируем если данных мало)
_MIN_SAMPLE = 5
# Не меняем чаще чем раз в N минут
_COOLDOWN_MIN = 60


def _now_msk() -> datetime:
    return datetime.now(MSK)


def load_state(base_dir: str) -> dict:
    path = os.path.join(base_dir, STATE_FILENAME)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "stale_exit_hours" in data:
                return data
    except Exception as e:
        _logger.warning("Не удалось прочитать %s: %s", STATE_FILENAME, e)
    return {**_DEFAULTS, "last_updated": None, "sample_n": 0, "change_history": []}


def save_state(base_dir: str, state: dict) -> None:
    path = os.path.join(base_dir, STATE_FILENAME)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        _logger.error("Не удалось сохранить %s: %s", STATE_FILENAME, e)


def get_current_params(base_dir: str) -> dict:
    """Вернуть текущие адаптивные параметры. Вызывается daemon'ом."""
    state = load_state(base_dir)
    return {
        "stale_exit_hours":      float(state.get("stale_exit_hours", _DEFAULTS["stale_exit_hours"])),
        "trailing_drawdown_pct": float(state.get("trailing_drawdown_pct", _DEFAULTS["trailing_drawdown_pct"])),
    }


def _clamp(value: float, key: str) -> float:
    lo, hi = _LIMITS[key]
    return max(lo, min(hi, value))


def _median(values: list) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def update_from_closed_positions(
    base_dir: str,
    closed_positions: list,
    cfg_fn=None,
) -> dict:
    """Обновить параметры по списку недавно закрытых позиций.

    Args:
        base_dir: путь к директории state-файлов
        closed_positions: список dict с полями:
            exit_reason (str), pnl_pct (float), best_pnl (float),
            hours_in_trade (float), exit_type (str)
        cfg_fn: callable CFG (опционально, для чтения настроек)

    Returns:
        dict с новыми значениями параметров + изменениями
    """
    state = load_state(base_dir)
    now = _now_msk()

    # Cooldown check
    last_upd = state.get("last_updated")
    if last_upd:
        try:
            last_dt = datetime.fromisoformat(last_upd)
            elapsed = (now - last_dt).total_seconds() / 60
            if elapsed < _COOLDOWN_MIN:
                return state
        except Exception:
            pass

    if len(closed_positions) < _MIN_SAMPLE:
        return state

    changes = []

    # ── 1. stale_exit_hours ──────────────────────────────────────────────────
    # Берём сделки где best_pnl <= 0.1% (никогда не шли в плюс)
    # Оптимальный порог = P25 их часов (выходим в первом квартиле)
    dead_hours = [
        p["hours_in_trade"]
        for p in closed_positions
        if p.get("best_pnl", 0) <= 0.1
        and p.get("hours_in_trade", 0) > 0
    ]

    if len(dead_hours) >= _MIN_SAMPLE:
        sorted_h = sorted(dead_hours)
        p25 = sorted_h[len(sorted_h) // 4]
        target_stale = round(p25, 1)
        current_stale = float(state.get("stale_exit_hours", _DEFAULTS["stale_exit_hours"]))
        step = _STEPS["stale_exit_hours"]

        if target_stale < current_stale - step * 0.5:
            new_stale = _clamp(current_stale - step, "stale_exit_hours")
            if new_stale != current_stale:
                changes.append(f"stale_exit_hours {current_stale:.1f}→{new_stale:.1f} "
                                f"(P25 dead_hours={p25:.1f}h, n={len(dead_hours)})")
                state["stale_exit_hours"] = new_stale
        elif target_stale > current_stale + step * 0.5:
            new_stale = _clamp(current_stale + step, "stale_exit_hours")
            if new_stale != current_stale:
                changes.append(f"stale_exit_hours {current_stale:.1f}→{new_stale:.1f} "
                                f"(P25 dead_hours={p25:.1f}h растёт, n={len(dead_hours)})")
                state["stale_exit_hours"] = new_stale

    # ── 2. trailing_drawdown_pct ─────────────────────────────────────────────
    # Берём TRAIL/Z_TRAIL выходы, считаем реальный drop (best - exit_pnl)
    # Оптимальный drawdown = медиана реальных drop * 0.85 (чуть тесней)
    trail_drops = [
        p.get("best_pnl", 0) - p.get("pnl_pct", 0)
        for p in closed_positions
        if "TRAIL" in p.get("exit_reason", "")
        and p.get("best_pnl", 0) > 0.1
    ]

    if len(trail_drops) >= _MIN_SAMPLE:
        med_drop = _median(trail_drops)
        if med_drop is not None and med_drop > 0:
            target_dd = round(med_drop * 0.85, 2)
            current_dd = float(state.get("trailing_drawdown_pct", _DEFAULTS["trailing_drawdown_pct"]))
            step = _STEPS["trailing_drawdown_pct"]

            if target_dd < current_dd - step * 0.5:
                new_dd = _clamp(current_dd - step, "trailing_drawdown_pct")
                if new_dd != current_dd:
                    changes.append(f"trailing_drawdown {current_dd:.2f}→{new_dd:.2f} "
                                   f"(median drop={med_drop:.2f}%, n={len(trail_drops)})")
                    state["trailing_drawdown_pct"] = new_dd
            elif target_dd > current_dd + step * 0.5:
                new_dd = _clamp(current_dd + step, "trailing_drawdown_pct")
                if new_dd != current_dd:
                    changes.append(f"trailing_drawdown {current_dd:.2f}→{new_dd:.2f} "
                                   f"(median drop={med_drop:.2f}% растёт, n={len(trail_drops)})")
                    state["trailing_drawdown_pct"] = new_dd

    # ── Persist ──────────────────────────────────────────────────────────────
    state["last_updated"] = now.isoformat()
    state["sample_n"] = len(closed_positions)

    if changes:
        history = state.get("change_history", [])
        for ch in changes:
            history.append({"ts": now.isoformat(), "change": ch})
            _logger.info("Adaptive exits: %s", ch)
        state["change_history"] = history[-50:]

    save_state(base_dir, state)
    return state


def get_status_str(base_dir: str) -> str:
    """Короткая строка для UI/лога."""
    state = load_state(base_dir)
    stale = state.get("stale_exit_hours", _DEFAULTS["stale_exit_hours"])
    dd = state.get("trailing_drawdown_pct", _DEFAULTS["trailing_drawdown_pct"])
    n = state.get("sample_n", 0)
    return f"stale={stale:.1f}h · trail_dd={dd:.2f} · n={n}"
