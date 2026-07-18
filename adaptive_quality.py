"""
engine/adaptive_quality.py — Adaptive Quality Gate (ANALYSIS-v51)

Автоматически двигает порог min_quality в зависимости от количества сигналов в скане.

Логика:
  signal_count < target_min → снизить min_quality на step (нижняя граница q_min)
  signal_count > target_max → повысить min_quality на step (верхняя граница q_max)
  иначе                     → не менять

Состояние хранится в adaptive_quality_state.json рядом с rally_state.json.
app.py пишет после каждого скана, читает перед каждым следующим.
monitor_daemon.py читает при process_pending() для логирования.

Пример state файла:
{
  "current_q": 60,
  "last_updated": "2026-04-05T17:30:00+03:00",
  "last_signal_count": 2,
  "change_history": [
    {"ts": "...", "old_q": 63, "new_q": 60, "reason": "signal_count=2 < target_min=3"}
  ]
}
"""

from __future__ import annotations
import json
import os
import logging
from datetime import datetime, timezone, timedelta

_logger = logging.getLogger("engine.adaptive_quality")

STATE_FILENAME = "adaptive_quality_state.json"
MSK = timezone(timedelta(hours=3))


def _now_msk() -> datetime:
    return datetime.now(MSK)


def load_state(base_dir: str) -> dict:
    """Загрузить текущее состояние adaptive quality из файла.
    Если файла нет — вернуть дефолт с q_default из конфига.
    """
    path = os.path.join(base_dir, STATE_FILENAME)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "current_q" in data:
                return data
    except Exception as e:
        _logger.warning("Не удалось прочитать %s: %s", STATE_FILENAME, e)

    try:
        from config_loader import CFG
        default_q = int(CFG("adaptive_quality", "q_default", 63))
    except Exception:
        default_q = 63

    return {
        "current_q": default_q,
        "last_updated": None,
        "last_signal_count": None,
        "change_history": [],
    }


def save_state(base_dir: str, state: dict) -> None:
    """Сохранить состояние в файл."""
    path = os.path.join(base_dir, STATE_FILENAME)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        _logger.error("Не удалось сохранить %s: %s", STATE_FILENAME, e)


def get_current_q(base_dir: str, cfg_fn=None) -> int:
    """Получить текущий активный порог quality.
    Вызывается перед каждым сканом и перед записью pending.
    """
    state = load_state(base_dir)
    return int(state.get("current_q", 63))


def update_after_scan(
    base_dir: str,
    signal_count: int,
    cfg_fn=None,
) -> tuple[int, int, str]:
    """Обновить порог после скана на основе количества сигналов.

    Args:
        base_dir: путь к директории с state-файлами
        signal_count: кол-во уникальных SIGNAL+READY пар в текущем скане
        cfg_fn: callable CFG (если None — импортируется автоматически)

    Returns:
        (old_q, new_q, reason) — старый порог, новый порог, описание изменения
    """
    if cfg_fn is None:
        try:
            from config_loader import CFG
            cfg_fn = CFG
        except ImportError:
            def cfg_fn(section, key, default=None):
                return default

    enabled = cfg_fn("adaptive_quality", "enabled", True)
    if not enabled:
        state = load_state(base_dir)
        q = int(state.get("current_q", 63))
        return q, q, "adaptive_quality disabled"

    q_min = int(cfg_fn("adaptive_quality", "q_min", 50))
    q_max = int(cfg_fn("adaptive_quality", "q_max", 70))
    step = int(cfg_fn("adaptive_quality", "step", 3))
    target_min = int(cfg_fn("adaptive_quality", "target_min", 3))
    target_max = int(cfg_fn("adaptive_quality", "target_max", 8))
    cooldown_min = float(cfg_fn("adaptive_quality", "cooldown_min", 30))

    state = load_state(base_dir)
    old_q = int(state.get("current_q", 63))
    now = _now_msk()

    # Cooldown check
    last_updated = state.get("last_updated")
    if last_updated:
        try:
            last_dt = datetime.fromisoformat(last_updated)
            if (now - last_dt).total_seconds() < cooldown_min * 60:
                remaining = cooldown_min - (now - last_dt).total_seconds() / 60
                reason = f"cooldown: ещё {remaining:.0f} мин до следующего изменения"
                state["last_signal_count"] = signal_count
                save_state(base_dir, state)
                return old_q, old_q, reason
        except Exception:
            pass

    # Determine new Q
    new_q = old_q
    reason = f"signal_count={signal_count} в целевом диапазоне [{target_min},{target_max}] — без изменений"

    if signal_count < target_min:
        new_q = max(q_min, old_q - step)
        if new_q != old_q:
            reason = (
                f"signal_count={signal_count} < target_min={target_min} → "
                f"снижаем min_quality {old_q} → {new_q}"
            )
        else:
            reason = f"signal_count={signal_count} < target_min={target_min}, но уже на минимуме q_min={q_min}"

    elif signal_count > target_max:
        # ANALYSIS-v53: повышаем Q только если сигналов > target_max * 1.5 (намного больше нормы).
        # Если сигналов чуть больше порога — это нормальная волатильность рынка,
        # не повод поднимать порог. Иначе Q улетает к q_max и блокирует все сделки.
        raise_threshold = int(target_max * 1.5)
        if signal_count > raise_threshold:
            new_q = min(q_max, old_q + step)
            if new_q != old_q:
                reason = (
                    f"signal_count={signal_count} > raise_threshold={raise_threshold} → "
                    f"повышаем min_quality {old_q} → {new_q}"
                )
            else:
                reason = f"signal_count={signal_count} > {raise_threshold}, но уже на максимуме q_max={q_max}"
        else:
            reason = (
                f"signal_count={signal_count} в зоне [{target_max},{raise_threshold}] — "
                f"удерживаем Q={old_q} (не поднимаем без явного избытка сигналов)"
            )

    # Update state
    state["current_q"] = new_q
    state["last_signal_count"] = signal_count
    state["last_updated"] = now.isoformat()

    if new_q != old_q:
        history = state.get("change_history", [])
        history.append({
            "ts": now.isoformat(),
            "old_q": old_q,
            "new_q": new_q,
            "signal_count": signal_count,
            "reason": reason,
        })
        # Держим только последние 50 изменений
        state["change_history"] = history[-50:]
        _logger.info("Adaptive quality: %s", reason)

    save_state(base_dir, state)
    return old_q, new_q, reason


def get_status_str(base_dir: str) -> str:
    """Короткая строка для отображения в UI.
    Пример: 'Q=60 ↓ (было 63, 2 сигнала)'
    """
    state = load_state(base_dir)
    q = state.get("current_q", 63)
    cnt = state.get("last_signal_count")
    history = state.get("change_history", [])

    arrow = ""
    if len(history) >= 2:
        prev_q = history[-2]["new_q"]
        if q < prev_q:
            arrow = " ↓"
        elif q > prev_q:
            arrow = " ↑"
    elif len(history) == 1:
        prev_q = history[-1]["old_q"]
        if q < prev_q:
            arrow = " ↓"
        elif q > prev_q:
            arrow = " ↑"

    cnt_str = f", {cnt} сигн." if cnt is not None else ""
    return f"Q={q}{arrow}{cnt_str}"
