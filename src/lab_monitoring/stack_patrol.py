"""Внешний патруль лаборатории (Stack Patrol) — разведка наружу.

Контракт (решение ЗавЛаба 2026-07-18, DDP):
  Проект lab-monitoring уже содержит этот слот внешнего монитора (он был
  orphan с 14.06). Ворон (raven) оживляет и доукомплектовывает его.

  ФАЗА 1 — «Что нового снаружи» (внешняя разведка, без LLM):
    - releases-watch по registry.json (state-diff: вышел ли новый релиз)
    - GitHub trending по СКОРОСТИ РОСТА звёзд (⭐/сут), не all-time топ
  ФАЗА 2 — «Актуализация нашего софта»:
    - для каждого python-окружения лабы (venv) -> pip list --outdated
    - склейка с CVE через OSV: какие апдейты РЕАЛЬНО чинят уязвимость
    - план обновления (сами pip install --upgrade НЕ делаются без «go»)

  Формат: collapse-to-green (Доминика, lab-monitor.py) — тихий день = 1 строка,
  раскрываем только блоки с сигналом. Без вызова LLM ($0/день).

Использование:
    python3 -m lab_monitoring.stack_patrol --two-phase            # двухфазный дайджест
    python3 -m lab_monitoring.stack_patrol --two-phase --dry-run  # только печать, без записи/отправки
    python3 -m lab_monitoring.stack_patrol --two-phase --telegram --chat-id 173681771
    python3 -m lab_monitoring.stack_patrol --pypi                 # старые источники (legacy)
    python3 -m lab_monitoring.stack_patrol --save                 # сохранить JSON
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(
    os.environ.get("LABMON_STACK_PATROL_DIR", "/var/lib/labmon/stack_patrol")
)
_BASE = (
    Path(__file__).resolve().parent.parent.parent
)  # .../lab-monitoring (корень проекта)
REGISTRY_PATH = _BASE / "patrol" / "registry.json"
STATE_PATH = _BASE / "patrol" / "state.json"
TG_CFG_PATH = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
MSK = timezone(timedelta(hours=3))


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _now_msk() -> datetime:
    return datetime.now(timezone.utc).astimezone(MSK)


def _fetch_json(url: str, timeout: int = 15, token: str | None = None) -> Any:
    headers = {
        "User-Agent": "labmon-stack-patrol/2.0",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _gh_token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        cfg = json.load(open(TG_CFG_PATH))
        for key in ("github", "providers"):
            node = cfg.get(key, {})
            t = node.get("token") or node.get("apiKey")
            if t:
                return t
    except Exception:
        pass
    return None


def _tg_bot_token() -> str | None:
    try:
        cfg = json.load(open(TG_CFG_PATH))
        tg = cfg.get("channels", {}).get("telegram", {}).get("accounts", {})
        if isinstance(tg, dict):
            if tg.get("raven", {}).get("botToken"):
                return tg["raven"]["botToken"]
            for acc in tg.values():
                if isinstance(acc, dict) and acc.get("botToken"):
                    return acc["botToken"]
    except Exception:
        pass
    return None


def _get_installed_version(pkg: str) -> str | None:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return None


# ─── PyPI (legacy) ────────────────────────────────────────────────────────────

STACK_PACKAGES_CORE = [
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
    "SQLAlchemy",
    "alembic",
    "asyncpg",
    "httpx",
    "aiohttp",
    "requests",
    "aiogram",
    "redis",
    "hiredis",
    "python-jose",
    "passlib",
    "bcrypt",
    "python-multipart",
    "aiosqlite",
    "aiofiles",
    "python-dotenv",
    "PyYAML",
    "loguru",
    "structlog",
    "typer",
    "click",
    "sse-starlette",
    "slowapi",
    "pytest",
    "pytest-asyncio",
    "coverage",
    "ruff",
    "mypy",
]
STACK_PACKAGES_EXTRA = [
    "boto3",
    "aioboto3",
    "openai",
    "beautifulsoup4",
    "lxml",
    "pdfminer.six",
    "pillow",
    "openpyxl",
    "paramiko",
    "Jinja2",
    "Mako",
    "python-dateutil",
    "tzlocal",
    "cryptography",
    "PyNaCl",
    "imap-tools",
    "Telethon",
    "playwright",
    "reportlab",
]


def pypi_check(packages: list[str] | None = None) -> list[dict]:
    if packages is None:
        packages = STACK_PACKAGES_CORE + STACK_PACKAGES_EXTRA
    results = []
    for pkg in packages:
        installed = _get_installed_version(pkg)
        if installed is None:
            continue
        try:
            data = _fetch_json(f"https://pypi.org/pypi/{pkg}/json")
            latest = data.get("info", {}).get("version", "?")
            flag = "✅" if latest == installed else "🔵"
            if latest != installed:
                try:
                    if int(latest.split(".")[0]) != int(installed.split(".")[0]):
                        flag = "🔴"
                    elif int(latest.split(".")[1]) != int(installed.split(".")[1]):
                        flag = "⚠️"
                except (IndexError, ValueError):
                    pass
            results.append(
                {"package": pkg, "installed": installed, "latest": latest, "flag": flag}
            )
        except Exception as e:
            results.append(
                {
                    "package": pkg,
                    "installed": installed,
                    "latest": "ERROR",
                    "flag": "❌",
                    "error": str(e),
                }
            )
    return results


# ─── Hacker News (legacy) ─────────────────────────────────────────────────────

HN_QUERIES = [
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "python async",
    "python security",
    "python llm",
    "uvicorn",
    "alembic",
]


def hn_scan() -> list[dict]:
    results = []
    seen: set[str] = set()
    for query in HN_QUERIES:
        try:
            url = f"https://hn.algolia.com/api/v1/search?query={urllib.parse.quote(query)}&tags=story&hitsPerPage=5"
            data = _fetch_json(url)
            for hit in data.get("hits", []):
                story_url = (
                    hit.get("url")
                    or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                )
                if story_url in seen:
                    continue
                seen.add(story_url)
                points = hit.get("points") or 0
                if points < 10:
                    continue
                results.append(
                    {
                        "query": query,
                        "title": hit.get("title", ""),
                        "points": points,
                        "comments": hit.get("num_comments", 0),
                        "date": hit.get("created_at", "")[:10],
                        "url": story_url,
                    }
                )
        except Exception as e:
            results.append({"query": query, "error": str(e)})
    results.sort(key=lambda x: x.get("points", 0), reverse=True)
    return results


# ─── GitHub trending по СКОРОСТИ РОСТА (fix «поебени») ─────────────────────────


def github_trending(
    keys: list[str] | None = None,
    per_key: int = 5,
    days: int = 7,
    min_velocity: float = 15.0,
) -> list[dict]:
    """Trending = недавние репо с высокой скоростью роста звёзд (⭐/сут), не all-time."""
    if keys is None:
        keys = ["agentic", "llm-tools", "autonomous-agents", "rag", "mcp-server"]
    token = _gh_token()
    out: list[dict] = []
    since = (_now_msk() - timedelta(days=days)).strftime("%Y-%m-%d")
    for key in keys:
        q = f"{key} stars:>30 pushed:>{since}"
        try:
            data = _fetch_json(
                f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}"
                f"&sort=stars&order=desc&per_page={per_key}",
                token=token,
            )
            for item in data.get("items", []):
                stars = item.get("stargazers_count", 0)
                created = item.get("created_at", "")
                try:
                    age = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(created.replace("Z", "+00:00"))
                    ).days or 1
                    velocity = stars / age if age > 0 else stars
                except Exception:
                    velocity = 0.0
                if velocity >= min_velocity:
                    out.append(
                        {
                            "repo": item["full_name"],
                            "stars": stars,
                            "velocity": round(velocity, 1),
                            "desc": (item.get("description") or "")[:60],
                        }
                    )
        except Exception:
            pass
    out.sort(key=lambda x: x["velocity"], reverse=True)
    return out[: max(5, per_key)]


# ─── RSS (legacy) ────────────────────────────────────────────────────────────

RSS_FEEDS = [
    ("Real Python", "https://realpython.com/atom.xml"),
    ("Python Insider", "https://pythoninsider.blogspot.com/feeds/posts/default"),
    ("PyCoder's Weekly", "https://pycoders.com/feed.atom"),
]


def rss_scan() -> list[dict]:
    results = []
    for name, url in RSS_FEEDS:
        try:
            xml = (
                urllib.request.urlopen(url, timeout=10)
                .read()
                .decode("utf-8", errors="replace")
            )
            titles = re.findall(r"<title[^>]*>([^<]+)</title>", xml)
            links = re.findall(r'<link[^>]*href="([^"]+)"', xml)
            for title, link in list(zip(titles[1:6], links[:5])):
                results.append(
                    {"feed": name, "title": title.strip(), "url": link.strip()}
                )
        except Exception as e:
            results.append({"feed": name, "error": str(e)})
    return results


# ─── OSV / CVE ────────────────────────────────────────────────────────────────

OSV_API = "https://api.osv.dev/v1/query"


def _osv_severity(vuln: dict) -> str:
    try:
        for sev in vuln.get("severity", []):
            if sev.get("type") == "CVSS_V3":
                s = sev.get("score", "")
                if "CRITICAL" in s or "/C:H/I:H/A:H" in s:
                    return "🔴 CRITICAL"
                if "HIGH" in s or "/C:H" in s or "/I:H" in s or "/A:H" in s:
                    return "🟠 HIGH"
                if "MEDIUM" in s or "/C:L" in s or "/I:L" in s or "/A:L" in s:
                    return "🟡 MEDIUM"
                if "LOW" in s:
                    return "🔵 LOW"
    except Exception:
        pass
    return "⚪ UNKNOWN"


def _osv_query(name: str, version: str) -> list[dict]:
    payload = json.dumps(
        {"version": version, "package": {"name": name, "ecosystem": "PyPI"}}
    ).encode()
    req = urllib.request.Request(
        OSV_API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "labmon-stack-patrol/2.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("vulns", [])


def cve_check(packages: list[str] | None = None) -> list[dict]:
    if packages is None:
        packages = STACK_PACKAGES_CORE + STACK_PACKAGES_EXTRA
    results = []
    for pkg in packages:
        installed = _get_installed_version(pkg)
        if installed is None:
            continue
        try:
            for vuln in _osv_query(pkg, installed)[:3]:
                results.append(
                    {
                        "package": pkg,
                        "installed": installed,
                        "id": vuln.get("id", "?"),
                        "summary": vuln.get("summary", "")[:100],
                        "severity": _osv_severity(vuln),
                    }
                )
        except Exception as e:
            results.append({"package": pkg, "installed": installed, "error": str(e)})
    return results


# ─── ФАЗА 1: releases-watch (state-diff) ──────────────────────────────────────


def _load_json(path: Path) -> dict:
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _gh_release_latest(repo: str) -> dict | None:
    """Последний релиз репо: через `gh api` (авторизован, без лимита) либо Atom-ленту."""
    try:
        out = subprocess.run(
            ["gh", "api", f"/repos/{repo}/releases?per_page=1", "--jq", ".[0]"],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if out.returncode == 0 and out.stdout.strip() not in ("", "null"):
            return json.loads(out.stdout)
    except Exception:
        pass
    try:
        xml = (
            urllib.request.urlopen(
                f"https://github.com/{repo}/releases.atom", timeout=15
            )
            .read()
            .decode("utf-8", "replace")
        )
        m = re.search(r"<entry>.*?<title>([^<]+)</title>", xml, re.S)
        u = re.search(r"<entry>.*?<updated>([^<]+)</updated>", xml, re.S)
        if m:
            return {
                "tag_name": m.group(1).strip(),
                "published_at": u.group(1).strip() if u else None,
            }
    except Exception:
        pass
    return None


def releases_watch(registry: dict, state: dict) -> tuple[list[dict], dict]:
    """Вернуть новые релизы (отсутствовали в state) и обновлённый state['releases']."""
    new_releases: list[dict] = []
    seen: dict[str, dict] = {}
    for grp, gdata in registry.get("groups", {}).items():
        if "releases" not in gdata.get("track", []):
            continue
        for repo in gdata.get("repos", []):
            try:
                rel = _gh_release_latest(repo)
                if not rel:
                    seen[repo] = state.get("releases", {}).get(repo, {})
                    continue
                tag = rel.get("tag_name") or rel.get("name")
                pub = rel.get("published_at")
                seen[repo] = {"tag": tag, "published_at": pub}
                prev = state.get("releases", {}).get(repo)
                if prev is None or prev.get("tag") != tag:
                    new_releases.append(
                        {
                            "repo": repo,
                            "tag": tag,
                            "published_at": pub,
                            "security": "security" in gdata.get("track", []),
                            "priority": gdata.get("priority"),
                        }
                    )
            except Exception:
                seen[repo] = state.get("releases", {}).get(repo, {})
    new_releases.sort(key=lambda r: (r.get("priority") != "HIGH", r["repo"]))
    return new_releases, seen


# ─── ФАЗА 2: наш стек — outdated + CVE (через OSV) ───────────────────────────


def our_stack_scan(targets: list[str]) -> dict:
    """Для каждого python-окружения: outdated + CVE (только по outdated, через OSV)."""
    report: dict[str, Any] = {}
    for tgt in targets:
        if not os.path.exists(tgt):
            report[tgt] = {"error": "not found"}
            continue
        try:
            out = subprocess.run(
                [tgt, "-m", "pip", "list", "--outdated", "--format=json"],
                capture_output=True,
                text=True,
                timeout=90,
            )
            outdated = json.loads(out.stdout) if out.stdout.strip() else []
        except Exception as e:
            report[tgt] = {"error": str(e)}
            continue
        cves = []
        for pkg in outdated:
            name, ver = pkg["name"], pkg["version"]
            try:
                vulns = _osv_query(name, ver)
            except Exception:
                vulns = []
            if vulns:
                cves.append(
                    {
                        "package": name,
                        "installed": ver,
                        "latest": pkg.get("latest_version"),
                        "vulns": vulns,
                    }
                )
        report[tgt] = {"outdated": outdated, "cves": cves}
    return report


def _short(tgt: str) -> str:
    if "/projects/" in tgt:
        return tgt.split("/projects/")[-1].split("/")[0]
    return os.path.basename(os.path.dirname(tgt)) or tgt


# ─── Сборка двухфазного дайджеста (collapse-to-green) ────────────────────────


def build_two_phase(
    new_releases: list[dict], trends: list[dict], stack: dict, as_json: bool = False
) -> str:
    stamp = _now_msk().strftime("%d.%m %H:%M")
    cve_total = sum(
        len(v.get("cves", []))
        for v in stack.values()
        if isinstance(v, dict) and "cves" in v
    )

    if as_json:
        return json.dumps(
            {
                "timestamp": _now_msk().isoformat(),
                "releases": new_releases,
                "trends": trends,
                "stack": stack,
            },
            ensure_ascii=False,
            indent=2,
        )

    if cve_total > 0:
        overall, summary = "🔴", f"{cve_total} риск"
    elif new_releases or trends:
        overall, summary = "🟡", "есть обновления"
    else:
        overall, summary = "🟢", "спокойно"

    lines = [f"🐦‍⬛ Внешний патруль · {stamp} МСК · {overall} {summary}"]

    if cve_total == 0 and not new_releases and not trends:
        return "\n".join(lines)  # тихий день — только шапка

    if new_releases:
        lines.append("• Релизы:")
        for r in new_releases[:15]:
            flag = " 🔒" if r.get("security") else ""
            lines.append(f"  - {r['repo']} → {r['tag']}{flag}")
    if trends:
        lines.append("• Тренды (рост ⭐/сут):")
        for t in trends[:5]:
            lines.append(f"  - {t['repo']} +{t['velocity']}⭐/сут ({t['stars']}⭐)")
    if cve_total > 0:
        lines.append("• Дыры (CVE):")
        for tgt, v in stack.items():
            if not isinstance(v, dict) or not v.get("cves"):
                continue
            lines.append(f"  - {_short(tgt)}: {len(v['cves'])} CVE")
            for c in v["cves"][:5]:
                sev = _osv_severity(c["vulns"][0]) if c.get("vulns") else "⚪"
                lines.append(
                    f"    · {c['package']} {c['installed']} → {c.get('latest')} ({sev})"
                )
        lines.append("• Обновить (план, только по «go»):")
        for tgt, v in stack.items():
            if not isinstance(v, dict) or not v.get("cves"):
                continue
            pkgs = ", ".join(c["package"] for c in v["cves"][:5])
            lines.append(f"  - {_short(tgt)}: {pkgs}")
    return "\n".join(lines)


# ─── Форматирование legacy / сохранение ───────────────────────────────────────

# ─── 3 варианта формата дайджеста (DDP, выбор ЗавЛаба 18.07) ──────────────────

_SEV_RANK = {
    "🔴 CRITICAL": 4,
    "🟠 HIGH": 3,
    "🟡 MEDIUM": 2,
    "🔵 LOW": 1,
    "⚪ UNKNOWN": 0,
}
_SEV_CANON = {
    "🔴 CRITICAL": "CRITICAL",
    "🟠 HIGH": "HIGH",
    "🟡 MEDIUM": "MEDIUM",
    "🔵 LOW": "LOW",
    "⚪ UNKNOWN": "UNKNOWN",
}


def _sev_of(cve):
    return _osv_severity(cve["vulns"][0]) if cve.get("vulns") else "⚪ UNKNOWN"


def _sev_summary(stack: dict) -> dict:
    c = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    for tgt, v in stack.items():
        if not isinstance(v, dict) or not v.get("cves"):
            continue
        for cve in v["cves"]:
            c[_SEV_CANON[_sev_of(cve)]] += 1
    return c


def _cve_targets(stack: dict, levels=("🔴 CRITICAL", "🟠 HIGH")) -> list:
    out = []
    for tgt, v in stack.items():
        if not isinstance(v, dict) or not v.get("cves"):
            continue
        if any(_sev_of(c) in levels for c in v["cves"]):
            out.append(tgt)
    return out


def _high_pkgs(stack: dict) -> list:
    pkgs = set()
    for tgt, v in stack.items():
        if not isinstance(v, dict) or not v.get("cves"):
            continue
        for cve in v["cves"]:
            if _sev_of(cve) in ("🔴 CRITICAL", "🟠 HIGH"):
                pkgs.add(cve["package"])
    return sorted(pkgs)


def build_v1(new_releases, trends, stack) -> str:
    """Вариант 1 — действие прежде всего (decision-first)."""
    stamp = _now_msk().strftime("%d.%m %H:%M")
    sev = _sev_summary(stack)
    high = sev["CRITICAL"] + sev["HIGH"]
    med = sev["MEDIUM"]
    active = high + med > 0
    if active:
        overall, summ = "🟠", "есть что проверить"
    elif new_releases or trends:
        overall, summ = "🟡", "есть обновления"
    else:
        overall, summ = "🟢", "спокойно"
    lines = [f"🐦‍⬛ Внешний патруль · {stamp} МСК · {overall} {summ}"]
    if overall == "🟢":
        return "\n".join(lines)
    if active:
        lines.append(
            "Что сделать: «go» → обновлю venv с HIGH/MED CVE (только CVE-фикс, системный python не трогаю)"
        )
    if high + med > 0:
        lines.append(f"• CVE (активные): {high} HIGH · {med} MED · остальное — шум")
        for tgt, v in stack.items():
            if not isinstance(v, dict) or not v.get("cves"):
                continue
            pkgs = [
                f"{c['package']}({_SEV_CANON[_sev_of(c)]})"
                for c in v["cves"]
                if _sev_of(c) in ("🔴 CRITICAL", "🟠 HIGH")
            ]
            if pkgs:
                lines.append(f"  - {_short(tgt)}: " + ", ".join(pkgs))
    if new_releases:
        lines.append(f"• Релизы (важные для тебя): {len(new_releases)}")
        for r in new_releases[:8]:
            flag = " 🔒" if r.get("security") else ""
            lines.append(f"  - {r['repo']} → {r['tag']}{flag}")
    if trends:
        lines.append("• Тренды: скрыто (не по лабе)")
    return "\n".join(lines)


def build_v2(new_releases, trends, stack) -> str:
    """Вариант 2 — утренний бриф (шаблонная человеческая речь, БЕЗ LLM)."""
    d = _now_msk().strftime("%d.%m")
    sev = _sev_summary(stack)
    high = sev["CRITICAL"] + sev["HIGH"]
    med = sev["MEDIUM"]
    if high + med == 0 and not new_releases and not trends:
        return f"🐦‍⬛ Утро, {d} — тихо, внимания не требует."
    lines = [f"🐦‍⬛ Утро, {d}"]
    if high + med > 0:
        tgs = [_short(t) for t in _cve_targets(stack)]
        pkgs = _high_pkgs(stack)
        lines.append(
            f"В {len(tgs)} твоих проектах ({', '.join(tgs)}) зависли старые пакеты: "
            f"{', '.join(pkgs[:3])} висят с HIGH-уязвимостями. Не критично, но реально."
        )
        lines.append("Дам «go» — обновлю только их, системный python не трогаю.")
    else:
        lines.append("Серьёзных дыр в твоих venv нет.")
    if new_releases:
        sec = [r for r in new_releases if r.get("security")]
        tail = (
            f" ({', '.join(r['repo'].split('/')[-1] for r in sec[:3])}…)" if sec else ""
        )
        lines.append(
            f"Из релизов ничего страшного: {len(sec)} выкатили безопасные обновы{tail}."
        )
    lines.append("Тренды дня (ECC, superpowers) к лабе не относятся — не выношу.")
    lines.append(
        "👉 Жду «go» на фикс venv." if high + med > 0 else "👉 Всё под контролем."
    )
    return "\n".join(lines)


def build_v3(new_releases, trends, stack) -> str:
    """Вариант 3 — статус-карточка (SRE status-card, матрица severity)."""
    stamp = _now_msk().strftime("%d.%m %H:%M")
    sev = _sev_summary(stack)
    high = sev["CRITICAL"] + sev["HIGH"]
    med = sev["MEDIUM"]
    if high + med == 0 and not new_releases and not trends:
        return f"🐦‍⬛ Патруль · {stamp} МСК · 🟢 0H 0M — спокойно"
    hdr = "🟢 0H 0M" if high + med == 0 else f"🟠 {high}H {med}M"
    lines = [f"🐦‍⬛ Патруль · {stamp} МСК · {hdr}"]
    if new_releases:
        sec = sum(1 for r in new_releases if r.get("security"))
        top3 = ", ".join(r["repo"].split("/")[-1] for r in new_releases[:3])
        lines.append(f"📦 Релизы: {len(new_releases)} (🔒 {sec}) — {top3} обновились")
    if trends:
        top = trends[0]
        lines.append(
            f"📈 Тренды: +{top['velocity']}⭐/сут {top['repo'].split('/')[-1]} (не по лабе)"
        )
    if high + med > 0:
        lines.append("🔧 CVE по venv:")
        for tgt, v in stack.items():
            if not isinstance(v, dict) or not v.get("cves"):
                continue
            th = sum(1 for c in v["cves"] if _sev_of(c) in ("🔴 CRITICAL", "🟠 HIGH"))
            tm = sum(1 for c in v["cves"] if _sev_of(c) == "🟡 MEDIUM")
            if th + tm == 0:
                continue
            icon = "🔴" if th > 0 else "🟠"
            lines.append(f"  {_short(tgt):12} {icon} {th}H {tm}M")
    lines.append("👉 «go» → фикс venv (только CVE)")
    return "\n".join(lines)


def _build_variant(n, new_releases, trends, stack, as_json=False):
    if as_json:
        return json.dumps(
            {"variant": n, "releases": new_releases, "trends": trends, "stack": stack},
            ensure_ascii=False,
            indent=2,
        )
    return {1: build_v1, 2: build_v2, 3: build_v3}[n](new_releases, trends, stack)


def format_report(
    pypi=None, hn=None, github=None, rss=None, cve=None, as_json=False
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if as_json:
        return json.dumps(
            {
                "timestamp": ts,
                "pypi": pypi,
                "hn": hn,
                "github": github,
                "rss": rss,
                "cve": cve,
            },
            ensure_ascii=False,
            indent=2,
        )
    lines = [f"🔍 Stack Patrol — {ts}", ""]
    if pypi is not None:
        lines.append(f"📦 PyPI ({len(pypi)} пакетов):")
        for it in pypi:
            if it["flag"] != "✅":
                lines.append(
                    f"  {it['flag']} {it['package']}: {it['installed']} → {it['latest']}"
                )
    if cve is not None:
        lines.append("")
        lines.append(f"🔒 CVE ({len(cve)}):")
        for it in cve:
            if "error" in it:
                continue
            lines.append(
                f"  {it.get('severity', '?')} {it['package']} ({it['installed']}): {it.get('id', '?')}"
            )
    if hn is not None:
        lines.append("")
        lines.append("🌐 Hacker News:")
        for it in hn[:20]:
            if "error" in it:
                continue
            lines.append(f"  [{it.get('points', 0)}⭐] {it.get('title', '')}")
    if github is not None:
        lines.append("")
        lines.append("🐙 GitHub Trending:")
        for it in github[:15]:
            if "error" in it:
                continue
            lines.append(f"  • {it['repo']} (+{it.get('velocity', '?')}⭐/сут)")
    if rss is not None:
        lines.append("")
        lines.append("📰 RSS:")
        for it in rss[:15]:
            if "error" in it:
                continue
            lines.append(f"  [{it['feed']}] {it['title']}")
    return "\n".join(lines)


def save_report(raw: str, extra: dict | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = _now_msk().strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"patrol_{date_str}.json"
    data = {"timestamp": _now_msk().isoformat(), "text": raw}
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


# ─── Telegram ─────────────────────────────────────────────────────────────────


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # дробим на чанки по 4000 символов
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    for ch in chunks:
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": ch,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode()
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"⚠️ Telegram send failed: {e}")


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        description="Stack Patrol — внешний патруль лаборатории"
    )
    p.add_argument(
        "--two-phase",
        action="store_true",
        help="Двухфазный дайджест (релизы+тренды+CVE+обновления)",
    )
    p.add_argument("--pypi", action="store_true", help="Только PyPI (legacy)")
    p.add_argument("--hn", action="store_true", help="Только Hacker News (legacy)")
    p.add_argument(
        "--github", action="store_true", help="Только GitHub trending (legacy)"
    )
    p.add_argument("--rss", action="store_true", help="Только RSS (legacy)")
    p.add_argument("--cve", action="store_true", help="Только CVE (legacy)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--save", action="store_true", help="Сохранить отчёт (JSON)")
    p.add_argument(
        "--dry-run", action="store_true", help="Не писать state/файл и не слать в TG"
    )
    p.add_argument(
        "--telegram",
        action="store_true",
        help="Отправить в Telegram (токен из openclaw.json)",
    )
    p.add_argument(
        "--telegram-token", type=str, help="Telegram bot token (иначе из openclaw.json)"
    )
    p.add_argument("--chat-id", type=str, default="173681771", help="Telegram chat ID")
    p.add_argument(
        "--variant",
        type=int,
        choices=[1, 2, 3],
        default=None,
        help="Вариант формата дайджеста: 1=действие, 2=бриф, 3=карточка",
    )
    p.add_argument(
        "--collect-json",
        type=str,
        help="Собрать данные и сохранить в JSON (без вывода/отправки)",
    )
    p.add_argument(
        "--from-json",
        type=str,
        help="Построить отчёт из сохранённого JSON (--variant N)",
    )
    args = parser_args(p)

    if args.collect_json:
        registry = _load_json(REGISTRY_PATH)
        state = _load_json(STATE_PATH)
        new_releases, seen = releases_watch(registry, state)
        trends = github_trending(
            keys=registry.get("trends", {}).get("search_keys"),
            per_key=registry.get("trends", {}).get("per_key", 5),
        )
        stack = our_stack_scan(registry.get("security_scan", {}).get("targets", []))
        json.dump(
            {"releases": new_releases, "trends": trends, "stack": stack},
            open(args.collect_json, "w"),
            ensure_ascii=False,
            indent=2,
        )
        print(
            f"collected -> {args.collect_json} (releases={len(new_releases)}, cve_targets={len(_cve_targets(stack))})"
        )
        return

    if args.from_json:
        data = json.load(open(args.from_json))
        print(
            _build_variant(
                args.variant or 1,
                data["releases"],
                data["trends"],
                data["stack"],
                as_json=args.json,
            )
        )
        return

    if args.two_phase:
        registry = _load_json(REGISTRY_PATH)
        state = _load_json(STATE_PATH) if not args.dry_run else _load_json(STATE_PATH)
        new_releases, seen = releases_watch(registry, state)
        trends = github_trending(
            keys=registry.get("trends", {}).get("search_keys"),
            per_key=registry.get("trends", {}).get("per_key", 5),
        )
        stack = our_stack_scan(registry.get("security_scan", {}).get("targets", []))
        if args.variant:
            report = _build_variant(
                args.variant, new_releases, trends, stack, as_json=args.json
            )
        else:
            report = build_two_phase(new_releases, trends, stack, as_json=args.json)

        print(report)

        if not args.dry_run:
            if args.save or args.telegram:
                save_report(
                    report, {"releases": new_releases, "trends": trends, "stack": stack}
                )
            # пишем state (чтобы релизы не дублировались)
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            json.dump(
                {"releases": seen}, open(STATE_PATH, "w"), ensure_ascii=False, indent=2
            )
        if args.telegram and not args.dry_run:
            token = args.telegram_token or _tg_bot_token()
            if token:
                _send_telegram(token, args.chat_id, report)
            else:
                print("⚠️ Нет Telegram-токена — не отправлено")
        return

    # legacy full / partial run
    full = not any([args.pypi, args.hn, args.github, args.rss, args.cve])
    pypi_r = pypi_check() if (full or args.pypi) else None
    hn_r = hn_scan() if (full or args.hn) else None
    if full or args.github:
        reg = _load_json(REGISTRY_PATH)
        gh_r = github_trending(keys=reg.get("trends", {}).get("search_keys"))
    else:
        gh_r = None
    rss_r = rss_scan() if (full or args.rss) else None
    cve_r = cve_check() if (full or args.cve) else None

    report = format_report(pypi_r, hn_r, gh_r, rss_r, cve_r, as_json=args.json)
    print(report)
    if args.save:
        print(f"\n📎 Отчёт сохранён: {save_report(report)}")


def parser_args(p: argparse.ArgumentParser):
    # небольшая обёртка, чтобы сохранить читаемость main()
    return p.parse_args()


if __name__ == "__main__":
    main()
