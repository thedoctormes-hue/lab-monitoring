"""Мониторинг бэкапов PostgreSQL — проверка наличия, свежести, размера."""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class BackupInfo:
    database: str
    path: str
    size_bytes: int = 0
    created_at: str = ""
    age_hours: float = -1
    status: str = "ok"  # ok, stale, missing, error


@dataclass
class BackupCheckResult:
    status: str = "ok"
    backup_dir: str = ""
    total_backups: int = 0
    total_size_bytes: int = 0
    backups: list[BackupInfo] = field(default_factory=list)
    last_backup_hours_ago: float = -1
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "backup_dir": self.backup_dir,
            "total_backups": self.total_backups,
            "total_size_mb": round(self.total_size_bytes / 1048576, 1),
            "last_backup_hours_ago": round(self.last_backup_hours_ago, 1) if self.last_backup_hours_ago >= 0 else -1,
            "backups": [
                {
                    "database": b.database, "path": b.path,
                    "size_mb": round(b.size_bytes / 1048576, 1),
                    "age_hours": round(b.age_hours, 1) if b.age_hours >= 0 else -1,
                    "status": b.status,
                }
                for b in self.backups
            ],
            "timestamp": self.timestamp,
        }


# Известные бэкап-директории (приоритет — первая найденная)
BACKUP_DIRS = [
    "/var/backups/postgresql",
    "/root/LabDoctorM/backups",
    "/root/backups",
    "/backup",
]

# Паттерны файлов бэкапов
BACKUP_PATTERNS = [
    r".*\.sql\.gz$",
    r".*\.dump$",
    r".*\.sql$",
    r".*\.pgdump$",
    r".*backup.*\.gz$",
]


async def _find_backup_dirs() -> list[Path]:
    """Найти существующие директории бэкапов."""
    found = []
    for d in BACKUP_DIRS:
        p = Path(d)
        if p.exists() and p.is_dir():
            found.append(p)
    return found


async def _scan_backups(backup_dir: Path) -> list[BackupInfo]:
    """Сканировать директорию бэкапов."""
    backups = []

    try:
        for f in backup_dir.rglob("*"):
            if not f.is_file():
                continue

            # Проверяем паттерн
            if not any(re.match(p, f.name) for p in BACKUP_PATTERNS):
                continue

            stat = f.stat()
            size = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600

            # Определяем имя БД из имени файла
            db_name = "unknown"
            name_lower = f.name.lower()
            for known_db in ["snablab", "vpn_bot", "postgres", "lab_monitoring", "myrmex"]:
                if known_db in name_lower:
                    db_name = known_db
                    break

            # Статус по возрасту
            status = "ok"
            if age_hours > 48:
                status = "stale"
            if age_hours > 168:  # 7 дней
                status = "missing"  # Файл есть, но очень старый

            backups.append(BackupInfo(
                database=db_name,
                path=str(f),
                size_bytes=size,
                created_at=mtime.isoformat(),
                age_hours=age_hours,
                status=status,
            ))
    except PermissionError:
        logger.warning(f"Permission denied: {backup_dir}")
    except Exception as e:
        logger.error(f"Error scanning {backup_dir}: {e}")

    return backups


async def check_pg_backups() -> BackupCheckResult:
    """Проверить бэкапы PostgreSQL."""
    result = BackupCheckResult(timestamp=datetime.now(timezone.utc).isoformat())

    # Ищем директории бэкапов
    dirs = await _find_backup_dirs()

    if not dirs:
        # Проверяем через pg_dump — есть ли вообще бэкапы
        result.status = "warning"
        result.backup_dir = "not found"
        return result

    all_backups: list[BackupInfo] = []

    for d in dirs:
        result.backup_dir = str(d)
        backups = await _scan_backups(d)
        all_backups.extend(backups)

    # Сортируем по возрасту
    all_backups.sort(key=lambda b: b.age_hours)

    result.backups = all_backups
    result.total_backups = len(all_backups)
    result.total_size_bytes = sum(b.size_bytes for b in all_backups)

    if all_backups:
        result.last_backup_hours_ago = all_backups[0].age_hours

        # Статус
        stale = [b for b in all_backups if b.status == "stale"]
        if stale:
            result.status = "warning"
        if result.last_backup_hours_ago > 168:  # 7 дней
            result.status = "error"
    else:
        result.status = "warning"
        result.last_backup_hours_ago = -1

    return result
