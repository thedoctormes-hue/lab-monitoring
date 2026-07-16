"""Mock-тесты категории 4 (Память/поиск) — канон ЗавЛаба 16.07.

Монитор показывает: живость сервера (container Up + векторы), синк за 1ч
(инкрементальные/полные/ошибки) и корректность юнитов
(alm-sync-incremental + статус последнего полного rebuild по journalctl).
Отчёт — только сигнал, без шума о мёртвых стеках (ONNX/FAISS/lab_search).
"""
import importlib.util
import os
from unittest.mock import patch

_MONITOR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "lab-monitor.py")
SPEC = importlib.util.spec_from_file_location("lab_monitor_catmem", _MONITOR_PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def _call():
    return M.cat_memory()


# базовый набор моков: убираем реальные journalctl/systemctl/файловые вызовы
_BASE = dict(
    _memory_gateway_ok=patch.object(M, "_memory_gateway_ok", return_value=True),
    _read_control_log=patch.object(M, "_read_control_log", return_value={"container": "Up", "vc": "8694", "incr_fails": "0", "rebuild_last": "ok"}),
    _count_sync_last_hour=patch.object(M, "_count_sync_last_hour", return_value=(12, 0, 0, 29)),
    _unit_failed=patch.object(M, "_unit_failed", return_value=False),
    _rebuild_last_failed=patch.object(M, "_rebuild_last_failed", return_value=False),
)


def test_server_up_clean():
    """Сервер Up, синк идёт без ошибок, юниты ок → OK."""
    with _BASE["_memory_gateway_ok"], _BASE["_read_control_log"], \
         _BASE["_count_sync_last_hour"], _BASE["_unit_failed"], _BASE["_rebuild_last_failed"]:
        ok, detail, details = _call()
    assert ok is True
    assert "memory-gateway MCP OK" in detail
    joined = detail + " " + " ".join(details)
    assert "сервер: Up" in joined
    assert "инкрементальных 12" in joined
    assert "ошибок 0" in joined
    assert "29 workspaces" in joined
    assert "ONNX" not in joined and "FAISS" not in joined
    # rebuild ок → строки про failed нет
    assert not any("последний полный reindex: failed" in d for d in details)


def test_server_down_container():
    """Контейнер Down → сервер DOWN → СБОЙ."""
    with patch.object(M, "_memory_gateway_ok", return_value=True), \
         patch.object(M, "_read_control_log", return_value={"container": "Down", "vc": "8694"}), \
         patch.object(M, "_count_sync_last_hour", return_value=(12, 0, 0, 29)), \
         patch.object(M, "_unit_failed", return_value=False), \
         patch.object(M, "_rebuild_last_failed", return_value=False):
        ok, detail, details = _call()
    assert ok is False
    assert "memory-gateway MCP СБОЙ" in detail
    assert "сервер: DOWN" in " ".join(details)


def test_unit_failed_shows_in_report():
    """Юнит alm-sync-incremental failed → СБОЙ + строка в отчёте."""
    with patch.object(M, "_memory_gateway_ok", return_value=True), \
         patch.object(M, "_read_control_log", return_value={"container": "Up", "vc": "8694", "incr_fails": "0", "rebuild_last": "ok"}), \
         patch.object(M, "_count_sync_last_hour", return_value=(12, 0, 0, 29)), \
         patch.object(M, "_unit_failed", return_value=True), \
         patch.object(M, "_rebuild_last_failed", return_value=False):
        ok, detail, details = _call()
    assert ok is False
    assert any("alm-sync-incremental.service: failed" in d for d in details)


def test_sync_errors_counted():
    """Ошибки синка за час суммируются (failed из sync.log + incr_fails из control_log)."""
    with patch.object(M, "_memory_gateway_ok", return_value=True), \
         patch.object(M, "_read_control_log", return_value={"container": "Up", "vc": "8694", "incr_fails": "2", "rebuild_last": "ok"}), \
         patch.object(M, "_count_sync_last_hour", return_value=(12, 3, 0, 29)), \
         patch.object(M, "_unit_failed", return_value=False), \
         patch.object(M, "_rebuild_last_failed", return_value=False):
        ok, detail, details = _call()
    joined = " ".join(details)
    assert "ошибок 5" in joined  # 3 (sync.log) + 2 (incr_fails)


def test_rebuild_failed_flag():
    """_rebuild_last_failed() → строка-напоминание про RUL-009."""
    with patch.object(M, "_memory_gateway_ok", return_value=True), \
         patch.object(M, "_read_control_log", return_value={"container": "Up", "vc": "8694", "incr_fails": "0"}), \
         patch.object(M, "_count_sync_last_hour", return_value=(12, 0, 0, 29)), \
         patch.object(M, "_unit_failed", return_value=False), \
         patch.object(M, "_rebuild_last_failed", return_value=True):
        ok, detail, details = _call()
    assert any("последний полный reindex: failed" in d for d in details)


def test_rebuild_ok_no_flag():
    """rebuild успешен → строки про failed НЕТ."""
    with patch.object(M, "_memory_gateway_ok", return_value=True), \
         patch.object(M, "_read_control_log", return_value={"container": "Up", "vc": "8694", "incr_fails": "0"}), \
         patch.object(M, "_count_sync_last_hour", return_value=(12, 0, 0, 29)), \
         patch.object(M, "_unit_failed", return_value=False), \
         patch.object(M, "_rebuild_last_failed", return_value=False):
        ok, detail, details = _call()
    assert not any("последний полный reindex: failed" in d for d in details)


def test_real_vc_reads_control_log():
    """_real_vc берёт vc из control_log."""
    with patch.object(M, "_read_control_log", return_value={"vc": "8694"}):
        assert M._real_vc() == 8694
    with patch.object(M, "_read_control_log", return_value=None):
        assert M._real_vc() == 0
