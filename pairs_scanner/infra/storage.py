"""
infra/storage.py — Единый фасад хранилища: SQLite (primary) + JSON (fallback).

Извлечено из db_store.py + monitor_v38_3.py (Волна 3).
Все функции работают через db_store если доступен, иначе JSON.
PERF-03: update_position() — атомарный UPDATE одной строки.
"""

import json
import os
import logging
from contextlib import contextmanager
from ..core.utils import atomic_json_save

_logger = logging.getLogger("infra.storage")

# ═══════════════════════════════════════════════════════
# DB BACKEND DETECTION
# ═══════════════════════════════════════════════════════

_db_available = False
try:
    from db_store import (
        db_load_positions, db_save_positions, db_update_position,
        db_get_open_positions, db_get_next_id,
        db_load_cooldowns, db_save_cooldowns, db_update_cooldown, db_get_cooldown,
        db_pair_memory_load, db_pair_memory_save, db_pair_memory_get, db_pair_memory_update,
        ensure_db,
    )
    ensure_db()
    _db_available = True
    _logger.info("storage: SQLite backend (db_store) доступен")
except ImportError:
    _logger.info("storage: SQLite недоступен — JSON fallback")


# ═══════════════════════════════════════════════════════
# PATH RESOLUTION
# ═══════════════════════════════════════════════════════

def _resolve_path(filename):
    """Resolve data file path: look in CWD, then project root."""
    if os.path.exists(filename):
        return filename
    _dir = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(os.path.dirname(_dir))
    candidate = os.path.join(_root, filename)
    return candidate if os.path.exists(candidate) else filename


POSITIONS_FILE = _resolve_path("positions.json")
COOLDOWNS_FILE = _resolve_path("pair_cooldowns.json")
PAIR_MEMORY_FILE = _resolve_path("pair_memory.json")


# ═══════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════

def load_positions(status_filter=None):
    """Load positions. SQLite primary, JSON fallback.
    
    Args:
        status_filter: 'OPEN', 'CLOSED', or None (all)
    Returns: list[dict]
    """
    if _db_available:
        try:
            return db_load_positions(status_filter=status_filter)
        except Exception as e:
            _logger.warning("db_load_positions failed, JSON fallback: %s", e)
    # JSON fallback
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if status_filter:
                return [p for p in data if isinstance(p, dict) and p.get('status') == status_filter]
            return data
    except Exception as e:
        _logger.error("load_positions JSON failed: %s", e)
    return []


def load_open_positions():
    """Shortcut: only OPEN positions (P3.12 FIX)."""
    return load_positions(status_filter='OPEN')


def save_positions(positions):
    """Save all positions. SQLite primary + JSON backup."""
    if _db_available:
        try:
            db_save_positions(positions)
        except Exception as e:
            _logger.warning("db_save_positions failed: %s", e)
    # JSON backup always
    try:
        atomic_json_save(POSITIONS_FILE, positions)
    except Exception as e:
        _logger.error("save_positions JSON failed: %s", e)


def update_position(pos_id, patch):
    """PERF-03: Atomic UPDATE one position by ID.
    SQLite: single UPDATE, no full table rewrite.
    JSON fallback: load all → patch → save all.
    
    Returns: True if found and updated.
    """
    if _db_available:
        try:
            result = db_update_position(pos_id, patch)
            if result:
                return True
        except Exception as e:
            _logger.warning("db_update_position failed, fallback: %s", e)
    # JSON fallback
    try:
        positions = load_positions()
        found = False
        for p in positions:
            if p.get('id') == pos_id:
                p.update(patch)
                found = True
                break
        if found:
            save_positions(positions)
        return found
    except Exception as e:
        _logger.error("update_position JSON failed: %s", e)
        return False


def get_next_id():
    """Next available position ID."""
    if _db_available:
        try:
            return db_get_next_id()
        except Exception:
            pass
    positions = load_positions()
    if not positions:
        return 1
    return max(p.get('id', 0) for p in positions if isinstance(p, dict)) + 1


# ═══════════════════════════════════════════════════════
# COOLDOWNS
# ═══════════════════════════════════════════════════════

def load_cooldowns():
    """Load cooldowns dict {pair: {...}}."""
    if _db_available:
        try:
            return db_load_cooldowns()
        except Exception:
            pass
    try:
        if os.path.exists(COOLDOWNS_FILE):
            with open(COOLDOWNS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_cooldowns(data):
    """Save cooldowns."""
    if _db_available:
        try:
            db_save_cooldowns(data)
        except Exception:
            pass
    try:
        atomic_json_save(COOLDOWNS_FILE, data)
    except Exception:
        pass


def update_cooldown(pair, entry):
    """Upsert cooldown for one pair."""
    if _db_available:
        try:
            db_update_cooldown(pair, entry)
            return
        except Exception:
            pass
    data = load_cooldowns()
    data[pair] = entry
    save_cooldowns(data)


# ═══════════════════════════════════════════════════════
# PAIR MEMORY
# ═══════════════════════════════════════════════════════

def pair_memory_load():
    """Load pair memory dict {pair: {...}}."""
    if _db_available:
        try:
            return db_pair_memory_load()
        except Exception:
            pass
    try:
        if os.path.exists(PAIR_MEMORY_FILE):
            with open(PAIR_MEMORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def pair_memory_get(pair):
    """Get pair memory for one pair."""
    if _db_available:
        try:
            return db_pair_memory_get(pair)
        except Exception:
            pass
    mem = pair_memory_load()
    return mem.get(pair)


def pair_memory_save(data):
    """Save entire pair memory."""
    if _db_available:
        try:
            db_pair_memory_save(data)
        except Exception:
            pass
    try:
        atomic_json_save(PAIR_MEMORY_FILE, data)
    except Exception:
        pass


def pair_memory_update(pair, entry):
    """Upsert pair memory for one pair."""
    if _db_available:
        try:
            db_pair_memory_update(pair, entry)
            return
        except Exception:
            pass
    mem = pair_memory_load()
    mem[pair] = entry
    pair_memory_save(mem)
