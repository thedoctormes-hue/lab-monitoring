#!/usr/bin/env python3
"""Группированный отчёт инфраструктуры в Telegram (по systemd timer, ~раз в 6ч).

Собирает ЖИВЫЕ метрики (system_check + PG-backup + alert_rules) и шлёт
одним сообщением. Не дублирует ежечасный ЛабМонитор (Доминики): это редкая
сводка инфраструктуры, а не детальный ежечасный ливенесс-отчёт.

Исправлен 2026-07-13 (DDP): ранее импортировал несуществующие
lab_monitoring.config / .alerts / _build_report_from_db. Теперь собирает
отчёт из живого сбора (без БД).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab_monitoring.config import load_config
from lab_monitoring.alerts import TelegramAlerter
from lab_monitoring.system_check import collect_system_metrics
from lab_monitoring.backup_monitor import check_pg_backups
from lab_monitoring.alert_rules import evaluate_all, Alert


def _fmt_alert(a: Alert) -> str:
    return f"[{a.severity}] {a.source}: {a.message}"


def build_report_text() -> str:
    metrics = collect_system_metrics()
    alerts = evaluate_all(metrics)
    lines = ["📊 Сводка инфраструктуры (lab_monitoring)"]

    disk = metrics.get("disk", {})
    mem = metrics.get("memory", {})
    load = metrics.get("load", [])
    lines.append(f"💾 Диск {disk.get('path', '/')}: {disk.get('used_percent', 0)}%")
    lines.append(f"🧠 RAM: {mem.get('used_percent', 0)}%")
    if load:
        lines.append(f"⚡ Load 1m: {load[0]}")
    for c in metrics.get("docker", []):
        lines.append(f"🐳 {c.get('name', '?')}: {c.get('status', '?')}")
    for svc in metrics.get("failed_services", []):
        lines.append(f"⚠️ failed: {svc}")

    if alerts:
        lines.append("")
        lines.append("🚨 Алерты:")
        for a in alerts:
            lines.append("  " + _fmt_alert(a))
    else:
        lines.append("✅ Алертов нет")
    return "\n".join(lines)


async def _backup_summary() -> str:
    try:
        res = await check_pg_backups()
        return f"🗄️ PG-бэкап: статус={res.status}, всего={res.total_backups}"
    except Exception as e:  # noqa: BLE001
        return f"🗄️ PG-бэкап: ошибка {e}"


async def main() -> None:
    config = load_config()
    if not config.telegram.bot_token or not config.telegram.chat_id:
        print("❌ Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        sys.exit(1)
    text = build_report_text() + "\n" + await _backup_summary()
    alerter = TelegramAlerter(config.telegram.bot_token, config.telegram.chat_id)
    if alerter._send(text):
        print("✅ Сводка отправлена в Telegram")
    else:
        print("❌ Ошибка отправки")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
