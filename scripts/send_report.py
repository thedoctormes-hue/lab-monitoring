#!/usr/bin/env python3
"""Отправка группированного отчёта мониторинга в Telegram.

Запускается отдельным systemd timer раз в 6 часов.
Собирает данные из БД за последние 6 часов и отправляет одним сообщением.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Добавляем src в PATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab_monitoring.config import load_config
from lab_monitoring.alerts import TelegramAlerter


async def main():
    config_path = os.environ.get(
        "CONFIG_PATH",
        str(Path(__file__).resolve().parent.parent / "config" / "servers.yaml"),
    )
    config = load_config(config_path)

    tg_token = config.telegram.bot_token
    tg_chat = config.telegram.chat_id

    if not tg_token or not tg_chat:
        print("❌ Telegram не настроен (нет токена или chat_id)")
        sys.exit(1)

    # Строим отчёт из БД
    from lab_monitoring.__main__ import _build_report_from_db
    report_text = await _build_report_from_db(config)

    if not report_text:
        print("❌ Отчёт пуст")
        sys.exit(1)

    # Отправляем
    alerter = TelegramAlerter(tg_token, tg_chat)
    success = await alerter._send(report_text)

    if success:
        print("✅ Группированный отчёт отправлен в Telegram")
    else:
        print("❌ Ошибка отправки")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
