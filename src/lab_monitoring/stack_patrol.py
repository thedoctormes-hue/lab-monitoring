"""Стек-патруль: разведка стека лаборатории наружу.

Источники:
  - PyPI — версии пакетов (установленная vs последняя)
  - Hacker News — посты по ключевым словам
  - GitHub — trending Python репозитории
  - RSS — Python blogs (realpython.com, pythontunnel и др.)
  - NVD/CVE — уязвимости в зависимостях через OSV API

Использование:
    python3 -m lab_monitoring.stack_patrol                     # полный патруль
    python3 -m lab_monitoring.stack_patrol --pypi              # только PyPI
    python3 -m lab_monitoring.stack_patrol --hn                # только HN
    python3 -m lab_monitoring.stack_patrol --github            # только GitHub trending
    python3 -m lab_monitoring.stack_patrol --rss               # только RSS
    python3 -m lab_monitoring.stack_patrol --cve               # только CVE проверка
    python3 -m lab_monitoring.stack_patrol --json              # JSON output
    python3 -m lab_monitoring.stack_patrol --save              # сохранить отчёт
    python3 -m lab_monitoring.stack_patrol --telegram-token T --chat-id C  # отправить в Telegram
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(
    os.environ.get("LABMON_STACK_PATROL_DIR", "/var/lib/labmon/stack_patrol")
)


# ─── Пакеты стека ──────────────────────────────────────────────────────────────
# Версии НЕ хардкодятся — читаются из venv через importlib.metadata

STACK_PACKAGES_CORE = [
    # Web framework
    "fastapi",
    "starlette",
    "uvicorn",
    # Data & validation
    "pydantic",
    "pydantic-settings",
    # ORM & DB
    "SQLAlchemy",
    "alembic",
    "asyncpg",
    # HTTP clients
    "httpx",
    "aiohttp",
    "requests",
    # Telegram
    "aiogram",
    # Redis
    "redis",
    "hiredis",
    # Auth / security
    "python-jose",
    "passlib",
    "bcrypt",
    "python-multipart",
    # Async
    "aiosqlite",
    "aiofiles",
    # Utils
    "python-dotenv",
    "PyYAML",
    "loguru",
    "structlog",
    "typer",
    "click",
    # Observability
    "sse-starlette",
    "slowapi",
    # Dev / testing
    "pytest",
    "pytest-asyncio",
    "coverage",
    "ruff",
    "mypy",
]

STACK_PACKAGES_EXTRA = [
    # Cloud / AWS
    "boto3",
    "aioboto3",
    # AI / LLM
    "openai",
    # Parsing
    "beautifulsoup4",
    "lxml",
    "pdfminer.six",
    "pillow",
    # Excel / reports
    "openpyxl",
    # SSH / system
    "paramiko",
    # Templates
    "Jinja2",
    "Mako",
    # Date/time
    "python-dateutil",
    "tzlocal",
    # Crypto
    "cryptography",
    "PyNaCl",
    # IMAP / mail
    "imap-tools",
    # Telethon
    "Telethon",
    # Playwright
    "playwright",
    # ReportLab
    "reportlab",
]


def _get_installed_version(pkg: str) -> str | None:
    """Прочитать установленную версию пакета из venv."""
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return None


def _fetch_json(url: str, timeout: int = 15) -> Any:
    """HTTP GET → JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "labmon-stack-patrol/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_text(url: str, timeout: int = 15) -> str:
    """HTTP GET → text."""
    req = urllib.request.Request(url, headers={"User-Agent": "labmon-stack-patrol/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ─── PyPI ─────────────────────────────────────────────────────────────────────

def _is_major_diff(a: str, b: str) -> bool:
    """Мажорная разница версий."""
    try:
        return int(a.split(".")[0]) != int(b.split(".")[0])
    except (IndexError, ValueError):
        return False


def _is_minor_diff(a: str, b: str) -> bool:
    """Минорная разница версий."""
    try:
        return int(a.split(".")[1]) != int(b.split(".")[1])
    except (IndexError, ValueError):
        return False


def pypi_check(packages: list[str] | None = None) -> list[dict]:
    """Проверить PyPI: установленная vs последняя версия."""
    if packages is None:
        packages = STACK_PACKAGES_CORE + STACK_PACKAGES_EXTRA

    results = []
    for pkg in packages:
        installed = _get_installed_version(pkg)
        if installed is None:
            continue  # не установлен — пропускаем
        try:
            data = _fetch_json(f"https://pypi.org/pypi/{pkg}/json")
            latest = data.get("info", {}).get("version", "?")
            if latest != installed:
                if _is_major_diff(installed, latest):
                    flag = "🔴"
                elif _is_minor_diff(installed, latest):
                    flag = "⚠️"
                else:
                    flag = "🔵"
            else:
                flag = "✅"
            results.append({
                "package": pkg,
                "installed": installed,
                "latest": latest,
                "flag": flag,
            })
        except Exception as e:
            results.append({
                "package": pkg,
                "installed": installed,
                "latest": "ERROR",
                "flag": "❌",
                "error": str(e),
            })
    return results


# ─── Hacker News ──────────────────────────────────────────────────────────────

HN_QUERIES = [
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "python async",
    "python observability",
    "python security",
    "python llm",
    "opentelemetry python",
    "uvicorn",
    "alembic",
]


def hn_scan() -> list[dict]:
    """Сканировать Hacker News по ключевым словам."""
    results = []
    seen_urls: set[str] = set()
    for query in HN_QUERIES:
        try:
            url = f"https://hn.algolia.com/api/v1/search?query={urllib.parse.quote(query)}&tags=story&hitsPerPage=5"
            data = _fetch_json(url)
            for hit in data.get("hits", []):
                story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                if story_url in seen_urls:
                    continue
                seen_urls.add(story_url)
                points = hit.get("points") or 0
                if points < 10:
                    continue
                results.append({
                    "query": query,
                    "title": hit.get("title", ""),
                    "points": points,
                    "comments": hit.get("num_comments", 0),
                    "date": hit.get("created_at", "")[:10],
                    "url": story_url,
                })
        except Exception as e:
            results.append({"query": query, "error": str(e)})
    results.sort(key=lambda x: x.get("points", 0), reverse=True)
    return results


# ─── GitHub Trending ──────────────────────────────────────────────────────────

def github_trending() -> list[dict]:
    """Сканировать GitHub trending (Python) — через парсинг HTML."""
    results = []
    try:
        html = _fetch_text("https://github.com/trending/python?since=daily")
        # Простой парсинг: ищем репозитории
        import re
        pattern = r'href="/([^/"]+/[^/"]+)"[^>]*>\s*<[^>]*>\s*([\w\s\-_.]+)'
        for match in re.finditer(r'href="/([^/"]+/[^/"]+)"', html):
            repo = match.group(1)
            if "?" in repo or "login" in repo or "explore" in repo or "trending" in repo:
                continue
            results.append({"repo": repo})
            if len(results) >= 20:
                break
    except Exception as e:
        results.append({"error": str(e)})
    return results


# ─── RSS Feeds ────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    ("Real Python", "https://realpython.com/atom.xml"),
    ("Python Insider", "https://pythoninsider.blogspot.com/feeds/posts/default"),
    ("PyCoder's Weekly", "https://pycoders.com/feed.atom"),
]

def rss_scan() -> list[dict]:
    """Сканировать RSS-ленты Python-блогов."""
    import re
    results = []
    for name, url in RSS_FEEDS:
        try:
            xml = _fetch_text(url, timeout=10)
            titles = re.findall(r'<title[^>]*>([^<]+)</title>', xml)
            links = re.findall(r'<link[^>]*href="([^"]+)"', xml)
            # Пропускаем первый title (заголовок самого фида)
            feed_titles = titles[1:6]
            feed_links = links[:5] if links else []
            entries = list(zip(feed_titles, feed_links)) if feed_titles else []
            for title, link in entries:
                results.append({
                    "feed": name,
                    "title": title.strip(),
                    "url": link.strip(),
                })
        except Exception as e:
            results.append({"feed": name, "error": str(e)})
    return results


# ─── CVE / OSV ────────────────────────────────────────────────────────────────

OSV_API = "https://api.osv.dev/v1/query"

def cve_check(packages: list[str] | None = None) -> list[dict]:
    """Проверить уязвимости через OSV API."""
    if packages is None:
        packages = STACK_PACKAGES_CORE + STACK_PACKAGES_EXTRA

    results = []
    for pkg in packages:
        installed = _get_installed_version(pkg)
        if installed is None:
            continue
        try:
            payload = json.dumps({
                "version": installed,
                "package": {"name": pkg, "ecosystem": "PyPI"},
            }).encode()
            req = urllib.request.Request(
                OSV_API,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "labmon-stack-patrol/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            vulns = data.get("vulns", [])
            if vulns:
                for vuln in vulns[:3]:  # max 3 на пакет
                    results.append({
                        "package": pkg,
                        "installed": installed,
                        "id": vuln.get("id", "?"),
                        "summary": vuln.get("summary", "")[:100],
                        "severity": _osv_severity(vuln),
                    })
        except Exception as e:
            results.append({"package": pkg, "installed": installed, "error": str(e)})
    return results


def _osv_severity(vuln: dict) -> str:
    """Извлечь severity из OSV-записи по CVSS v3 score."""
    try:
        for sev in vuln.get("severity", []):
            if sev.get("type") == "CVSS_V3":
                score = sev.get("score", "")
                # Извлекаем base score из CVSS string
                # CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H → нужно вычислить
                # OSV не даёт числовой score напрямую, так что парсим по severity rating
                if "CRITICAL" in score:
                    return "🔴 CRITICAL"
                elif "HIGH" in score:
                    return "🟠 HIGH"
                elif "MEDIUM" in score:
                    return "🟡 MEDIUM"
                elif "LOW" in score:
                    return "🔵 LOW"
                # Если rating нет в строке — считаем base score по metrics
                # AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H = ~9.8 CRITICAL
                if "/C:H/I:H/A:H" in score:
                    return "🔴 CRITICAL"
                elif "/C:H" in score or "/I:H" in score or "/A:H" in score:
                    return "🟠 HIGH"
                elif "/C:L" in score or "/I:L" in score or "/A:L" in score:
                    return "🟡 MEDIUM"
    except (IndexError, ValueError):
        pass
    return "⚪ UNKNOWN"


# ─── Форматирование ──────────────────────────────────────────────────────────

def format_report(
    pypi: list[dict] | None = None,
    hn: list[dict] | None = None,
    github: list[dict] | None = None,
    rss: list[dict] | None = None,
    cve: list[dict] | None = None,
    as_json: bool = False,
) -> str:
    """Форматировать отчёт."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if as_json:
        return json.dumps(
            {"timestamp": ts, "pypi": pypi, "hn": hn, "github": github, "rss": rss, "cve": cve},
            ensure_ascii=False, indent=2,
        )

    lines = [f"🔍 Stack Patrol — {ts}", ""]

    # PyPI
    if pypi is not None:
        updates = [p for p in pypi if p["flag"] != "✅"]
        lines.append(f"📦 PyPI ({len(pypi)} пакетов, {len(updates)} обновлений):")
        for item in pypi:
            flag = item["flag"]
            if flag == "✅":
                continue  # up-to-date пропускаем в текстовом режиме
            lines.append(f"  {flag} {item['package']}: {item['installed']} → {item['latest']}")
        if not updates:
            lines.append("  ✅ Все пакеты актуальны!")

    # CVE
    if cve is not None:
        lines.append("")
        lines.append(f"🔒 CVE ({len(cve)} уязвимостей):")
        for item in cve:
            if "error" in item:
                continue
            lines.append(f"  {item.get('severity', '?')} {item['package']} ({item['installed']}): {item.get('id', '?')}")
            lines.append(f"    {item.get('summary', '')}")

    # HN
    if hn is not None:
        lines.append("")
        lines.append("🌐 Hacker News (top 20):")
        for item in hn[:20]:
            if "error" in item:
                continue
            pts = item.get("points", 0)
            title = item.get("title", "")
            url = item.get("url", "")
            date = item.get("date", "")
            lines.append(f"  [{pts}⭐] {title} ({date})")
            lines.append(f"       {url}")

    # GitHub
    if github is not None:
        lines.append("")
        lines.append("🐙 GitHub Trending (Python):")
        for item in github[:15]:
            if "error" in item:
                continue
            lines.append(f"  • {item['repo']}")

    # RSS
    if rss is not None:
        lines.append("")
        lines.append("📰 RSS Feeds:")
        for item in rss[:15]:
            if "error" in item:
                continue
            lines.append(f"  [{item['feed']}] {item['title']}")
            lines.append(f"    {item['url']}")

    return "\n".join(lines)


# ─── Сохранение ───────────────────────────────────────────────────────────────

def save_report(**sections) -> Path:
    """Сохранить JSON-отчёт."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"patrol_{date_str}.json"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"timestamp": ts}
    for key in ("pypi", "hn", "github", "rss", "cve"):
        if key in sections and sections[key] is not None:
            data[key] = sections[key]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Stack Patrol — разведка стека")
    parser.add_argument("--pypi", action="store_true", help="Только PyPI версии")
    parser.add_argument("--hn", action="store_true", help="Только Hacker News")
    parser.add_argument("--github", action="store_true", help="Только GitHub trending")
    parser.add_argument("--rss", action="store_true", help="Только RSS")
    parser.add_argument("--cve", action="store_true", help="Только CVE проверка")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--save", action="store_true", help="Сохранить отчёт")
    parser.add_argument("--telegram-token", type=str, help="Telegram bot token")
    parser.add_argument("--chat-id", type=str, help="Telegram chat ID")
    args = parser.parse_args()

    # Если ничего не выбрано — полный патруль
    full_run = not any([args.pypi, args.hn, args.github, args.rss, args.cve])

    pypi_results = pypi_check() if (full_run or args.pypi) else None
    hn_results = hn_scan() if (full_run or args.hn) else None
    github_results = github_trending() if (full_run or args.github) else None
    rss_results = rss_scan() if (full_run or args.rss) else None
    cve_results = cve_check() if (full_run or args.cve) else None

    report = format_report(
        pypi_results, hn_results, github_results, rss_results, cve_results,
        as_json=args.json,
    )
    print(report)

    if args.save:
        path = save_report(
            pypi=pypi_results, hn=hn_results, github=github_results,
            rss=rss_results, cve=cve_results,
        )
        print(f"\n📎 Отчёт сохранён: {path}")

    if args.telegram_token and args.chat_id:
        _send_telegram(args.telegram_token, args.chat_id, report)


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    """Отправить сообщение в Telegram."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram send failed: {e}")


if __name__ == "__main__":
    main()
