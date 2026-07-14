"""Mock-тесты на ROOT A (ONNX health) и ROOT B (reindex service) категории 4.

Проверяют, что диагностика опирается на структурированный сигнал
`onnx_available` (а не на TCP-liveness порта) и на `is-failed` reindex-сервиса
(а не на `is-active` таймера). Без реальных subprocess-вызовов.
"""
import importlib.util
import json
import os
from unittest.mock import patch

_MONITOR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "lab-monitor.py")
SPEC = importlib.util.spec_from_file_location("lab_monitor_catmem", _MONITOR_PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class _Proc:
    def __init__(self, stdout=""):
        self.stdout = stdout


def _fake_run(cmd, **kw):
    if "lab_search.py health" in cmd:
        return _Proc(stdout=json.dumps({
            "faiss_loaded": True,
            "onnx_available": _fake_run.onnx_available,
            "vectors": 37596,
        }))
    if "is-failed reindex-incremental.service" in cmd:
        return _Proc(stdout="failed" if _fake_run.reindex_failed else "inactive")
    if "is-active reindex" in cmd:
        return _Proc(stdout="active" if _fake_run.reindex_active else "inactive")
    return _Proc(stdout="")


def _set(onnx_available, reindex_failed=False, reindex_active=False):
    _fake_run.onnx_available = onnx_available
    _fake_run.reindex_failed = reindex_failed
    _fake_run.reindex_active = reindex_active


def _call():
    return M.cat_memory()


def test_root_a_onnx_embedder_down_is_red():
    _set(onnx_available=False)
    with patch.object(M, "run", _fake_run), patch.object(M, "port_ok", lambda p: True):
        ok, detail, details = _call()
    assert ok is False
    assert "onnx_embedder=FAIL" in detail
    # старое поведение (TCP-only) писало бы "ONNX :8082 ok" — этого быть не должно
    assert "ONNX :8082 ok" not in detail


def test_root_a_onnx_embedder_ok_is_green():
    _set(onnx_available=True)
    with patch.object(M, "run", _fake_run), patch.object(M, "port_ok", lambda p: True):
        ok, detail, details = _call()
    assert ok is True
    assert "onnx_embedder=OK" in detail


def test_root_b_reindex_service_failed_is_red():
    _set(onnx_available=True, reindex_failed=True)
    with patch.object(M, "run", _fake_run), patch.object(M, "port_ok", lambda p: True):
        ok, detail, details = _call()
    assert ok is False
    assert "reindex_service=failed" in detail


def test_root_a_advice_routes_to_onnx_not_reindex():
    # порт жив (up), но эмбеддер мёртв → совет чинить ONNX, НЕ reindex
    detail = ("ONNX :8082 up (onnx_embedder=FAIL); lab_search vectors=37596 FAIL; "
              "reindex_timer=off; reindex_service=ok")
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "ONNX embedder FAIL" in advice
    assert "запусти reindex" not in advice


def test_root_b_advice_routes_to_restart_when_service_failed():
    detail = ("ONNX :8082 up (onnx_embedder=OK); lab_search vectors=37596 ok; "
              "reindex_timer=off; reindex_service=failed")
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "systemctl restart reindex-incremental.service" in advice
