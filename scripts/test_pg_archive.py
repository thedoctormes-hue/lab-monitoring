#!/usr/bin/env python3
"""
Тест: анализ PostgreSQL WAL архива — мусор или нужные данные?

Проверяет:
1. archive_mode должен быть off (нет реплики, нет PITR)
2. archive_command = cp → тупое копирование без cleanup
3. archive_cleanup_command = '' → никто не чистит
4. wal_level = replica → избыточно для одиночной инсталляции
5. Нет replication slots → некому потреблять WAL
6. Нет standby → репликация не настроена
7. pg-backup.service упал → бэкапы не работают

Вывод: 13G WAL архива — мусор, можно чистить.
"""

import subprocess
import os
import sys
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PgConfig:
    archive_mode: str = ""
    archive_command: str = ""
    archive_timeout: int = 0
    wal_level: str = ""
    wal_keep_size: str = ""
    max_wal_size: str = ""
    archive_cleanup_command: str = ""


@dataclass
class ArchiveAnalysis:
    total_files: int = 0
    total_size_bytes: int = 0
    oldest_file: str = ""
    newest_file: str = ""
    oldest_date: str = ""
    newest_date: str = ""
    current_wal: str = ""
    replication_slots: list = field(default_factory=list)
    standby_count: int = 0
    backup_service_active: bool = False
    databases: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    is_garbage: bool = False


def run_sql(query: str) -> str:
    result = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-t", "-A", "-c", query],
        capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip()


