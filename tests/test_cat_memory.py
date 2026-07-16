"""Mock-тесты категории 4 (Память/поиск) — канон ЗавЛаба 16.07.

Реальный сем-поиск = memory-gateway MCP (stdio, OpenClaw-managed).
Отчёт показывает ТОЛЬКО сигнал о memory-gateway (жив/мёртв).
Детали про мёртвые стеки (ONNX/FAISS/lab_search) и закрытый reindex
— в коде/комментариях и документации, НЕ в ежечасном отчёте (шум).
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
    """memory-gateway бэкенд жив → ok=True, отчёт чистый (без шума о мёртвых стеках)."""
    with patch.object(M, "_memory_gateway_ok", return_value=True):
        ok, detail, details = _call()
    assert ok is True
    assert "memory-gateway MCP (semantic+lexical)=РАБОТАЕТ" in detail
    # совет: действий нет
    advice = M.ADVICE[4](True, "Память/поиск", detail)
    assert "действий нет" in advice
    # детали: только memory-gateway + lexical.db
    assert any("memory-gateway MCP — единый рабочий сем-поиск" in d for d in details)
    assert any("lexical.db (лексич. слой memory-gateway)" in d for d in details)
    # НЕТ шума про мёртвые стеки в отчёте
    joined = detail + " ".join(details)
    assert "ONNX" not in joined
    assert "reindex" not in joined


def test_memory_gateway_down_is_red():
    """memory-gateway бэкенд мёртв → ok=False, совет проверить lexical.db + run.py."""
    with patch.object(M, "_memory_gateway_ok", return_value=False):
        ok, detail, details = _call()
    assert ok is False
    assert "memory-gateway MCP (semantic+lexical)=FAIL" in detail
    advice = M.ADVICE[4](False, "Память/поиск", detail)
    assert "lexical.db" in advice
    assert "mcp-tools/memory-gateway/run.py" in advice


def test_advice_ok_no_action():
    """OK → совет 'действий нет'."""
    detail = "memory-gateway MCP (semantic+lexical)=РАБОТАЕТ"
    advice = M.ADVICE[4](True, "Память/поиск", detail)
    assert "действий нет" in advice


def test_detail_contains_only_memory_gateway_signal():
    """Detail содержит только сигнал о memory-gateway, без шума мёртвых стеков."""
    with patch.object(M, "_memory_gateway_ok", return_value=True):
        ok, detail, details = _call()
    assert "memory-gateway MCP (semantic+lexical)" in detail
    joined = detail + " " + " ".join(details)
    # канон: мёртвые стеки НЕ светятся в отчёте
    assert "ONNX" not in joined
    assert "FAISS" not in joined
    assert "lab_search" not in joined
    assert "reindex" not in joined
