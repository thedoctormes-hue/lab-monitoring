"""Mock-тесты категории 4 (Память/поиск) — канон ЗавЛаба 16.07.

Реальный сем-поиск = memory-gateway MCP (stdio, OpenClaw-managed).
ONNX/FAISS/lab_search.py — МЁРТВЫ (зона antcat), НЕ использовать.
reindex ЗАКРЫТ ЗавЛабом 16.07.
Проверяем живость memory-gateway: lexical.db + запускаемость сервера.
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


def test_memory_gateway_ok_is_green():
    """memory-gateway бэкенд жив → ok=True (lexical.db есть + run.py есть)."""
    with patch.object(M, "_memory_gateway_ok", return_value=True):
        ok, detail, details = _call()
    assert ok is True
    assert "memory-gateway MCP (semantic+lexical)=РАБОТАЕТ" in detail
    assert "ONNX/FAISS/lab_search=МЁРТВ" in detail
    assert "reindex=ЗАКРЫТ(16.07)" in detail
    # совет: действий нет
    advice = M.ADVICE[4](True, "Память/поиск", detail)
    assert "действий нет" in advice


def test_memory_gateway_down_is_red():
    """memory-gateway бэкенд мёртв (нет lexical.db или run.py) → ok=False."""
    with patch.object(M, "_memory_gateway_ok", return_value=False):
        ok, detail, details = _call()
    assert ok is False
    assert "memory-gateway MCP (semantic+lexical)=FAIL" in detail
    assert "ONNX/FAISS/lab_search=МЁРТВ" in detail
    assert "reindex=ЗАКРЫТ(16.07)" in detail
    # совет: проверить lexical.db и run.py
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "lexical.db" in advice
    assert "mcp-tools/memory-gateway/run.py" in advice
    assert "ONNX/FAISS/lab_search мертвы" in advice


def test_advice_ok_no_action():
    """OK → совет 'действий нет'."""
    detail = "memory-gateway MCP (semantic+lexical)=РАБОТАЕТ; ONNX/FAISS/lab_search=МЁРТВ (зона antcat, НЕ использовать); reindex=ЗАКРЫТ(16.07)"
    advice = M.ADVICE[4](True, "Память/поиск", detail)
    assert "действий нет" in advice


def test_detail_contains_canon_facts():
    """Detail содержит факты канона: ONNX мёртв, reindex закрыт, memory-gateway — рабочий."""
    with patch.object(M, "_memory_gateway_ok", return_value=True):
        ok, detail, details = _call()
    assert "МЁРТВ (зона antcat, НЕ использовать)" in detail
    assert "ЗАКРЫТ(16.07)" in detail
    assert "memory-gateway MCP (semantic+lexical)" in detail
    # out содержит правильные строки
    assert any("memory-gateway MCP — единый рабочий сем-поиск" in d for d in details)
    assert any("ONNX-embedder :8082 / FAISS / lab_search.py — МЁРТВЫ" in d for d in details)
    assert any("reindex-юниты ЗАКРЫТЫ ЗавЛабом 16.07" in d for d in details)
