"""Canonical monitoring thresholds for lab-monitoring.

SINGLE SOURCE OF TRUTH for alert thresholds. Used by:
  - src/lab_monitoring/alert_rules.py  (infra alerts)
  - bin/lab-monitor.py (Доминики ЛабМонитор) — imports with fallback

Both monitors MUST read thresholds from here so they never diverge
(see DDP 2026-07-13: two monitors had different disk thresholds → contradiction).

Sources are noted inline:
  - disk 88/90 : ADR-039 disk rotation policy (warn before it's too late)
  - mem 85/95  : Linux uses cache/buffers; available RAM matters more than used
  - load 2x/4x : research — load > cores = waiting; 2x = serious, 4x = critical
  - swap 10/30 : any notable swap on a ~8G RAM server is a warning
  - inodes     : can exhaust even with free space (many small files)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AlertConfig:
    """Thresholds for alerting. Tuned for a single-server lab setup."""

    # Disk: alert before it's too late. 88% = warning, 90% = critical
    disk_warn_pct: float = 88.0
    disk_critical_pct: float = 90.0

    # Memory: Linux uses cache/buffers, so available RAM matters more than used
    # Warning at 85% used, critical at 95%
    mem_warn_pct: float = 85.0
    mem_critical_pct: float = 95.0

    # Load: alert when load exceeds Nx CPU cores
    load_warn_multiplier: float = 2.0
    load_critical_multiplier: float = 4.0

    # Swap: any notable swap usage on a server with ~8G RAM is a warning
    swap_warn_pct: float = 10.0
    swap_critical_pct: float = 30.0

    # Inodes: can run out even with free disk space (many small files)
    inode_warn_pct: float = 70.0
    inode_critical_pct: float = 85.0

    # PostgreSQL: connection failures are critical
    pg_conn_critical: bool = True

    # OLLAMA: API unavailable (blocks LLM features)
    ollama_critical: bool = True

    # Journal: high error rate in last hour
    journal_err_warn_per_min: float = 10.0
    journal_err_critical_per_min: float = 50.0
