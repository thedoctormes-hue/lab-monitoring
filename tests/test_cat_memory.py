"""Mock-тесты категории 4 (Память/поиск) — новый deprecated-стэк.

Проверяют, что диагностика опирается на лексическую живость поиска,
а НЕ на ONNX health. ONNX/FAISS deprecated — алерты про него не красные.
Без реальных subprocess-вызовов.
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
    """Mock run() для тестов категории 4."""
    # systemctl is-active reindex...
    if "is-active reindex" in cmd:
        return _Proc(stdout="active" if _fake_run.reindex_active else "inactive")
    # systemctl is-failed reindex-incremental.service
    if "is-failed reindex-incremental.service" in cmd:
        return _Proc(stdout="failed" if _fake_run.reindex_failed else "inactive")
    # lab_search.py search "OpenClaw" --topN 1 --json
    if "lab_search.py search" in cmd:
        # Возвращаем JSON-список (пустой или с результатом) — успех лексического поиска
        return _Proc(stdout=json.dumps(_fake_run.lex_search_result))
    return _Proc(stdout="")


def _set(lex_ok=True, reindex_failed=False, reindex_active=False):
    """Настройка мока: работает ли лексический поиск + состояние reindex."""
    _fake_run.lex_search_result = [{"title": "x"}] if lex_ok else []
    _fake_run.reindex_failed = reindex_failed
    _fake_run.reindex_active = reindex_active


def _call():
    return M.cat_memory()


def test_lexical_search_ok_is_green():
    """Лексический поиск жив → ok=True (даже если ONNX down)."""
    _set(lex_ok=True)
    # 8082 не слушает → semantic OFF
    with patch.object(M, "run", _fake_run), patch.object(M, "port_ok", lambda p: p != 8082):
        ok, detail, details = _call()
    assert ok is True
    assert "lexical=ok" in detail
    assert "semantic(ONNX)=OFF (deprecated" in detail


def test_lexical_search_down_is_red():
    """Лексический поиск мёртв → ok=FAIL (реальная проблема)."""
    # stdout пустой → _lexical_search_works возвращает False
    _fake_run.lex_search_result = ""  # не JSON, пустой stdout
    with patch.object(M, "run", _fake_run), patch.object(M, "port_ok", lambda p: p != 8082):
        ok, detail, details = _call()
    assert ok is False
    assert "lexical=FAIL" in detail


def test_reindex_failed_no_longer_red_when_lex_ok():
    """reindex failed НЕ делает красным, если лексика жива (стэк deprecated)."""
    _set(lex_ok=True, reindex_failed=True)
    with patch.object(M, "run", _fake_run), patch.object(M, "port_ok", lambda p: True):
        ok, detail, details = _call()
    assert ok is True
    assert "reindex_service=failed" in detail
    # совет не должен требовать рестарт reindex
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "перезапускай" not in advice.lower() or "не перезапускай" in advice.lower()


def test_advice_lexical_fail_routes_to_search():
    """lexical=FAIL → совет проверить lab_search, НЕ ONNX."""
    detail = "поиск lexical=FAIL; semantic(ONNX)=OFF (deprecated — стэк меняется); reindex_timer=off; reindex_service=ok"
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "lab_search.py search" in advice
    assert "ONNX embedder" not in advice


def test_advice_reindex_ghost_failed_tells_not_to_restart():
    """reindex_service=failed (призрак) → совет НЕ рестартить, а ждать нового стэка."""
    detail = "поиск lexical=ok; semantic(ONNX)=OFF (deprecated — стэк меняется); reindex_timer=off; reindex_service=failed"
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "не перезапускай" in advice.lower() or "дождись" in advice.lower()
    assert "systemctl restart reindex" not in advice
