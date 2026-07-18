"""Тесты B2 (red line #37): классификация web_fetch 404 и auto-restart как benign-with-context.

Ключевая идея (НЕ suppression): сигнал НЕ прячется и НЕ подавляется, но и НЕ
считается ошибкой доставки / аварией. Показывается с контекстом (URL, NRestarts).

Покрывает изменённые модули lab-monitor.py:
  - scan_gateway_log_errors_1h  → web_fetch 404 = benign (вне cnt, виден с URL)
  - cat_services            → Restart=always + низкий NRestarts = benign (НЕ проблема)
"""
import datetime
import importlib.util
import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

SPEC = importlib.util.spec_from_file_location(
    "lab_monitor",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "bin", "lab-monitor.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def _now_iso_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_web404_classified_benign(tmp_path, monkeypatch):
    """web_fetch 404 НЕ считается ошибкой (cnt), но виден benign-строкой с URL."""
    log = tmp_path / "openclaw-test.log"
    now_iso = _now_iso_utc()
    real_err = {
        "_meta": {"logLevelName": "ERROR", "date": now_iso},
        "message": "[tools] read failed: boom",
    }
    web404 = {
        "_meta": {"logLevelName": "ERROR", "date": now_iso},
        "message": ('[tools] web_fetch failed: Web fetch failed (404): SECURITY '
                    'raw_params={"url":"https://example.com/missing"}'),
    }
    log.write_text("\n".join(json.dumps(r) for r in (real_err, web404)))
    # детерминизм: сканируем только наш temp-лог
    monkeypatch.setattr(M, "glob", types.SimpleNamespace(glob=lambda p: [str(log)]))
    monkeypatch.setattr(M.os.path, "getmtime", lambda p: time.time())

    cnt, out = M.scan_gateway_log_errors_1h()
    joined = "\n".join(out)

    # только реальная ошибка (read) считается; 404 — benign, вне cnt
    assert cnt == 1, (cnt, out)
    # 404 виден benign-категорией с извлечённым URL
    assert "🌐 web 404" in joined, out
    assert "https://example.com/missing" in joined, out
    # 404 НЕ поднимает ВНИМАНИЕ как ошибка доставки
    assert "ВНИМАНИЕ" not in joined, out


def _mock_run_benign(unit="myunit.service"):
    def _run(cmd, **kw):
        r = types.SimpleNamespace(stdout="", returncode=0, stderr="")
        if "systemctl --state=failed" in cmd:
            r.stdout = ""
        elif "systemctl list-units" in cmd:
            r.stdout = f"{unit} loaded active running"
        elif "systemctl show" in cmd and "ExecStart" in cmd:
            r.stdout = ""
        elif "systemctl show" in cmd and "Restart" in cmd:
            r.stdout = "always"
        elif "systemctl show" in cmd and "NRestarts" in cmd:
            r.stdout = "NRestarts=0"
        elif "systemctl is-active" in cmd:
            r.stdout = "active"
        else:
            r.stdout = ""
        return r
    return _run


def test_auto_restart_benign(tmp_path, monkeypatch):
    """Restart=always + NRestarts=0 (низкий) → benign-строка, НЕ проблема/авария."""
    monkeypatch.setattr(M, "run", _mock_run_benign())
    monkeypatch.setattr(M, "load_services_state", lambda: {})
    monkeypatch.setattr(M, "save_services_state", lambda s: None)
    monkeypatch.setattr(M, "port_ok", lambda p: True)
    monkeypatch.setattr(M, "MONITOR_PORTS", [])

    ok, summary, details = M.cat_services()
    joined = "\n".join(details)

    assert ok is True, (summary, details)
    assert "auto-restart юниты (benign" in joined, details
    assert "Restart=always, NRestarts=0" in joined, details
    # НЕ классифицируется как авария/проблема
    assert "проблемы сервисов" not in joined, details
