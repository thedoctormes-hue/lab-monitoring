---
description: "lab-monitoring — README"
type: readme
last_reviewed: 2026-07-14
last_code_change: 2026-07-14
status: active
---

# 📊 Lab Monitoring

> **Владелец:** DoctorM&Ai | **Статус:** active

## Описание

Мониторинг состояния инфраструктуры лаборатории — проверка здоровья сервисов, баз данных, системных метрик. Собирает данные о дисках, памяти, CPU, Docker-контейнерах и состоянии systemd-сервисов, формирует JSON-отчёт с алертами.

## Проблема → Решение

**Проблема:** Ручная проверка состояния серверов и сервисов занимает время и подвержена человеческому фактору. Сбой в работе бэкапа или переполнение диска может остаматься незамеченным.

**Решение:** Автоматический сбор метрик и оценка по правилам алертов. Один запрос — полная картина состояния инфраструктуры с чёткими статусами OK / WARNING / CRITICAL.

## Стек технологий

- **Python 3.12+** — основной язык
- **asyncio** — асинхронный сбор метрик
- **loguru** — логирование
- **PostgreSQL** — проверка бэкапов
- **Docker** — мониторинг контейнеров
- **systemd** — отслеживание состояния сервисов
- **subprocess + /proc** — сбор системных метрик без внешних зависимостей

## Ключевые фичи

- **Проверка PostgreSQL-бэкапов** — мониторинг каталога бэкапов, подсчёт количества
- **Системные метрики** — диск, память, CPU через `/proc` и `os.statvfs`
- **Docker-мониторинг** — статус контейнеров через `docker ps`
- **Systemd-мониторинг** — обнаружение failed-сервисов
- **Правила алертов** — настраиваемые пороги CRITICAL/WARNING
- **JSON-отчёт** — струксированный вывод для интеграции с Alertmanager и скриптами
- **Exit code 0** — инструмент отчитывается через JSON, не блокирует CI

## Параметры алертов

Монитор (`bin/lab-monitor.py`) оценивает состояние по порогам из `src/lab_monitoring/thresholds.py`:

- **Диск:** `disk_warn_pct = 80`, `disk_crit_pct = 90` (норма < 80%).
- **Петли auto-restart:** `NRESTARTS_LIFETIME_WARN = 20` — сервис проблемный при `lifetime ≥ 20` перезапусков (или дельте ≥ `CRASH_LOOP_DELTA` за час).
- **ONNX-embedder (:8082):** токен `onnx_embedder=OK|FAIL` в категории [4]; при FAIL семантический поиск лабы не работает.
- **reindex-incremental.service:** детект через `is-failed` (не `is-active`); при FAILED без активного таймера — advice «restart service».
- **MONITOR_PORTS:** `[5432, 18789, 8086, 8087, 8888]` (порт 8710 orex удалён 2026-07-14 — ложный 🔴).

Запуск:

```bash
python3 bin/lab-monitor.py            # ежечасный отчёт (OpenClaw cron heartbeat-dominika)
python3 bin/lab-monitor.py --daily   # ежедневный (09:00 МСК, heartbeat-dominika-daily)
python3 bin/lab-monitor.py --selftest # self-check
```

## Структура проекта

```
lab-monitoring/
├── src/lab_monitoring/
│   ├── __main__.py        # Точка входа (asyncio)
│   ├── system_check.py    # Сбор системных метрик (диск, память, CPU, Docker, systemd)
│   ├── backup_monitor.py  # Проверка PostgreSQL-бэкапов
│   └── alert_rules.py     # Оценка по правилам алертов
├── tests/                 # Тесты
├── docs/                  # Документация
├── scripts/               # Вспомогательные скрипты
├── systemd/               # systemd unit-файлы
└── CHANGELOG.md           # История изменений
```

## Быстрый старт

```bash
# Установка
cd /root/LabDoctorM/projects/lab-monitoring
pip install -e .

# Запуск полной проверки
python3 -m lab_monitoring

# Запуск тестов
pytest tests/ -v
```

## Пример вывода

```json
{
  "timestamp": "2026-06-27T18:00:00+00:00",
  "backup_check": {
    "status": "ok",
    "backup_dir": "/var/backups/postgres",
    "total_backups": 7
  },
  "system_metrics": {
    "disk": { "path": "/", "used_percent": 45.2 },
    "memory": { "total_bytes": 16777216000, "used_percent": 32.1 }
  },
  "alerts": [],
  "summary": {
    "status": "ok",
    "critical_count": 0,
    "warning_count": 0,
    "total_alerts": 0
  }
}
```

## Статус проекта

**Active.** Базовый мониторинг работает. Планируется интеграция с Prometheus/Grafana и расширение набора проверок.

## Лицензия

Внутренний проект LabDoctorM.
