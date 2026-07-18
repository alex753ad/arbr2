"""
infra/notifications.py — Единый модуль уведомлений.

Ранее send_telegram дублировался в 3 файлах:
  - monitor_v38_3.py (с IP-fallback + SSL disable)
  - bybit_executor.py (_emergency_alert)
  - monitor_daemon.py (alert)

Теперь: одна реализация, retry 3x, только api.telegram.org (SEC-02 FIX).
"""

import json
import logging
import os
import time
import urllib.request
import urllib.parse

_logger = logging.getLogger("infra.notifications")

# Telegram API — только HTTPS с полной SSL-проверкой (SEC-02 FIX)
_TG_URL = "https://api.telegram.org"


def send_telegram(
    token: str,
    chat_id: str,
    message: str,
    retry: int = 3,
    parse_mode: str = "HTML",
) -> tuple[bool, str]:
    """Отправить сообщение в Telegram с retry.
    
    SEC-02 FIX: только api.telegram.org, полная SSL-проверка.
    ERR-02 FIX: retry 3x с exponential backoff.
    
    Returns: (success: bool, detail: str)
    """
    if not token or not chat_id:
        return False, "Token или Chat ID не заданы"

    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode('utf-8')

    last_err = ""
    for attempt in range(retry):
        try:
            url = f"{_TG_URL}/bot{token}/sendMessage"
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode()
                data = json.loads(body)
                if data.get('ok'):
                    return True, "OK"
                return False, data.get('description', 'Unknown error')
        except Exception as e:
            last_err = str(e)[:120]
            _logger.warning("Telegram attempt %d/%d: %s", attempt + 1, retry, last_err)
            if attempt < retry - 1:
                time.sleep(1.0 * (attempt + 1))  # 1s, 2s

    _logger.error("Telegram НЕДОСТУПЕН после %d попыток", retry)
    return False, f"All attempts failed. Last: {last_err}"


def send_webhook(
    url: str,
    message: str,
    retry: int = 2,
) -> tuple[bool, str]:
    """Отправить сообщение через webhook (Slack, Discord, custom).
    
    Payload: {"text": message, "content": message}
    (text для Slack, content для Discord)
    """
    if not url:
        return False, "Webhook URL не задан"

    payload = json.dumps({"text": message, "content": message}).encode('utf-8')
    last_err = ""

    for attempt in range(retry):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            return True, "OK"
        except Exception as e:
            last_err = str(e)[:120]
            if attempt < retry - 1:
                time.sleep(1.0)

    return False, f"Webhook failed: {last_err}"


def emergency_alert(
    message: str,
    tg_token: str = "",
    tg_chat_id: str = "",
    webhook_url: str = "",
    log_file: str = "bybit_emergency.log",
) -> None:
    """Критический алерт — файл + Telegram + webhook.
    
    Вынесено из bybit_executor._emergency_alert().
    Порядок: (1) файл всегда, (2) Telegram 3x, (3) webhook 2x.
    """
    from ..core.utils import now_msk

    # 1. Файл (всегда)
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{now_msk().isoformat()} {message}\n{'='*60}\n")
    except Exception as e:
        _logger.error("emergency_alert log write failed: %s", e)

    _logger.critical(message)

    # 2. Telegram — env vars → explicit params
    _token = tg_token or os.environ.get("TG_TOKEN", "")
    _chat = tg_chat_id or os.environ.get("TG_CHAT_ID", "")
    if not _token or not _chat:
        try:
            from .config import CFG
            _token = _token or CFG("telegram", "token", "")
            _chat = _chat or CFG("telegram", "chat_id", "")
        except Exception:
            pass

    if _token and _chat:
        ok, detail = send_telegram(_token, _chat, message, retry=3)
        if not ok:
            _logger.error("emergency_alert Telegram failed: %s", detail)

    # 3. Webhook
    _wh = webhook_url
    if not _wh:
        try:
            from .config import CFG
            _wh = CFG("bybit", "emergency_webhook", "")
        except Exception:
            pass
    if _wh:
        ok, detail = send_webhook(_wh, message)
        if not ok:
            _logger.warning("emergency_alert webhook failed: %s", detail)
