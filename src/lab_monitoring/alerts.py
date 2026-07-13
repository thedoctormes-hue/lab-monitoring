"""Telegram alerter for lab_monitoring (no external deps; uses urllib).

Replaces the missing `lab_monitoring.alerts` module that send_report.py
previously imported. Keeps the same public surface (TelegramAlerter._send).
"""
from __future__ import annotations

import urllib.parse
import urllib.request


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _send(self, text: str) -> bool:
        """Send a message; return True on HTTP 200, False on any failure."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception:
            return False