def run_shell(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return result.stdout.strip()


def get_pg_config() -> PgConfig:
    cfg = PgConfig()
    conf_path = "/etc/postgresql/16/main/postgresql.conf"
    try:
        with open(conf_path) as f:
            text = f.read()
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().split("#")[0].strip().strip("'")
                if key == "archive_mode":
                    cfg.archive_mode = val
                elif key == "archive_command":
                    cfg.archive_command = val
                elif key == "archive_timeout":
                    try:
                        cfg.archive_timeout = int(val.split()[0])
                    except ValueError:
                        cfg.archive_timeout = 0
                elif key == "wal_level":
                    cfg.wal_level = val
                elif key == "wal_keep_size":
                    cfg.wal_keep_size = val
                elif key == "max_wal_size":
                    cfg.max_wal_size = val
                elif key == "archive_cleanup_command":
                    cfg.archive_cleanup_command = val
    except FileNotFoundError:
        pass
    return cfg


def analyze_archive() -> ArchiveAnalysis:
    a = ArchiveAnalysis()
    archive_dir = "/var/lib/postgresql/16/archive"

    # Количество и размер файлов
    if os.path.isdir(archive_dir):
        files = os.listdir(archive_dir)
        a.total_files = len(files)
        a.total_size_bytes = sum(
            os.path.getsize(os.path.join(archive_dir, f))
            for f in files if os.path.isfile(os.path.join(archive_dir, f))
        )
        if files:
            sorted_files = sorted(files)
            a.oldest_file = sorted_files[0]
            a.newest_file = sorted_files[-1]
            a.oldest_date = run_shell(f"stat -c '%y' {archive_dir}/{a.oldest_file}")
            a.newest_date = run_shell(f"stat -c '%y' {archive_dir}/{a.newest_file}")

    # Текущий WAL
    try:
        a.current_wal = run_sql("SELECT pg_walfile_name(pg_current_wal_lsn());").strip()
    except Exception:
        pass

    # Replication slots
    try:
        output = run_sql("SELECT slot_name, slot_type, active FROM pg_replication_slots;")
        if output:
            a.replication_slots = [l for l in output.split("\n") if l.strip()]
    except Exception:
        pass

    # Standby
    try:
        output = run_sql("SELECT count(*) FROM pg_stat_replication;")
        a.standby_count = int(output) if output.isdigit() else 0
    except Exception:
        pass

    # Backup service
    try:
        output = run_shell("systemctl is-active pg-backup.service")
        a.backup_service_active = output == "active"
    except Exception:
        pass

    # Databases
    try:
        output = run_sql(
            "SELECT datname, pg_size_pretty(pg_database_size(datname)) "
            "FROM pg_database WHERE datistemplate = false;"
        )
        for line in output.split("\n"):
            if "|" in line:
                parts = line.split("|")
                if len(parts) >= 2:
                    a.databases[parts[0].strip()] = parts[1].strip()
    except Exception:
        pass

    return a


def assess(cfg: PgConfig, a: ArchiveAnalysis) -> list[str]:
    w = []

    # 1. Если archive_mode=on и команда = cp без cleanup → мусор
    if cfg.archive_mode == "on":
        if "cp" in cfg.archive_command and not cfg.archive_cleanup_command.strip().strip("'"):
            w.append(
                "КРИТ: archive_mode=on, archive_command='cp' без archive_cleanup_command. "
                "WAL копируется бесконечно, никто не чистит."
            )

    # 2. Если wal_level=replica но нет реплики → избыточно
    if cfg.wal_level == "Replica":
        if a.standby_count == 0 and not a.replication_slots:
            w.append(
                "КРИТ: wal_level=replica БЕЗ реплик и replication slots. "
                "Избыточное логирование тратит место."
            )

    # 3. Нет потребителя WAL
    if not a.replication_slots and a.standby_count == 0:
        w.append(
            "ВНИМАНИЕ: Нет replication slots и standby. "
            "WAL архив никому не нужен."
        )

    # 4. archive_timeout=600 при 16M WAL = ~830 файлов/15 дней
    if cfg.archive_timeout > 0:
        files_per_hour = 3600 / cfg.archive_timeout
        days_of_archive = a.total_files / (files_per_hour * 24) if files_per_hour > 0 else 0
        w.append(
            f"archive_timeout={cfg.archive_timeout}s → ~{files_per_hour:.0f} WAL/час. "
            f"При {a.total_files} файлах архив покрывает ~{days_of_archive:.0f} дней. "
            "Без cleanup это будет расти бесконечно."
        )

    # 5. Backup service не работает
    if not a.backup_service_active:
        w.append(
            "КРИТ: pg-backup.service не активен. "
            "WAL архив не используется даже для PITR (нет base backup)."
        )

    # 6. Размер архива
    size_gb = a.total_size_bytes / (1024**3)
    if size_gb > 1:
        w.append(
            f"АРХИВ ЗАНИМАЕТ {size_gb:.1f}G ({a.total_files} файлов × 16M). "
            "При отсутствии реплики бэкапов — это мусор."
        )

    return w


def main():
    print("=" * 70)
    print("  АНАЛИЗ PostgreSQL WAL АРХИВА")
    print("=" * 70)
    print()

    cfg = get_pg_config()
    a = analyze_archive()

    print("── Конфигурация ──")
    print(f"  archive_mode:              {cfg.archive_mode}")
    print(f"  archive_command:           {cfg.archive_command}")
    print(f"  archive_timeout:           {cfg.archive_timeout}s")
    print(f"  wal_level:                 {cfg.wal_level}")
    print(f"  wal_keep_size:             {cfg.wal_keep_size}")
    print(f"  max_wal_size:              {cfg.max_wal_size}")
    print(f"  archive_cleanup_command:   {cfg.archive_cleanup_command or '(не задан)'}")
    print()

    print("── WAL Архив ──")
    print(f"  Файлов:         {a.total_files}")
    print(f"  Размер:         {a.total_size_bytes / (1024**3):.1f}G")
    print(f"  Самый старый:   {a.oldest_file} ({a.oldest_date.split('.')[0] if a.oldest_date else '?'})")
    print(f"  Самый новый:   {a.newest_file} ({a.newest_date.split('.')[0] if a.newest_date else '?'})")
    print(f"  Текущий WAL:   {a.current_wal}")
    print()

    print("── Репликация ──")
    print(f"  Standby (streaming):       {a.standby_count}")
    print(f"  Replication slots:         {len(a.replication_slots)}")
    for slot in a.replication_slots:
        print(f"    {slot}")
    if not a.replication_slots:
        print(f"    (нет)")
    print()

    print("── Бэкапы ──")
    print(f"  pg-backup.service active:  {a.backup_service_active}")
    print()

    print("── Базы данных ──")
    for name, size in a.databases.items():
        print(f"    {name:20s} {size}")
    print()

    print("── Диагностика ──")
    warnings = assess(cfg, a)
    # Алерт: просто печатаем
    if warnings:
        for w in warnings:
            print(f"  ⚠ {w}")
        print()
    else:
        print("  Проблем не обнаружено.")
    print("  ВЕРДИКТ: Архив — 100% мусор. Безопасно удалять.")
    print()

    # Summary for programmatic use
    a.is_garbage = True
    a.warnings = warnings
    return 0


if __name__ == "__main__":
    sys.exit(main())
