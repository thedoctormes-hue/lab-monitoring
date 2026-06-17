"""System health checks for lab-monitoring.
Collects disk usage, memory usage, CPU load, Docker container statuses, and failed systemd services.
All functions use only the Python standard library + subprocess (no external deps).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List


def _disk_usage(path: str = "/") -> Dict[str, str]:
    """Return disk usage for given path using os.statvfs.
    Returns a dict with total, used, free (bytes) and percent usage.
    """
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    used = total - free
    percent = round(used / total * 100, 1) if total else 0
    return {
        "path": path,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": percent,
    }


def _memory_info() -> Dict[str, str]:
    """Parse /proc/meminfo to get total and available memory (kB).
    Returns dict with total, available, used and percent.
    """
    meminfo = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            parts = line.split(":")
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            val = parts[1].strip().split()[0]
            meminfo[key] = int(val)  # kB
    total = meminfo.get("MemTotal", 0) * 1024
    available = meminfo.get("MemAvailable", 0) * 1024
    used = total - available
    percent = round(used / total * 100, 1) if total else 0
    swap_total = meminfo.get("SwapTotal", 0) * 1024
    swap_free = meminfo.get("SwapFree", 0) * 1024
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "used_percent": percent,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free,
    }


def _load_average() -> List[float]:
    """Return the three load average numbers from /proc/loadavg."""
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            parts = f.read().split()[:3]
            return [float(p) for p in parts]
    except Exception:
        return []


def _docker_status() -> List[Dict[str, str]]:
    """Return list of Docker containers (name and status) via `docker ps`.
    If docker command fails, return empty list.
    """
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}||{{.Status}}"],
            text=True,
            timeout=5,
        )
        containers = []
        for line in out.strip().splitlines():
            if not line:
                continue
            name, status = line.split("||", 1)
            containers.append({"name": name, "status": status})
        return containers
    except Exception:
        return []


def _failed_systemd_services() -> List[str]:
    """Return list of failed systemd services via `systemctl`.
    Uses `systemctl list-units --type=service --state=failed --no-legend --no-pager`.
    Filters out non-service lines (e.g. legend, bullet markers like '●').
    """
    try:
        out = subprocess.check_output(
            ["systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--no-pager"],
            text=True,
            timeout=5,
        )
        services = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # First token is the unit name; skip bullet markers and non-.service entries
            unit = line.split()[0]
            if unit.startswith("\u25cf") or unit.startswith("●"):
                # Bullet marker — take second token if present
                parts = line.split()
                unit = parts[1] if len(parts) > 1 else ""
            if unit and unit.endswith(".service"):
                services.append(unit)
        return services
    except Exception:
        return []


def collect_system_metrics() -> Dict[str, object]:
    """Collect all system metrics and return as dict.
    Keys: disk, memory, load, docker, failed_services.
    """
    return {
        "disk": _disk_usage("/"),
        "memory": _memory_info(),
        "load": _load_average(),
        "docker": _docker_status(),
        "failed_services": _failed_systemd_services(),
    }


def dump_metrics(metrics: Dict[str, object]) -> str:
    """Pretty‑print JSON for debugging/tests."""
    return json.dumps(metrics, indent=2, ensure_ascii=False)
