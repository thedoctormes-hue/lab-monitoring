"""Entry point для запуска lab_monitoring через `python3 -m lab_monitoring`.

Exit code is always 0 — the monitoring tool is a reporter, not a gate.
Severity is communicated via JSON output (summary.status, alerts[].severity).
External consumers (alertmanager, scripts) should parse the JSON.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from loguru import logger

from lab_monitoring.backup_monitor import check_pg_backups
from lab_monitoring.system_check import collect_system_metrics
from lab_monitoring.alert_rules import evaluate_all


async def main() -> int:
    logger.info("lab-monitoring: запуск полной проверки")
    try:
        # 1. PostgreSQL backup check
        result = await check_pg_backups()

        # 2. System metrics collection
        sys_metrics = collect_system_metrics()

        # 3. Evaluate alert rules
        alerts = evaluate_all(sys_metrics)
        critical_count = sum(1 for a in alerts if a.severity == "CRITICAL")
        warning_count = sum(1 for a in alerts if a.severity == "WARNING")

        # 4. Combined report
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "backup_check": {
                "status": result.status,
                "backup_dir": result.backup_dir,
                "total_backups": result.total_backups,
            },
            "system_metrics": sys_metrics,
            "alerts": [a.to_dict() for a in alerts],
            "summary": {
                "status": "critical" if critical_count > 0 else ("warning" if warning_count > 0 else "ok"),
                "critical_count": critical_count,
                "warning_count": warning_count,
                "total_alerts": len(alerts),
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))

        # Log summary
        summary = report["summary"]
        if summary["status"] == "ok":
            logger.info(f"lab-monitoring: всё ОК, бэкапов: {result.total_backups}")
        else:
            logger.warning(f"lab-monitoring: {summary['critical_count']} critical, {summary['warning_count']} warning")

        return 0
    except Exception as e:
        logger.error(f"lab-monitoring: ошибка — {e}")
        # Even on internal error, return valid JSON so consumers can parse it
        error_report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {"status": "error", "message": str(e)},
            "alerts": [],
        }
        print(json.dumps(error_report, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
