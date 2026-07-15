"""Alert rules engine for lab-monitoring.
Evaluates collected metrics against thresholds and returns actionable alerts.
Designed to minimize noise — only alerts on conditions that require human action.

Best practices applied from research (2025-2026):
- Tiered severity: INFO → WARNING → CRITICAL
- Threshold-based, not anomaly-based (single server, predictable workloads)
- Alert on symptoms that affect user experience, not raw metrics
- Avoid flapping: use sustained conditions, not point-in-time spikes
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

from lab_monitoring.thresholds import AlertConfig


@dataclass
class Alert:
    severity: str      # INFO, WARNING, CRITICAL
    source: str        # subsystem that triggered
    message: str       # human-readable description
    value: float = 0   # current value
    threshold: float = 0  # threshold that was exceeded

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "source": self.source,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
        }


# Thresholds live in a single shared module so that src/lab_monitoring and
# bin/lab-monitor.py (Доминики ЛабМонитор) never diverge on numbers.
# (AlertConfig is imported at the top of this file.)


def get_cpu_count() -> int:
    """Return number of CPU cores."""
    try:
        import os
        return os.cpu_count() or 2
    except Exception:
        return 2


def evaluate_disk(disk: dict, config: AlertConfig) -> Optional[Alert]:
    """Check disk usage against thresholds."""
    pct = disk.get("used_percent", 0)
    path = disk.get("path", "/")
    if pct >= config.disk_critical_pct:
        return Alert("CRITICAL", "disk",
                     f"Диск {path} заполнен на {pct}% — нужна очистка",
                     pct, config.disk_critical_pct)
    elif pct >= config.disk_warn_pct:
        return Alert("WARNING", "disk",
                     f"Диск {path} заполнен на {pct}% — следим",
                     pct, config.disk_warn_pct)
    return None


def evaluate_memory(memory: dict, config: AlertConfig) -> Optional[Alert]:
    """Check memory usage against thresholds."""
    pct = memory.get("used_percent", 0)
    if pct >= config.mem_critical_pct:
        return Alert("CRITICAL", "memory",
                     f"RAM заполнена на {pct}% — возможны OOM kills",
                     pct, config.mem_critical_pct)
    elif pct >= config.mem_warn_pct:
        return Alert("WARNING", "memory",
                     f"RAM заполнена на {pct}%",
                     pct, config.mem_warn_pct)
    return None


def evaluate_load(load: list, config: AlertConfig) -> Optional[Alert]:
    """Check CPU load against thresholds based on core count."""
    if not load:
        return None
    cores = get_cpu_count()
    one_min = load[0]
    warn_threshold = cores * config.load_warn_multiplier
    critical_threshold = cores * config.load_critical_multiplier
    if one_min >= critical_threshold:
        return Alert("CRITICAL", "load",
                     f"Load {one_min:.2f} при {cores} ядрах — возможна деградация",
                     one_min, critical_threshold)
    elif one_min >= warn_threshold:
        return Alert("WARNING", "load",
                     f"Load {one_min:.2f} при {cores} ядрах — повышенная нагрузка",
                     one_min, warn_threshold)
    return None


def evaluate_docker_containers(containers: List[dict], config: AlertConfig) -> List[Alert]:
    """Check Docker container health.
    Alert on: unhealthy status, or containers that crash-loop.
    """
    alerts = []
    for c in containers:
        status = c.get("status", "").lower()
        name = c.get("name", "unknown")
        if "unhealthy" in status:
            alerts.append(Alert("CRITICAL", "docker",
                                f"Контейнер {name} в состоянии unhealthy",
                                0, 0))
        elif "exited" in status or "dead" in status:
            alerts.append(Alert("CRITICAL", "docker",
                                f"Контейнер {name} остановлен ({status})",
                                0, 0))
    return alerts


def evaluate_failed_services(services: List[str], config: AlertConfig) -> List[Alert]:
    """Check failed systemd services.
    Production-critical services get CRITICAL severity.
    Others get WARNING.
    """
    critical_services = {
        "nginx.service", "postgresql.service", "docker.service",
        "stenographerobot.service", "myrmex-control.service",
        "lab-vault.service", "doctor-m-bot.service", "maildaemonrobot.service",
        "autoexpert.service", "consilium.service",
    }
    alerts = []
    for svc in services:
        if svc in critical_services:
            alerts.append(Alert("CRITICAL", "systemd",
                                f"Критический сервис {svc} упал",
                                0, 0))
        else:
            alerts.append(Alert("WARNING", "systemd",
                                f"Сервис {svc} в состоянии failed",
                                0, 0))
    return alerts


def evaluate_swap(memory: dict, config: AlertConfig) -> Optional[Alert]:
    """Check swap usage — notable swap on a 7.8G RAM server is a warning."""
    # Parse from /proc/meminfo if available
    swap_total = memory.get("swap_total_bytes", 0)
    swap_used_bytes = memory.get("swap_used_bytes", 0)
    if swap_total == 0:
        return None
    swap_pct = (swap_used_bytes / swap_total) * 100
    if swap_pct >= config.swap_critical_pct:
        return Alert("CRITICAL", "memory",
                     f"Swap заполнен на {swap_pct:.1f}% — возможна деградация",
                     swap_pct, config.swap_critical_pct)
    elif swap_pct >= config.swap_warn_pct:
        return Alert("WARNING", "memory",
                     f"Swap заполнен на {swap_pct:.1f}%",
                     swap_pct, config.swap_warn_pct)
    return None


def evaluate_all(metrics: dict, config: AlertConfig = None) -> List[Alert]:
    """Run all alert rules against collected metrics.
    Returns list of active alerts (empty = all clear).
    """
    if config is None:
        config = AlertConfig()
    alerts = []

    # Disk
    if "disk" in metrics:
        result = evaluate_disk(metrics["disk"], config)
        if result:
            alerts.append(result)

    # Memory
    if "memory" in metrics:
        result = evaluate_memory(metrics["memory"], config)
        if result:
            alerts.append(result)

    # Swap
    if "memory" in metrics:
        result = evaluate_swap(metrics["memory"], config)
        if result:
            alerts.append(result)

    # Load
    if "load" in metrics:
        result = evaluate_load(metrics["load"], config)
        if result:
            alerts.append(result)

    # Docker
    if "docker" in metrics:
        docker_alerts = evaluate_docker_containers(metrics["docker"], config)
        alerts.extend(docker_alerts)

    # Systemd
    if "failed_services" in metrics:
        svc_alerts = evaluate_failed_services(metrics["failed_services"], config)
        alerts.extend(svc_alerts)

    return alerts


def alerts_to_json(alerts: List[Alert]) -> str:
    """Serialize alerts list to JSON."""
    return json.dumps([a.to_dict() for a in alerts], indent=2, ensure_ascii=False)
