"""Tests for the shared thresholds module (single source of truth)."""
from __future__ import annotations

from lab_monitoring.alert_rules import AlertConfig as AlertConfigFromRules
from lab_monitoring.thresholds import AlertConfig


def test_defaults_sourced():
    c = AlertConfig()
    assert c.disk_warn_pct == 80.0
    assert c.disk_critical_pct == 90.0
    assert c.mem_warn_pct == 85.0
    assert c.mem_critical_pct == 95.0
    assert c.load_warn_multiplier == 2.0
    assert c.load_critical_multiplier == 4.0
    assert c.swap_warn_pct == 10.0
    assert c.swap_critical_pct == 30.0


def test_alert_rules_reports_shared_config():
    # alert_rules must re-export the SAME dataclass so both monitors align
    assert AlertConfigFromRules is AlertConfig


def test_config_override():
    c = AlertConfig(disk_warn_pct=70.0)
    assert c.disk_warn_pct == 70.0
    assert c.disk_critical_pct == 90.0  # unchanged
