"""Тесты для мониторинга бэкапов PostgreSQL."""
from __future__ import annotations

import asyncio
import tempfile
import os
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from lab_monitoring.backup_monitor import (
    check_pg_backups, BackupCheckResult, BackupInfo,
    _find_backup_dirs, _scan_backups, BACKUP_DIRS,
)


class TestBackupMonitor:

    def test_find_backup_dirs_none(self):
        """Нет директорий бэкапов."""
        result = asyncio.get_event_loop().run_until_complete(_find_backup_dirs())
        # Может быть пустым или содержать /var/backups/postgresql если создан
        assert isinstance(result, list)

    def test_find_backup_dirs_with_var(self):
        """/var/backups/postgresql должна находиться если существует."""
        result = asyncio.get_event_loop().run_until_complete(_find_backup_dirs())
        p = Path("/var/backups/postgresql")
        if p.exists():
            assert p in result

    def test_scan_backups_empty_dir(self, tmp_path):
        """Пустая директория."""
        result = asyncio.get_event_loop().run_until_complete(_scan_backups(tmp_path))
        assert result == []

    def test_scan_backups_with_files(self, tmp_path):
        """Сканирование директории с файлами бэкапов."""
        # Создаём тестовые файлы
        backup_file = tmp_path / "snablab_20250523.sql.gz"
        backup_file.write_text("fake backup data")
        (tmp_path / "readme.txt").write_text("not a backup")

        result = asyncio.get_event_loop().run_until_complete(_scan_backups(tmp_path))
        assert len(result) == 1
        assert result[0].database == "snablab"
        assert result[0].size_bytes > 0

    def test_scan_backups_vpn_bot(self, tmp_path):
        """Бэкап vpn_bot."""
        backup_file = tmp_path / "vpn_bot_20250523.dump"
        backup_file.write_text("fake")

        result = asyncio.get_event_loop().run_until_complete(_scan_backups(tmp_path))
        assert len(result) == 1
        assert result[0].database == "vpn_bot"

    def test_scan_backups_old_stale(self, tmp_path):
        """Старый бэкап получает статус stale."""
        backup_file = tmp_path / "snablab_old.sql.gz"
        backup_file.write_text("fake")

        # Устанавливаем mtime на 3 дня назад
        old_time = time.time() - 3 * 86400
        os.utime(backup_file, (old_time, old_time))

        result = asyncio.get_event_loop().run_until_complete(_scan_backups(tmp_path))
        assert len(result) == 1
        assert result[0].status == "stale"
        assert result[0].age_hours > 48

    def test_check_pg_backups_no_dir(self):
        """Бэкапов нет — warning."""
        with patch("lab_monitoring.backup_monitor._find_backup_dirs", return_value=[]):
            result = asyncio.get_event_loop().run_until_complete(check_pg_backups())
            assert result.status == "warning"


class TestBackupInfoModel:

    def test_backup_info_defaults(self):
        info = BackupInfo(database="test", path="/tmp/test.sql.gz")
        assert info.status == "ok"
        assert info.age_hours == -1
        assert info.size_bytes == 0

    def test_backup_check_result_defaults(self):
        result = BackupCheckResult()
        assert result.status == "ok"
        assert result.total_backups == 0
        assert result.backups == []

    def test_backup_check_result_to_dict(self):
        result = BackupCheckResult(
            status="ok",
            backup_dir="/var/backups",
            total_backups=2,
            total_size_bytes=1048576,
            last_backup_hours_ago=2.5,
        )
        d = result.to_dict()
        assert d["status"] == "ok"
        assert d["total_backups"] == 2
        assert d["total_size_mb"] == 1.0
        assert d["last_backup_hours_ago"] == 2.5
