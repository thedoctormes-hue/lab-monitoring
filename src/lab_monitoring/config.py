"""Minimal config loader for lab_monitoring (no external deps).

Loads Telegram bot token / chat id from environment or a .env file.
Designed to replace the missing module that send_report.py previously imported.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _load_dotenv(path: Path) -> None:
    """Best-effort .env parser (no python-dotenv dependency)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class TelegramConfig:
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None


@dataclass
class Config:
    telegram: TelegramConfig


def load_config(path: Optional[str] = None) -> Config:
    base = Path(__file__).resolve().parent.parent
    _load_dotenv(base / ".env")
    if path:
        _load_dotenv(Path(path))
    return Config(
        telegram=TelegramConfig(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        )
    )
