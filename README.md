---
description: "lab-monitoring — README"
type: readme
last_reviewed: 2026-06-21
last_code_change: 2026-06-21
status: active
---

# Lab Monitoring

> **Владелец:** DoctorM&Ai | **Статус:** active

## Описание

Мониторинг состояния лаборатории — сервисы, агенты, проекты. Проверка здоровья инфраструктуры.

## Быстрый старт

### Требования
- Python 3.12+

### Установка
```bash
cd /root/LabDoctorM/projects/lab-monitoring
pip install -e .
```

### Запуск
```bash
python -m lab_monitoring
```

## Архитектура

**Стек:** Python, pytest, systemd

## Разработка

```bash
# Тесты
pytest tests/ -v
```

## Документация

- [API](docs/API.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Деплой](docs/DEPLOY.md)
- [ADR](docs/ADR/)
