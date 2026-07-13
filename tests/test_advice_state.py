"""Тесты синергии порогов и гардов слоя реагирования (Фаза 2, DDP 2026-07-13)."""
import importlib.util
import os
from unittest.mock import patch


_MONITOR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "lab-monitor.py")
SPEC = importlib.util.spec_from_file_location("lab_monitor_ph2", _MONITOR_PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_thresholds_synced_from_shared():
    """Монитор должен брать диск-пороги из единого источника (thresholds.py)."""
    from lab_monitoring.thresholds import AlertConfig
    cfg = AlertConfig()
    assert M.THRESHOLDS["disk_warn_pct"] == int(cfg.disk_warn_pct)
    assert M.THRESHOLDS["disk_crit_pct"] == int(cfg.disk_critical_pct)


def test_advice_state_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "advice_state.json"
    monkeypatch.setattr(M, "ADVICE_STATE_FILE", str(p))
    payload = {"7": {"last_ts": 1.0, "count": 2, "cooldown_until": 999.0}}
    M.save_advice_state(payload)
    assert M.load_advice_state() == payload


def test_ping_healthchecks_called_when_env_set(monkeypatch):
    monkeypatch.setenv("HEALTHCHECKS_URL", "https://hc.example/ping")
    with patch("urllib.request.urlopen") as mock:
        M.ping_healthchecks()
        mock.assert_called_once()


def test_ping_healthchecks_skipped_when_no_env(monkeypatch):
    monkeypatch.delenv("HEALTHCHECKS_URL", raising=False)
    with patch("urllib.request.urlopen") as mock:
        M.ping_healthchecks()
        mock.assert_not_called()


def test_notify_fallback_called_when_env_set(monkeypatch):
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hook.example/x")
    with patch("urllib.request.urlopen") as mock:
        M.notify_fallback("привет")
        mock.assert_called_once()


def test_notify_fallback_skipped_when_no_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)
    with patch("urllib.request.urlopen") as mock:
        M.notify_fallback("привет")
        mock.assert_not_called()
