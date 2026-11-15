"""Telegram alerts. The trading key is redacted again here in case a caller interpolated it."""
import os
import requests
from trader.core import log, redact


def alert(text):
    text = redact(text)
    print("ALERT: " + text, flush=True)
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True},
            timeout=20,
        )
        return True
    except Exception as e:
        log("exit", "telegram failed", err=redact(str(e))[:200])
        return False
