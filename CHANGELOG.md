---
description: "lab-monitoring — история изменений"
type: changelog
last_reviewed: 2026-06-21
last_code_change: 2026-06-21
status: active
---

# Changelog

## [Unreleased]

- Создан базовый CHANGELOG.

## [2026-07-14] — Точная диагностика + чистка ложных тревог

- **ROOT A** (cat[4]): парсинг `onnx_available` → токен `onnx_embedder=OK|FAIL`, advice-роутинг при сбое ONNX-embedder (:8082).
- **ROOT B** (cat[9]): `reindex-incremental.service` FAILED детектится через `is-failed` (не `is-active`) + гвард `reindex_timer=active`.
- **ROOT C** (cat[9]): `NRESTARTS_LIFETIME_WARN = 20`, вывод `(дельта +X/час; lifetime Y)` — видны петли auto-restart.
- **ROOT D** (cat[5]): пороги диска `80/90` (ADR-039).
- **Chore**: порт `8710` (orex, остановлен 14.07) удалён из `MONITOR_PORTS` (ложный 🔴).
- Тесты: `+5 test_cat_memory.py`, `pytest` 81 passed; `--selftest` live обе стороны.
- Коммиты: `4f8c441`, `23b8025` (запушены в origin/main).
