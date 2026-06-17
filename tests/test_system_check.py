"""Tests for lab_monitoring.system_check module."""
import os
import sys

import pytest

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab_monitoring.system_check import (
    _disk_usage,
    _memory_info,
    _load_average,
    _docker_status,
    _failed_systemd_services,
    collect_system_metrics,
    dump_metrics,
)


class TestDiskUsage:
    def test_returns_required_keys(self):
        result = _disk_usage("/")
        assert "path" in result
        assert "total_bytes" in result
        assert "used_bytes" in result
        assert "free_bytes" in result
        assert "used_percent" in result

    def test_path_is_root(self):
        result = _disk_usage("/")
        assert result["path"] == "/"

    def test_percent_in_valid_range(self):
        result = _disk_usage("/")
        assert 0 <= result["used_percent"] <= 100

    def test_bytes_are_positive(self):
        result = _disk_usage("/")
        assert result["total_bytes"] > 0
        assert result["used_bytes"] >= 0
        assert result["free_bytes"] > 0


class TestMemoryInfo:
    def test_returns_required_keys(self):
        result = _memory_info()
        assert "total_bytes" in result
        assert "available_bytes" in result
        assert "used_bytes" in result
        assert "used_percent" in result
        assert "swap_total_bytes" in result
        assert "swap_used_bytes" in result

    def test_total_is_positive(self):
        result = _memory_info()
        assert result["total_bytes"] > 0

    def test_percent_in_valid_range(self):
        result = _memory_info()
        assert 0 <= result["used_percent"] <= 100

    def test_used_equals_total_minus_available(self):
        result = _memory_info()
        assert result["used_bytes"] == result["total_bytes"] - result["available_bytes"]


class TestLoadAverage:
    def test_returns_three_values(self):
        result = _load_average()
        assert len(result) == 3

    def test_all_non_negative(self):
        result = _load_average()
        for v in result:
            assert v >= 0


class TestDockerStatus:
    def test_returns_list(self):
        result = _docker_status()
        assert isinstance(result, list)

    def test_containers_have_name_and_status(self):
        result = _docker_status()
        for c in result:
            assert "name" in c
            assert "status" in c


class TestFailedSystemdServices:
    def test_returns_list(self):
        result = _failed_systemd_services()
        assert isinstance(result, list)

    def test_entries_are_strings(self):
        result = _failed_systemd_services()
        for s in result:
            assert isinstance(s, str)


class TestCollectSystemMetrics:
    def test_returns_all_keys(self):
        result = collect_system_metrics()
        assert "disk" in result
        assert "memory" in result
        assert "load" in result
        assert "docker" in result
        assert "failed_services" in result


class TestDumpMetrics:
    def test_returns_valid_json(self):
        metrics = collect_system_metrics()
        json_str = dump_metrics(metrics)
        assert isinstance(json_str, str)
        # Should be parseable
        import json
        parsed = json.loads(json_str)
        assert "disk" in parsed
