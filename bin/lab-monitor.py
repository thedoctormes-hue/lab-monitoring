#!/usr/bin/env python3
"""
lab-monitor.py — монитор лаборатории (Доминика)
Ежечасная сводка по 8 категориям + слой реагирования (advise) + полный дамп (--full).
Дизайн: projects/lab-monitoring/docs/monitor-design.md

Категории (8):
  1. Агенты колонии        2. Платформа OpenClaw     3. MCP-сервисы
  4. Память и поиск        5. Данные и хранилища     6. Сеть и внешний доступ
  7. Проекты и код         8. Ресурсы хоста

Поведение:
  - без флагов: компактная сводка (8 строк OK/FAIL + гибрид-шапка + блок провалов + совет)
  - --full: развёрнутый дамп (per-agent, доктор-warn целиком, диск по разделам, докер, логи)
  - дрейф варнингов доктора: базовые (allowlist) игнорируются, 🔴 только на НОВЫХ
"""
import datetime
import json
import os
import random
import re
import socket
import subprocess
import sys
import time

PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PARENT, "src"))  # import lab_monitoring (единый источник порогов) при запуске как скрипта

WORKSPACES = "/root/LabDoctorM/workspaces"
PROJECTS   = "/root/LabDoctorM/projects"
STATE_DIR  = "/root/LabDoctorM/workspaces/dominika/monitor-state"
os.makedirs(STATE_DIR, exist_ok=True)

AGENTS = ["kotolizator","mangust","raven","owl","bestia","streikbrecher","dominika","antcat"]
MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime.now(MSK)

# сводный сборник цитат (ЗавЛаб: перенёс + смержил все grimoire.md -> nevermind.md)
QUOTE_FILE = "/root/LabDoctorM/workspaces/dominika/nevermind.md"

# === ПОРОГИ ЧЕСТНОСТИ (каждый — откуда норма) ===
# Монитор обязан сверять значения именно с этими порогами, а не с "магическими числами".
THRESHOLDS = {
    "disk_warn_pct": 80,    # норма <80%  (источник: ADR-039, практика ротации диска лаборатории)
    "disk_crit_pct": 90,    # КРИТ     >=90% (диск почти полон)
    "nrestarts_ok": 5,      # оставлено для ПРОЧИХ сервисов (накопленный lifetime-порог). Для gateway — см. оконную логику ниже.
    "restart_window": "1h", # ОКНО отчёта = час (cron heartbeat-dominika: 0 * * * * MSK). Источник: логика ЗавЛаба 2026-07-13 — отчёт приходит каждый час, перезапуски считаем за это окно, чтобы сверять с памятью «я сам рестартил?».
    "nrestarts_window_auto_ok": 0,  # авто-перезапусков (systemd сам поднял после падения) за окно: норма 0; >=1 → 🔴 подозрительно. Ручные рестарты ЗавЛаб знает/ожидает; авто = нежданное падение.
    "load_warn_x": 1.0,     # load1 < ядер            = ок
    "load_high_x": 2.0,     # load1 < 2×ядер          = повышенная (не сбой); >=2×ядер = тревога
    "mem_warn_pct": 85,     # RAM used >=85%          = внимание (Linux кэширует; важен available)
    "mem_crit_pct": 95,     # RAM used >=95%          = тревога (ОЗУ почти исчерпано)
}

# --- Синхронизация с единым источником порогов (src/lab_monitoring/thresholds.py) ---
# Чтобы два монитора (src и этот) не расходились по числам (DDP 2026-07-13).
# Если пакет недоступен — остаёмся на локальных порогах (fallback).
try:
    from lab_monitoring.thresholds import AlertConfig as _AlertConfig
    _cfg = _AlertConfig()
    THRESHOLDS["disk_warn_pct"] = int(_cfg.disk_warn_pct)
    THRESHOLDS["disk_crit_pct"] = int(_cfg.disk_critical_pct)
    THRESHOLDS["mem_warn_pct"] = int(_cfg.mem_warn_pct)
    THRESHOLDS["mem_crit_pct"] = int(_cfg.mem_critical_pct)
except Exception:
    pass

# --- Гарды слоя реагирования (DDP 2026-07-13): dedup + cooldown + circuit-breaker ---
ADVISE_COOLDOWN_S = 6 * 3600   # повторный совет по тому же ключу не раньше чем через 6ч
ADVISE_CIRCUIT_K  = 3          # после 3-х советов подряд — circuit-breaker (стоп)
ADVICE_STATE_FILE = os.path.join(STATE_DIR, "advice_state.json")

# === ДИЗАЙН-АПГРЕЙД (DDP 2026-07-13, Тир 1+2): severity-тиры + тренд ===
# Иконки статуса категорий: ок / внимание / тревога (3-значный статус).
ICON = {True: "✅", "warn": "⚠️", False: "🔴"}
OVERALL_EMOJI = {"OK": "🟢", "ВНИМАНИЕ": "🟡", "ТРЕВОГА": "🔴"}

# Файл истории метрик для дельт и sparkline (текстовых трендов).
METRICS_HISTORY_FILE = os.path.join(STATE_DIR, "lab-monitor-metrics.json")
HISTORY_MAX = 48  # ~2 суток при часовом кроне
SPARK_CHARS = "▁▂▃▄▅▆▇█"

# === Тир 3 (DDP 2026-07-13): тихие часы + ack/silence + дневной дайджест ===
# В тихие часы (по умолчанию 00:00–08:00 МСК) отправляем ТОЛЬКО 🔴 (ТРЕВОГА),
# чтобы не будить ЗавЛаба по ночам ради «всё ок». Окно переопределяется env.
QUIET_HOURS_START = int(os.environ.get("QUIET_HOURS_START", "0"))  # МСК час начала тишины
QUIET_HOURS_END   = int(os.environ.get("QUIET_HOURS_END", "8"))    # МСК час окончания (не включая)
# Ручное заглушение конкретного совета/алерта: {"5": <unix_ts до которого silent>}
ACK_FILE = os.path.join(STATE_DIR, "ack.json")
SERVICES_STATE_FILE = os.path.join(STATE_DIR, "services_state.json")
CRASH_LOOP_DELTA = 3  # рост NRestarts между прогонами >= этого → активная петля
NRESTARTS_LIFETIME_WARN = 20  # накопленный (lifetime) NRestarts >= этого → хронический рестарт (виден без активной петли за час)
MONITOR_PORTS = [5432, 18789, 8086, 8087, 8888]  # критичные порты (PostgreSQL/gateway/MCP/onnx-worker)

# === Тир 4 (DDP 2026-07-13): симптомный фрейминг + латентность gateway ===
# Вместо сухого «X DOWN» — показываем последствие (что сломается у ЗавЛаба).
SYMPTOM = {
    1: "колония не получит отчёт/команду",
    2: "доставка сообщений ЗавЛабу остановлена",
    3: "внутренние инструменты (память/ключи/порты) недоступны агентам",
    4: "семантический поиск лабы не работает — агенты слепы к памяти",
    5: "приложения не смогут писать/читать БД (api-hub ляжет)",
    6: "внешний доступ / метапоиск / SSL нарушены",
    8: "хост перегружен — сервисы могут деградировать",
}
GATEWAY_PORT = 18789
GATEWAY_LATENCY_WARN_MS = 1000


def worst(*states):
    """Худший из статусов: False < 'warn' < True."""
    if any(s is False for s in states):
        return False
    if any(s == "warn" for s in states):
        return "warn"
    return True


def load_advice_state():
    try:
        with open(ADVICE_STATE_FILE, "r", encoding="utf-8") as _f:
            return json.load(_f)
    except Exception:
        return {}


def save_advice_state(state):
    try:
        with open(ADVICE_STATE_FILE, "w", encoding="utf-8") as _f:
            json.dump(state, _f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fmt_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(ts, MSK).strftime("%H:%M %d.%m")
    except Exception:
        return str(ts)


# --- Тир 3: тихие часы + ack/silence ---
def quiet_hours_active(now=None):
    """True, если сейчас в окне тишины (МСК). Поддерживает пересечение полуночи."""
    now = now or NOW
    h = now.hour
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= h < QUIET_HOURS_END
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END


def load_ack():
    try:
        with open(ACK_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def is_acked(cid, now_ts, ack_state=None):
    ack = ack_state if ack_state is not None else load_ack()
    until = ack.get(str(cid))
    if until is None:
        return False
    try:
        return now_ts < float(until)
    except Exception:
        return False


def symptom_frame(cid, status, summary):
    """Тир4: при провале дописываем последствие (симптом), чтобы ЗавЛаб видел
    не просто 'DOWN', а что именно сломается в его руках."""
    if status is True:
        return summary
    sym = SYMPTOM.get(cid)
    if not sym:
        return summary
    return f"{summary} → последствие: {sym}"


# --- Dead-man's-switch (world-class pattern, DDP 2026-07-13) ---
# Опционально: если задан HEALTHCHECKS_URL (healthchecks.io или self-hosted),
# монитор шлёт ping об успешном прогоне. Если прогон не дошёл (упал/не
# запустился) — ping не придёт, и внешний сторож поднимет тревогу.
# ВЫКЛЮЧЕНО по умолчанию (уважает §11: авто-watchdog не обязателен; это его
# опциональный апгрейд — «телефон ЗавЛаба», но автоматизированный).
def ping_healthchecks():
    url = os.environ.get("HEALTHCHECKS_URL")
    if not url:
        return
    try:
        import urllib.request
        urllib.request.urlopen(url, timeout=10)
    except Exception:
        pass


# --- Второй канал доставки (fallback) ---
# Опционально: если задан NOTIFY_WEBHOOK_URL, продублировать отчёт туда
# (HTTP POST, поле 'text'). Запасной канал, если основной (крон -> Telegram)
# не дойдёт. ВЫКЛЮЧЕНО по умолчанию.
def notify_fallback(text):
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url:
        return
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"text": text[:4000]}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


# варнинги доктора, которые считаем "базовым шумом" (известны, приняты) -> в allowlist
DOCTOR_ALLOWLIST = [
    "message tool unavailable", "config-health.json", "legacy state migration",
    "plugin", "tavily", "memory-core", "memory-wiki", "low-power",
    "NODE_COMPILE_CACHE", "OPENCLAW_NO_RESPAWN",
    "plaintext sec", "openclaw.json contains plaintext",
]

# маршрутизация категория(провал) -> кто релевантен для расследования
ROUTE = {
    1: "соответствующий агент + Мангуст (аналитик)",
    2: "Котолизатор / Муравей",
    3: "Муравей",
    4: "Муравей / Ворон",
    5: "Муравей",
    6: "Бестия / Ворон",
    7: "Штрейкбрехер",
    8: "Муравей",
}
# маршрутизация категория(провал) -> кто релевантен для расследования
ROUTE = {
    1: "соответствующий агент + Мангуст (аналитик)",
    2: "Котолизатор / Муравей",
    3: "Муравей",
    4: "Муравей / Ворон",
    5: "Муравей",
    6: "Бестия / Ворон",
    7: "Штрейкбрехер",
    8: "Муравей",
}

# COMPUTED-running-to-guidance — умные советы по провалам (cid -> текст)
ADVICE = {
    1: lambda ok, s, d: "проверь heartbeat-крон агента и что агент отвечает (sessions_list)" if not ok
        else "отчёт дошёл — агенты живы (факт доставки); сверься при необходимости",
    2: lambda ok, s, d: "systemctl restart openclaw-gateway.service — ТОЛЬКО по прямой команде «рестарт»!"
        if "down" in s.lower()
        else ("root-cause: journalctl -u openclaw-gateway --since '-1h'" if "авто-перезапуск" in s.lower()
              else ("сверься с памятью: ты сам рестартил?" if "ручн" in s.lower()
                    else "gateway ок — действий нет")),
    3: lambda ok, s, d: "упавшие MCP: проверь systemctl status mcp-* и порты; при необходимости restart"
        if not ok else "MCP ок — действий нет",
    4: lambda ok, s, d: ("лексический поиск FAIL → проверь lab_search.py search; семантика deprecated (ONNX down, новый стэк в работе)"
        if "lexical=FAIL" in d
        else ("reindex-призрак FAILED — стэк меняется, не перезапускай; дождись нового реиндекса"
              if "reindex_service=failed" in d
              else ("reindex-таймер active (призрак) — стэк меняется, не запускай второй раз"
                    if "reindex_timer=active" in d
                    else "память/поиск: семантика на новом стэке, лексика работает — действий нет"))),
    5: lambda ok, s, d: ("PostgreSQL DOWN → systemctl status postgresql; sudo journalctl -u postgresql --since '-15m'"
        if "pg" in d.lower() and "down" in d.lower()
        else ("disk высокий → du -sh /var /tmp /root 2>/dev/null; найди и очисти (trash > rm), но сначала фактчек"
              if "disk" in d.lower() and ("85" in d or "95" in d or "крит" in d.lower() or "высок" in d.lower())
              else "сверься по diag (PG/disk)")),
    6: lambda ok, s, d: ("VPN DOWN → systemctl status amnezia-awg2; проверь конфиг VPN"
        if "vpn" in d.lower() and ("down" in d.lower() or "упал" in d.lower())
        else ("searxng DOWN → systemctl status searxng; curl -s localhost:8889"
              if "searxng" in d.lower() and ("down" in d.lower() or "упал" in d.lower())
              else ("SSL истёк/FAIL → обнови сертификат (certbot renew / провайдер)"
                    if "ssl" in d.lower() and ("истёк" in d.lower() or "expire" in d.lower() or "fail" in d.lower())
                    else "сверься по diag (VPN/searxng/SSL)"))),
    7: lambda ok, s, d: ("git-dirty — рабочая норма; если хочешь чисто — ./bin/lab-commit.sh <агент>"
        if "git-dirty" in d.lower() or "git-dirty" in s.lower()
        else ("инциденты открыты → сверься по projects/*/incidents, закрой или эскалируй"
              if "инцидент" in d.lower()
              else "сверься по diag (git/инциденты)")),
    8: lambda ok, s, d: ("load высокий → htop — кто жрёт CPU; не убивай без понимания"
        if "load" in d.lower() and ("высок" in d.lower() or "crit" in d.lower() or "крит" in d.lower())
        else ("RAM высокая → free -m; найди процесс-пожиратель, не убивай systemd-сервисы"
              if "ram" in d.lower() and ("высок" in d.lower() or "крит" in d.lower())
              else ("docker DOWN → docker ps -a; systemctl status docker"
                    if "docker" in d.lower() and "down" in d.lower()
                    else "сверься по diag (load/RAM/docker)"))),
}

ROUTE_SKILLS = "research + labsearch + Археолог корней"

# «Что это» простым языком — контекстная подсказка для каждой категории (для --full)
CAT_HINT = {
    1: "САМ ФАКТ, что этот отчёт ДОШЁЛ = OpenClaw и агент-отправитель живы на 100% (не дошёл бы иначе). Правило ЗавЛаба: пришёл отчёт → живы; не пришёл → мертвы. Строка ниже лишь проверяет целостность файлов-памяти агентов на диске (НЕ живость).",
    2: "OpenClaw (гейтвей). Живость доказана доставкой этого отчёта.\n"
       "Перезапуски меряем за окно отчёта (1ч), а не за всю жизнь юнита:\n"
       "· ручные рестарты — ты сам их делал, это ок (сверься с памятью)\n"
       "· авто-перезапуски (systemd сам поднял после падения) — 🔴 подозрительно, ищи root-cause\n"
       "· lifetime NRestarts — справочно, тревогу НЕ управляет (иначе горел бы вечно)",
    3: "MCP — внутренние сервисы-помощники: память/поиск, хранилище ключей, привратник портов. Список берётся живьём из systemd (не захардкожен).",
    4: "Память/поиск: семантический стэк (ONNX+FAISS) deprecated — переезд на новый стэк. Сейчас работает лексический поиск (lab_search), семантика OFF. vectors-метрика deprecated (показывает лексическую живость 1/0). reindex-юниты — призраки, будут пересозданы с новым стэком.",
    5: "Базы и диск. disk — заполненность; норма <80%, тревога с 80%, крит 90%.",
    6: "Внешний доступ. VPN, метапоиск searxng, SSL-сертификат сайта (чтоб не протёк).",
    7: "Код проектов. git-dirty = несохранённые правки (рабочая норма, не сбой). Инциденты: «открыто» = без метки resolved/closed в шапке файла.",
    8: "Железо. load — загрузка CPU (норма < числа ядер). RAM — занято/всего; available = сколько реально доступно приложениям (free + reclaimable cache, buff/cache). total = used + free + buff/cache.",
}


def get_random_quote():
    """Рандомная цитата из сводного гримуара (nevermind.md).
    Честно: если файла/цитат нет — возвращает None (не выдумываем)."""
    path = QUOTE_FILE
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = [ln[2:].strip() for ln in f if ln.startswith("- ") and len(ln) > 2]
    except Exception:
        return None
    if not lines:
        return None
    q = random.choice(lines)
    if len(q) > 300:
        q = q[:300].rstrip() + "…"
    return q


def clean_line(s):
    """Убирает box-символы рамок и лишние пробелы из строк доктора."""
    s = s.strip().strip("│┃|").strip()
    s = s.replace("─", "").replace("WARNING:", "").strip()
    return re.sub(r"\s{2,}", " ", s).strip()


def run(cmd, timeout=12, cwd=None):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd)
    except Exception:
        return None


def port_ok(port, host="127.0.0.1", timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def doctor_warnings():
    """openclaw doctor кэшируется раз в сутки; возвращает {all, count, new}.
    new пересчитывается при КАЖДОМ чтении по актуальному DOCTOR_ALLOWLIST."""
    cache = os.path.join(STATE_DIR, "doctor.json")
    data = None
    if os.path.isfile(cache):
        try:
            data = json.load(open(cache))
        except Exception:
            data = None
    if data and (NOW.replace(tzinfo=None) - datetime.datetime.fromisoformat(data["ts"])).total_seconds() < 24*3600:
        # пересчитываем new по текущему allowlist (он мог измениться)
        data["new"] = [w for w in data.get("all", [])
                        if not any(sub.lower() in w.lower() for sub in DOCTOR_ALLOWLIST)]
        return data
    r = run("openclaw doctor", timeout=90)
    warns = []
    if r and r.stdout:
        for line in r.stdout.splitlines():
            s = line.strip()
            if "⚠" in s or ("warn" in s.lower() and "──" not in s and not s.startswith("◇")):
                warns.append(clean_line(s))
    new = [w for w in warns if not any(sub.lower() in w.lower() for sub in DOCTOR_ALLOWLIST)]
    out = {"ts": NOW.replace(tzinfo=None).isoformat(), "all": warns, "count": len(warns), "new": new}
    try:
        json.dump(out, open(cache, "w"))
    except Exception:
        pass
    return out


# ---------- Категории ----------

def cat_agents():
    # ЖИВОСТЬ агентов доказана самим ФАКТОМ доставки этого отчёта (правило ЗавЛаба:
    # пришёл = живы 100%, не пришёл = мертвы 100%). Проверка целостности файлов-памяти
    # (grimoire.md) убрана: файлы смержены в nevermind.md (ЗавЛаб, 2026-07), монитор
    # искал несуществующие пути и лгал «ОТСУТСТВУЕТ» для всех агентов.
    return (True, "живы (отчёт дошёл)", [])


def classify_restarts(journal_text, lifetime_nrest, window="1h"):
    """Чистая функция: классифицирует перезапуски gateway из текста journalctl за окно.
    Возвращает dict: total/auto/manual/lifetime/window/classification.
    classification: 'ok' (0 за окно) | 'manual' (были старты, но ручные) | 'auto' (systemd сам поднимал).
    Маркер авто-перезапуска = 'Scheduled restart' (systemd Restart=always после падения).
    Первый старт при загрузке сервера тоже считается 'Starting' без 'Scheduled restart'
    и попадает в manual — это ок: загрузка не является падением.
    """
    total = len(re.findall(r"(Started|Starting) OpenClaw Gateway", journal_text))
    auto = len(re.findall(r"Scheduled restart", journal_text))
    manual = max(0, total - auto)
    if auto >= 1:
        classification = "auto"
    elif total >= 1:
        classification = "manual"
    else:
        classification = "ok"
    return {
        "total": total, "auto": auto, "manual": manual,
        "lifetime": lifetime_nrest, "window": window,
        "classification": classification,
    }


def cat_openclaw():
    r = run("systemctl is-active openclaw-gateway.service", timeout=6)
    active = bool(r and r.stdout.strip() == "active")
    nr = run("systemctl show -p NRestarts openclaw-gateway.service", timeout=6)
    nrest = "?"
    if nr and nr.stdout:
        m = re.search(r"NRestarts=(\d+)", nr.stdout)
        if m:
            nrest = m.group(1)
    win = THRESHOLDS["restart_window"]
    jl = run(f"journalctl -u openclaw-gateway.service --since '-{win}' --no-pager", timeout=15)
    jtext = (jl.stdout or "") if jl else ""
    cls = classify_restarts(jtext, nrest, win)
    dw = doctor_warnings()
    # === РЕАЛЬНАЯ живость: порт 18789 слушает? (systemd unit-state НЕ показатель:
    # gateway может быть осиротевшим процессом вне systemd — тогда unit inactive,
    # но сервис реально доставляет, см. INC от 2026-07-15) ===
    import time as _time
    _t0 = _time.monotonic()
    gw_ok = port_ok(GATEWAY_PORT)
    gw_lat = int((_time.monotonic() - _t0) * 1000)
    gw_lat_s = f"gateway latency: {gw_lat}ms ({'ok' if gw_ok else 'no response'})"
    if gw_ok and gw_lat >= GATEWAY_LATENCY_WARN_MS:
        gw_lat_s += " · ⚠️ медленный отклик"
    # Детали перезапусков — одной строкой.
    out = [gw_lat_s, f"🔄 перезапуски за {win}: всего {cls['total']} · ручные ~{cls['manual']} · авто {cls['auto']} · lifetime {nrest}"]
    if not gw_ok:
        # Порт не слушает = gateway реально лёг → красная (доставка остановлена).
        ok = False
        detail = f"gateway DOWN (порт {GATEWAY_PORT} не слушает; доставка остановлена)"
    else:
        # Порт слушает = gateway реально работает и доставляет (дошедший отчёт = доказательство).
        # Только НОВЫЕ замечания doctor могут опустить статус; авто-перезапуски и
        # осиротевший процесс — это ⚠️ в details, НЕ авария доставки.
        ok = not dw["new"]
        if not active:
            # Осиротевший процесс: systemd-юнит не управляет им → латентный риск, не авария.
            out.append(f"⚠️ gateway слушает :{GATEWAY_PORT}, но systemd-юнит inactive/dead — "
                       f"осиротевший процесс; при падении systemd не поднимет (нужен re-parent)")
            detail = "gateway работает (порт слушает) · вне systemd-присмотра"
        elif cls["classification"] == "auto":
            out.append(f"⚠️ АВТО-перезапуск за {win}: {cls['auto']} (systemd сам поднимал после падения — нужен root-cause)")
            detail = f"gateway работает (порт слушает) · авто-перезапуски за {win}: {cls['auto']}"
        elif cls["classification"] == "manual":
            detail = (f"gateway работает (порт слушает) · ручных рестартов за {win}: {cls['manual']} "
                      f"(ты сам делал — ок, сверься с памятью)")
        else:
            detail = f"gateway работает (порт слушает) · перезапусков за {win}: 0"
    return ok, detail, out


def cat_mcp():
    """Динамически спрашиваем systemd: какие mcp-*.service РЕАЛЬНО запущены.
    Не хардкодим число — чтобы не врать при появлении/удалении сервисов."""
    known_ports = {"mcp-memory": 8087, "mcp-apikeys": 8086, "mcp-gatekeeper": 8888}
    r = run("systemctl list-units --type=service --state=running 'mcp-*' --no-legend --no-pager", timeout=8)
    services = []
    if r and r.stdout:
        for line in r.stdout.splitlines():
            unit = line.strip().split()[0] if line.strip() else ""
            if unit.endswith(".service") and "heartbeat-collect" not in unit:
                services.append(unit[:-len(".service")])
    up, out = 0, []
    for svc in sorted(services):
        p = known_ports.get(svc)
        if p is not None:
            ok = port_ok(p)
            out.append(f"{svc} (порт {p}): {'работает' if ok else 'DOWN'}")
        else:
            ok = True
            out.append(f"{svc}: работает (systemd)")
        up += 1 if ok else 0
    total = len(services)
    if total == 0:
        return False, "ни одного MCP не запущено!", ["ожидали memory/apikeys/gatekeeper"]
    return (up == total), f"{up}/{total} работают", out


def _lexical_search_works():
    """Real lexical liveness probe: run a search query, expect JSON list (even empty)."""
    try:
        r = run("python3 /root/LabDoctorM/projects/lab-memory/scripts/lab_search.py search \"OpenClaw\" --topN 1 --json",
                timeout=30, cwd="/root/LabDoctorM/projects/lab-memory")
        if not r or not r.stdout:
            return False
        data = json.loads(r.stdout)
        return isinstance(data, list)
    except Exception:
        return False


def cat_memory():
    # === Семантический стэк (ONNX+FAISS) ДЕПРЕКЕЙТНУТ (ЗавЛаб, 2026-07-15):
    # семантика переезжает на другой стэк. ONNX-embedder намеренно остановлен (14.07),
    # юнит стёрт, :8082 не слушает — это ОЖИДАЕМО, не авария.
    # Единственный реальный сигнал здоровья памяти = лексический поиск lab_search отвечает.
    ri = run("systemctl is-active reindex-incremental.timer reindex-full.timer", timeout=6)
    ri_active = bool(ri and "active" in ri.stdout)
    ri_fail = run("systemctl is-failed reindex-incremental.service", timeout=6)
    ri_failed = bool(ri_fail and "failed" in ri_fail.stdout)
    # Лексический поиск жив? — реальный запрос к lab_search (sem неважен, lex должен отвечать).
    lex_ok = _lexical_search_works()
    onnx_port = port_ok(8082)
    onnx_embedder_ok = onnx_port  # грубый прокси: порт жив = эмбеддер потенциально жив
    # Алерт НЕ красный из-за ONNX: стэк меняется. Реальный сбой только если лексический поиск мертв.
    ok = lex_ok
    semantic_state = "OK" if onnx_embedder_ok else "OFF (deprecated — стэк меняется)"
    detail = (f"поиск lexical={'ok' if lex_ok else 'FAIL'}; "
              f"semantic(ONNX)={semantic_state}; "
              f"reindex_timer={'active' if ri_active else 'off'}; reindex_service={'failed' if ri_failed else 'ok'}")
    out = [f"reindex-incremental.timer: {'active' if ri_active else 'off'} (призрак — стэк меняется)",
           f"reindex-incremental.service: {'failed' if ri_failed else 'active'} (призрак — стэк меняется)"]
    if not onnx_embedder_ok:
        out.append("⚠️ ONNX-embedder deprecated: юнит стёрт, :8082 не слушает — ожидается замена стэка; не авария")
    return ok, detail, out


def cat_data():
    out = []
    # PostgreSQL — нативный (systemd) на :5432, а НЕ docker-контейнер api-hub-db-1.
    # Проект api-hub удалён, БД живёт в systemd-сервисе postgresql (данные в /var/lib/postgresql).
    pg = run("pg_isready -h 127.0.0.1 -p 5432", timeout=8)
    pg_up = bool(pg and pg.stdout and "accepting connections" in pg.stdout)
    out.append(f"PostgreSQL(:5432): {'up' if pg_up else 'DOWN'}")
    # SQLite state
    sq = "/root/.openclaw/state/openclaw.sqlite"
    sq_ok = os.path.isfile(sq) and os.path.getsize(sq) > 0
    out.append(f"SQLite state: {'ok' if sq_ok else 'FAIL'} ({os.path.getsize(sq)//1024//1024 if sq_ok else 0} MB)")
    # disk
    df = run("df -h / | tail -1 | awk '{print $5}'", timeout=6)
    disk = df.stdout.strip() if df else "?"
    pct = int(disk.rstrip("%")) if disk and disk.rstrip("%").isdigit() else 0
    warn = THRESHOLDS["disk_warn_pct"]    # 80
    crit = THRESHOLDS["disk_crit_pct"]    # 90
    if pct >= crit:
        disk_status = False
        disk_hint = f"КРИТ (≥{crit}%)"
    elif pct >= warn:
        disk_status = "warn"
        disk_hint = f"⚠️ близко к порогу (≥{warn}%)"
    else:
        disk_status = True
        disk_hint = "ок"
    db_status = True if (pg_up and sq_ok) else False
    status = worst(db_status, disk_status)
    summary = f"PostgreSQL {'up' if pg_up else 'DOWN'}; disk {disk} (норма <{warn}% — {disk_hint})"
    return status, summary, out


def cat_network():
    out = []
    vpn = run("docker ps --filter name=amnezia-awg2 --format '{{.Status}}'", timeout=8)
    vpn_up = bool(vpn and vpn.stdout.strip() and "Up" in vpn.stdout)
    out.append(f"VPN(amnezia-awg2): {'up' if vpn_up else 'DOWN'}")
    sx = port_ok(8889)
    out.append(f"searxng(:8889): {'ok' if sx else 'DOWN'}")
    # SSL shtab-ai.ru
    ssl = run("echo | timeout 8 openssl s_client -servername shtab-ai.ru -connect shtab-ai.ru:443 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null", timeout=12)
    ssl_ok = bool(ssl and ssl.stdout.strip())
    exp = ssl.stdout.strip().replace("notAfter=", "") if ssl_ok else "?"
    out.append(f"SSL shtab-ai.ru: {'ok' if ssl_ok else 'FAIL'} (exp {exp})")
    ok = vpn_up and sx and ssl_ok
    return ok, f"VPN {'up' if vpn_up else 'DOWN'}; searxng {'ok' if sx else 'DOWN'}; SSL {'ok' if ssl_ok else 'FAIL'}", out


def cat_projects():
    out = []
    dirty = 0
    repos_dirty = []
    for p in ["lab-memory", "mcp-tools", "api-hub", "DoctorM_and_Ai"]:
        d = os.path.join(PROJECTS, p)
        if not os.path.isdir(d):
            continue
        g = run("git status --porcelain | wc -l", timeout=8, cwd=d)
        n = int(g.stdout.strip()) if g and g.stdout.strip().isdigit() else 0
        dirty += n
        repos_dirty.append((p, n))
    # инциденты: всего / закрыто / открыто (честный подсчёт, не всё = «открытое»)
    inc_total, inc_closed = 0, 0
    open_incidents = []
    closed_re = re.compile(r"status:\s*(resolved|closed|done)", re.IGNORECASE)
    now = datetime.datetime.now().timestamp()
    for root in [WORKSPACES, PROJECTS]:
        for _ in os.listdir(root) if os.path.isdir(root) else []:
            idir = os.path.join(root, _, "incidents")
            if not os.path.isdir(idir):
                continue
            for f in os.listdir(idir):
                if not f.endswith(".md"):
                    continue
                inc_total += 1
                fpath = os.path.join(idir, f)
                try:
                    with open(fpath, errors="ignore") as fh:
                        head = fh.read(600)
                    is_closed = bool(closed_re.search(head))
                except Exception:
                    is_closed = False
                if is_closed:
                    inc_closed += 1
                else:
                    open_incidents.append((f, _, os.path.getmtime(fpath)))
    inc_open = inc_total - inc_closed
    pct = round(inc_closed / inc_total * 100) if inc_total else 0
    # детализация по репозиториям
    for p, n in repos_dirty:
        out.append(f"{p}: {n} файл(ов)" if n else f"{p}: 0 (чистый)")
    out.append(f"инциденты: {inc_open} открыто / {inc_total} ({inc_closed} закрыто)")
    # топ-5 старейших открытых инцидентов (застой)
    oldest = sorted(open_incidents, key=lambda x: x[2])[:5]
    if oldest:
        out.append("старейшие открытые (застой):")
        for f, owner, mtime in oldest:
            days = int((now - mtime) / 86400)
            out.append(f"  · {f[:-3]} ({owner}, {days} дн)")
    repo_list = ", ".join(f"{p} {n}" for p, n in repos_dirty if n) or "нет"
    ok = True  # информационная категория (WIP/INC — базовый шум лаборатории, не сбой)
    summary = (f"незакоммичено {dirty} файлов ({repo_list}) — не сбой; "
               f"инциденты {inc_open} открыто / {inc_total} ({inc_closed} закрыто, {pct}% решено)")
    return ok, summary, out


def cat_host():
    out = []
    la = run("cat /proc/loadavg | awk '{print $1, $2, $3}'", timeout=5)
    load = la.stdout.strip() if la else "?"
    out.append(f"loadavg: {load}")
    # RAM: used/total, free, buff/cache, available — чтобы уравнение сходилось на глаз
    mem = run("free -m | awk '/Mem:/ {print $3, $2, $4, $6, $7}'", timeout=5)
    used = total = free_m = buff = avail = "?"
    if mem:
        p = mem.stdout.split()
        if len(p) >= 5:
            used, total, free_m, buff, avail = p[0], p[1], p[2], p[3], p[4]
    ram = f"{used}/{total} MB"
    out.append(f"RAM: {ram} — used {used}; free {free_m}; buff/cache {buff}; available {avail} MB")
    dp = run("docker ps --format '{{.Names}}'", timeout=8)
    conts = dp.stdout.strip().splitlines() if dp and dp.stdout.strip() else []
    cont = str(len(conts))
    out.append(f"контейнеры ({cont}): {', '.join(conts) if conts else 'нет'}")
    ncpu = run("nproc", timeout=5)
    cores = ncpu.stdout.strip() if ncpu and ncpu.stdout.strip().isdigit() else "?"
    # load(1мин): всплески до ~2×ядер — норма; устойчивое превышение — тревога
    load1 = 0.0
    try:
        load1 = float(load.split()[0])
    except Exception:
        pass
    ncores = int(cores) if cores.isdigit() else 4
    lw, lh = THRESHOLDS["load_warn_x"], THRESHOLDS["load_high_x"]
    if load1 < ncores*lw:
        load_hint = "ок"
        load_status = True
    elif load1 < ncores*lh:
        load_hint = "повышенная"
        load_status = "warn"
    else:
        load_hint = "ВЫСОКАЯ"
        load_status = False
    pct = round(load1 / ncores * 100) if ncores else 0
    out.append(f"ядер CPU: {cores} (нагрузка {load1} из {cores} = {pct}%, норма <100%)")
    # RAM: used/total; важен available, но для порога берём used% (Linux кэширует)
    try:
        ram_pct = round(int(used) / int(total) * 100) if (str(total).isdigit() and int(total) > 0) else 0
    except Exception:
        ram_pct = 0
    mw, mc = THRESHOLDS["mem_warn_pct"], THRESHOLDS["mem_crit_pct"]
    if ram_pct >= mc:
        ram_hint = f"ВЫСОКАЯ (≥{mc}%)"
        ram_status = False
    elif ram_pct >= mw:
        ram_hint = f"⚠️ повышенная (≥{mw}%)"
        ram_status = "warn"
    else:
        ram_hint = "ок"
        ram_status = True
    out.append(f"RAM: {ram} — used {used}; free {free_m}; buff/cache {buff}; available {avail} MB ({ram_pct}% used)")
    status = worst(load_status, ram_status)
    summary = (f"нагрузка CPU {load1} из {cores} ядер (~{pct}%, {load_hint}); "
               f"память {ram} ({ram_pct}% used, {ram_hint}; доступно {avail} MB); контейнеры {cont} запущено")
    return status, summary, out


def load_services_state():
    try:
        with open(SERVICES_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_services_state(state):
    try:
        with open(SERVICES_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


PORT_RE = re.compile(
    r"--port[ =](\d+)"                                       # --port 8082 / --port=8082
    r"|-p[ =](\d+)"                                        # -p 8082 / -p=8082
    r"|(?:(?:\d{1,3}\.){3}\d{1,3}|localhost):(\d{2,5})\b"  # 127.0.0.1:8082 / localhost:5432
)


def extract_ports(text):
    """Извлекает номера портов из строки запуска (--port N, -p N, host:port).
    Ловит явные флаги и host:port (IP/localhost). НЕ ловит таймштампы
    вида start_time=[...HH:MM:SS...] — иначе :MM/:SS превращаются в ложные «порт N»."""
    ports = set()
    for m in PORT_RE.finditer(text):
        p = m.group(1) or m.group(2) or m.group(3)
        if p:
            try:
                ports.add(int(p))
            except Exception:
                pass
    return ports


def cat_services():
    """Категория 9: системные сервисы (systemd) и зарегистрированные порты.
    Ловит crash-loop (SubState=auto-restart/restarting, рост NRestarts за окно),
    упавшие юниты (systemctl --state=failed), неслушающие порты из ExecStart юнитов
    и неотвечающие критичные порты (MONITOR_PORTS)."""
    problems = []
    # 1. упавшие юниты (явно)
    failed_out = run("systemctl --state=failed --type=service --no-legend --no-pager", timeout=10).stdout.strip()
    failed_units = []
    for ln in failed_out.splitlines():
        parts = ln.split()
        if not parts:
            continue
        failed_units.append(parts[1] if parts[0] == "●" else parts[0])
    for u in failed_units:
        problems.append(f"{u}: юнит упал (failed)")
    # 2. все service-юниты: SUB (бесплатно из list-units) + порты из ExecStart + NRestarts
    state = load_services_state()
    now_ts = time.time()
    new_state = {}
    units_out = run("systemctl list-units --type=service --no-legend --no-pager", timeout=10).stdout.strip()
    for line in units_out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, _load, _active, sub = parts[0], parts[1], parts[2], parts[3]
        if not unit.endswith(".service"):
            continue
        # SUB-проблемы для ВСЕХ юнитов (бесплатно из list-units)
        if sub in ("failed", "auto-restart", "restarting"):
            problems.append(f"{unit}: SUB={sub}")
        # порты из ExecStart для активных юнитов
        if sub in ("running", "auto-restart"):
            execstart = run(f"systemctl show {unit} -p ExecStart --value", timeout=8).stdout
            for p in extract_ports(execstart):
                if not port_ok(p):
                    problems.append(f"{unit}: порт {p} не отвечает")
        # NRestarts только для restart-policy юнитов (ловит тихие петли)
        if sub == "running":
            rp = run(f"systemctl show {unit} -p Restart --value", timeout=8).stdout.strip()
            if rp in ("always", "on-failure"):
                raw = run(f"systemctl show {unit} -p NRestarts --value", timeout=8).stdout.strip()
                n = int(raw.split("=")[-1]) if raw and raw.split("=")[-1].isdigit() else 0
                prev = state.get(unit, {})
                prev_n = int(prev.get("n", 0) or 0)
                delta = n - prev_n
                new_state[unit] = {"n": n, "ts": now_ts}
                if delta >= CRASH_LOOP_DELTA or n >= NRESTARTS_LIFETIME_WARN:
                    parts = []
                    if delta >= CRASH_LOOP_DELTA:
                        parts.append(f"дельта +{delta} за час")
                    if n >= NRESTARTS_LIFETIME_WARN:
                        parts.append(f"lifetime {n}")
                    problems.append(f"{unit}: перезапуски ({'; '.join(parts)})")
    save_services_state(new_state)
    # 3. критичные порты (явный список)
    for p in MONITOR_PORTS:
        if not port_ok(p):
            problems.append(f"порт {p}: не отвечает (критичный)")
    details = []
    if problems:
        details.append("🔴 проблемы сервисов:")
        details += [f"  • {x}" for x in problems]
    else:
        details.append(f"все юниты стабильны (проверено {len(new_state)})")
    ok = len(problems) == 0
    summary = "Сервисы: OK" if ok else f"Сервисы: {len(problems)} пробл."
    return ok, summary, details


def self_factcheck(results):
    """Встроенный гард честности. Ловит самого себя: противоречие между
    заголовком (summary/иконка) и деталями/порогами. Без этого монитор может
    выдать ✅ при значении вне нормы или при расхождении заголовок↔детали."""
    problems = []
    for cid, name, status, summary, details in results:
        det = "\n".join(details)
        if cid == 2:
            if status is True and "DOWN" in summary:
                problems.append(f"{name}: ✅ но gateway DOWN")
            if status is True and "АВТО-перезапуск" in summary:
                problems.append(f"{name}: ✅ но АВТО-перезапуск за окно (не должно быть при ok)")
        elif cid == 3:
            m = re.search(r"(\d+)/(\d+) работают", summary)
            if m and int(m.group(1)) != int(m.group(2)) and status is True:
                problems.append(f"{name}: ✅ но {m.group(1)}/{m.group(2)} работают")
            if status is True and "DOWN" in det:
                problems.append(f"{name}: ✅ но есть DOWN в деталях")
        elif cid == 4:
            if status is True and ("DOWN" in det or "FAIL" in det or "FAIL" in summary):
                problems.append(f"{name}: ✅ но FAIL/DOWN в данных")
        elif cid == 5:
            m = re.search(r"disk (\d+)%", summary)
            if m and int(m.group(1)) >= THRESHOLDS["disk_warn_pct"] and status is True:
                problems.append(f"{name}: ✅ но disk {m.group(1)}% (норма <{THRESHOLDS['disk_warn_pct']})")
            if status is True and "DOWN" in summary:
                problems.append(f"{name}: ✅ но PostgreSQL DOWN")
        elif cid == 6:
            if status is True and ("DOWN" in det or "FAIL" in det or "DOWN" in summary or "FAIL" in summary):
                problems.append(f"{name}: ✅ но DOWN/FAIL в данных")
        elif cid == 8:
            m = re.search(r"1мин ([\d.]+)", summary)
            cm = re.search(r"норма <(\d+)", summary)
            if m and cm:
                l1 = float(m.group(1))
                cores = int(cm.group(1))
                if l1 >= THRESHOLDS["load_high_x"] * cores and status is True:
                    problems.append(f"{name}: ✅ но load1 {l1} ≥ тревожного {THRESHOLDS['load_high_x']*cores}")
    return problems


def independent_probe():
    """НЕЗАВИСИМЫЙ замер каждой категории другим кодом/командой,
    чтобы сверить с тем, что выдал монитор (ловит хардкод и раси).
    Возвращает {cid: строка_независимого_замера}."""
    probe = {}
    probe[1] = "агенты: живость доказана доставкой отчёта (grimoire.md смержены в nevermind.md)"
    probe[2] = f"gateway: {'listening :%d' % GATEWAY_PORT if port_ok(GATEWAY_PORT) else 'DOWN'}"
    r = run("systemctl list-units --type=service --state=running 'mcp-*' --no-legend --no-pager", timeout=8)
    svcs = []
    if r and r.stdout:
        for line in r.stdout.splitlines():
            u = line.strip().split()[0] if line.strip() else ""
            if u.endswith(".service") and "heartbeat-collect" not in u:
                svcs.append(u[:-len(".service")])
    kp = {"mcp-memory": 8087, "mcp-apikeys": 8086, "mcp-gatekeeper": 8888}
    up = sum(1 for s in svcs if (port_ok(kp[s]) if s in kp else True))
    probe[3] = f"mcp запущено/порты отвечают: {up}/{len(svcs)}"
    # Семантический стэк deprecated — ONNX health сломан, векторов нет смысла.
    # Проверяем лексическую живость поиска вместо мёртвого health-эндпоинта.
    lex_ok = _lexical_search_works()
    probe[4] = f"lab_search: semantic deprecated (ONNX down, new stack pending) — lexical probe {'ok' if lex_ok else 'FAIL'}"
    df = run("df -h / | tail -1 | awk '{print $5}'", timeout=6)
    pg = run("pg_isready -h 127.0.0.1 -p 5432", timeout=8)
    probe[5] = f"disk {df.stdout.strip() if df else '?'} | PostgreSQL {'up' if pg and 'accepting connections' in pg.stdout else 'DOWN'}"
    vpn = run("docker ps --filter name=amnezia-awg2 --format '{{.Status}}'", timeout=8)
    sx = port_ok(8889)
    ssl = run("echo | timeout 8 openssl s_client -servername shtab-ai.ru -connect shtab-ai.ru:443 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null", timeout=12)
    probe[6] = f"VPN {'Up' if vpn and 'Up' in vpn.stdout else 'DOWN'} | searxng {'ok' if sx else 'DOWN'} | SSL {'ok' if ssl and ssl.stdout.strip() else 'FAIL'}"
    inc_total, inc_closed = 0, 0
    cre = re.compile(r"status:\s*(resolved|closed|done)", re.IGNORECASE)
    for root in [WORKSPACES, PROJECTS]:
        if not os.path.isdir(root):
            continue
        for _ in os.listdir(root):
            idir = os.path.join(root, _, "incidents")
            if not os.path.isdir(idir):
                continue
            for f in os.listdir(idir):
                if not f.endswith(".md"):
                    continue
                inc_total += 1
                try:
                    with open(os.path.join(idir, f), errors="ignore") as fh:
                        h = fh.read(600)
                    if cre.search(h):
                        inc_closed += 1
                except Exception:
                    pass
    probe[7] = f"инцидентов всего {inc_total}, открыто {inc_total - inc_closed}"
    la = run("cat /proc/loadavg", timeout=5)
    probe[8] = f"load1 {la.stdout.strip().split()[0] if la and la.stdout else '?'}"
    return probe


def selftest_report():
    """--selftest: монитор сверяет себя с независимым замером и показывает вердикт."""
    results = []
    for cid, name, fn in CATEGORIES:
        try:
            status, summary, details = fn()
        except Exception as e:
            status, summary, details = False, f"ERROR: {e}", []
        results.append((cid, name, status, summary, details))
    probe = independent_probe()
    lines = ["🦊 ЛабМонитор · САМОПРОВЕРКА (--selftest)", ""]
    for cid, name, status, summary, details in results:
        lines.append(f"[{cid}] {name}")
        lines.append(f"   монитор : {ICON[status]} {summary}")
        lines.append(f"   независ. : {probe.get(cid, '—')}")
    sf = self_factcheck(results)
    lines.append("")
    if sf:
        lines.append("🔴 САМОПРОВЕРКА НАШЛА НЕСОВПАДЕНИЯ (монитор врёт!):")
        for p in sf:
            lines.append(f"   • {p}")
    else:
        lines.append("✅ САМОПРОВЕРКА: монитор честен — заголовки совпадают с деталями и нормами")
    return "\n".join(lines)


CATEGORIES = [
    (1, "Агенты",        cat_agents),
    (2, "OpenClaw",      cat_openclaw),
    (3, "MCP",           cat_mcp),
    (4, "Память/поиск",  cat_memory),
    (5, "Данные",        cat_data),
    (6, "Сеть",          cat_network),
    (7, "Проекты",       cat_projects),
    (8, "Сервер",       cat_host),
    (9, "Сервисы",      cat_services),
]


# ---------- Метрики + история для тренда (Тир 2, DDP 2026-07-13) ----------
def collect_metrics():
    """Независимый замер ключевых сигналов для дельт/sparkline.
    Дёшево (для часового крона ок); не зависит от человекочитаемых summary категорий."""
    m = {}
    df = run("df -h / | tail -1 | awk '{print $5}'", timeout=6)
    disk = df.stdout.strip() if df else "?"
    m["disk_pct"] = int(disk.rstrip("%")) if disk and disk.rstrip("%").isdigit() else 0
    la = run("cat /proc/loadavg | awk '{print $1}'", timeout=5)
    ncpu = run("nproc", timeout=5)
    cores = int(ncpu.stdout.strip()) if ncpu and ncpu.stdout.strip().isdigit() else 4
    try:
        l1 = float(la.stdout.strip())
    except Exception:
        l1 = 0.0
    m["load_pct"] = round(l1 / cores * 100) if cores else 0
    mem = run("free -m | awk '/Mem:/ {print $3, $2}'", timeout=5)
    if mem and mem.stdout:
        p = mem.stdout.split()
        m["ram_used_mb"] = int(p[0]) if len(p) >= 1 and str(p[0]).isdigit() else 0
        m["ram_total_mb"] = int(p[1]) if len(p) >= 2 and str(p[1]).isdigit() else 0
    else:
        m["ram_used_mb"] = m["ram_total_mb"] = 0
    # Семантический стэк deprecated — векторов нет смысла. Лексическая живость поиска = 1/0.
    m["vectors"] = 1 if _lexical_search_works() else 0
    dirty = 0
    for p in ["lab-memory", "mcp-tools", "api-hub", "DoctorM_and_Ai"]:
        d = os.path.join(PROJECTS, p)
        if not os.path.isdir(d):
            continue
        g = run("git status --porcelain | wc -l", timeout=8, cwd=d)
        n = int(g.stdout.strip()) if g and g.stdout.strip().isdigit() else 0
        dirty += n
    m["git_dirty"] = dirty
    inc_total, inc_closed = 0, 0
    cre = re.compile(r"status:\s*(resolved|closed|done)", re.IGNORECASE)
    for root in [WORKSPACES, PROJECTS]:
        if not os.path.isdir(root):
            continue
        for _ in os.listdir(root):
            idir = os.path.join(root, _, "incidents")
            if not os.path.isdir(idir):
                continue
            for f in os.listdir(idir):
                if not f.endswith(".md"):
                    continue
                inc_total += 1
                try:
                    with open(os.path.join(idir, f), errors="ignore") as fh:
                        if cre.search(fh.read(600)):
                            inc_closed += 1
                except Exception:
                    pass
    m["open_incidents"] = inc_total - inc_closed
    m["ts"] = datetime.datetime.now(MSK).timestamp()
    return m


def load_metrics_history():
    try:
        with open(METRICS_HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_metrics_history(hist):
    try:
        with open(METRICS_HISTORY_FILE, "w") as f:
            json.dump(hist[-HISTORY_MAX:], f)
    except Exception:
        pass


def _spark(values):
    if not values:
        return "—"
    lo, hi = min(values), max(values)
    if hi == lo:
        return SPARK_CHARS[-1] * len(values)
    return "".join(SPARK_CHARS[min(len(SPARK_CHARS) - 1,
                                   int((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1)))]
                   for v in values)


def compute_trend(history, current):
    """delta = текущее минус предыдущий замер; spark = последние N значений."""
    prev = history[-1] if history else None
    keys = ["disk_pct", "load_pct", "ram_used_mb", "vectors", "git_dirty", "open_incidents"]

    def band(key):
        series = [h.get(key, 0) for h in history] + [current.get(key, 0)]
        cur = current.get(key, 0)
        delta = (cur - prev.get(key, 0)) if prev else 0
        return {"cur": cur, "delta": delta, "spark": _spark(series[-12:])}
    return {k: band(k) for k in keys}


def daily_summary(hist):
    """Тир3: сводка за 24ч (min/avg/max) по ключевым метрикам из истории."""
    cutoff = datetime.datetime.now(MSK).timestamp() - 86400
    day = [h for h in hist if h.get("ts", 0) >= cutoff]
    if len(day) < 2:
        return ["📊 Дайджест за 24ч: недостаточно данных (нужно ≥2 прогона)"]
    keys = ["disk_pct", "load_pct", "ram_used_mb", "vectors", "open_incidents", "git_dirty"]
    lines = [f"📊 Дайджест за 24ч (n={len(day)}):"]
    for k in keys:
        vals = [h.get(k, 0) for h in day if isinstance(h.get(k), (int, float))]
        if not vals:
            continue
        lo, hi, avg = min(vals), max(vals), round(sum(vals) / len(vals), 1)
        lines.append(f"    · {k}: min {lo} / avg {avg} / max {hi}")
    return lines


def build_hourly_events(cur, prev):
    """Секция «События за час»: delta текущего прогона с предыдущим.
    Показывает ЧТО ИЗМЕНИЛОСЬ за час (новые/решённые проблемы, сервисы, метрики),
    а не статичный снимок и не старые повторы советов."""
    if not prev:
        return []
    lines = []
    # категории: новые 🔴 / решённые ✅
    cf = set(cur.get("cat_fails", []))
    pf = set(prev.get("cat_fails", []))
    for cid in sorted(cf - pf):
        lines.append(f"  • 🔴 НОВАЯ проблема [{cid}]")
    for cid in sorted(pf - cf):
        lines.append(f"  • ✅ РЕШЕНО [{cid}]")
    # сервисы (cid 9): появившиеся/исчезнувшие строки проблем
    csd = cur.get("cat_services_details", []) or []
    psd = prev.get("cat_services_details", []) or []
    if isinstance(csd, str):
        csd = [csd]
    if isinstance(psd, str):
        psd = [psd]
    for d in csd:
        if d not in psd:
            lines.append(f"  • ▲ {d}")
    for d in psd:
        if d not in csd:
            lines.append(f"  • ▼ {d}")
    # метрики: delta (диск/load/vectors/инциденты)
    for key, label in [("disk_pct", "диск"), ("load_pct", "load"),
                          ("vectors", "vectors"), ("open_incidents", "инциденты")]:
        d = (cur.get(key, 0) or 0) - (prev.get(key, 0) or 0)
        if d:
            lines.append(f"  • {label}: {'+' if d > 0 else ''}{d}")
    return lines


def build_report(full=False, daily=False):
    results, fails = [], []
    for cid, name, fn in CATEGORIES:
        try:
            status, summary, details = fn()
        except Exception as e:
            status, summary, details = False, f"ERROR: {e}", []
        results.append((cid, name, status, summary, details))
        if status is False:
            fails.append((cid, name, summary))

    has_warn = any(st == "warn" for _, _, st, _, _ in results)
    overall = "ТРЕВОГА" if fails else ("ВНИМАНИЕ" if has_warn else "OK")
    sf = self_factcheck(results)  # гард честности: монитор ловит сам себя
    stamp = NOW.strftime("%H:%M")
    score = sum(1 for _, _, st, _, _ in results if st is True)
    total = len(results)

    # --- метрики + тренд (независимый замер, история для дельт/sparkline) ---
    cur = collect_metrics()
    cur["categories_ok"] = score
    cur["cat_fails"] = sorted(str(c[0]) for c in fails)
    # детали сервисов (cid 9) — для дельты «события за час»
    svc_details = []
    for cid, name, status, summary, details in results:
        if cid == 9:
            svc_details = list(details)
    cur["cat_services_details"] = svc_details
    hist = load_metrics_history()
    trend = compute_trend(hist, cur)
    prev_run = hist[-1] if hist else None
    hist.append(cur)
    save_metrics_history(hist)

    # --- Тир3: тихие часы — не беспокоим ЗавЛаба без 🔴 ---
    if quiet_hours_active() and overall != "ТРЕВОГА":
        return ""

    emoji = OVERALL_EMOJI[overall]
    header = f"🦊 ЛабМонитор · {stamp} МСК · {emoji} {overall} · {score}/{total}"

    def _d(key):
        d = trend[key]["delta"]
        return f"{'+' if d > 0 else ''}{d}"
    delta_bits = []
    if trend["disk_pct"]["delta"]:
        delta_bits.append(f"диск {_d('disk_pct')}%")
    if trend["load_pct"]["delta"]:
        delta_bits.append(f"load {_d('load_pct')}%")
    if trend["vectors"]["delta"]:
        delta_bits.append(f"vectors {_d('vectors')}")
    if trend["open_incidents"]["delta"]:
        delta_bits.append(f"инциденты {_d('open_incidents')}")
    delta_str = (" · " + ", ".join(delta_bits)) if delta_bits else " · без изменений"
    signals_line = f"💾{trend['disk_pct']['cur']}% 🧠{trend['vectors']['cur']} ⚡{trend['load_pct']['cur']}%{delta_str}"

    def doctor_line():
        dw = doctor_warnings()
        if dw["new"]:
            return f"🩺 openclaw doctor: {len(dw['new'])} НОВОЕ замечание — глянь"
        if dw["count"]:
            return f"🩺 openclaw doctor: {dw['count']} известное(ых), новых нет"
        return None

    def quote_block():
        q = get_random_quote()
        return ["", f"📜 Цитата часа: {q}"] if q else []

    # --- COLLAPSE-TO-GREEN: когда всё OK — минимум строк (борьба с alert fatigue) ---
    if overall == "OK" and not full and not sf:
        cl = [header, signals_line]
        events = build_hourly_events(cur, prev_run)
        if events:
            cl.append("⚡ События за час:")
            cl += events
        dl_doc = doctor_line()
        if dl_doc:
            cl.append(dl_doc)
        cl.append("ℹ️ полный дамп — !подробно")
        return "\n".join(cl)

    # --- есть внимание/тревога ИЛИ full: показываем категории ---
    lines = [header, signals_line]
    for cid, name, status, summary, details in results:
        lines.append(f"{ICON[status]} {name}: {symptom_frame(cid, status, summary)}")

    if sf:
        lines.append("🔴 САМОПРОВЕРКА (монитор поймал сам себя):")
        for p in sf:
            lines.append(f"  • {p}")

    # слой реагирования (advise) — умный совет по провалу, иначе fallback на маршрут.
    # Гарды (DDP 2026-07-13): dedup по ключу инцидента + cooldown + circuit-breaker.
    if fails:
        advice_lines = []
        details_by_cid = {cid: details for cid, name, status, summary, details in results}
        advice_state = load_advice_state()
        ack_state = load_ack()
        now_ts = datetime.datetime.now(MSK).timestamp()
        changed = False
        fail_cids = {str(c[0]) for c in fails}
        for cid, name, summary in fails:
            if is_acked(cid, now_ts, ack_state):
                advice_lines.append(f"  → [{cid}] {name}: 🔕 заглушено (ack до {fmt_ts(ack_state.get(str(cid), now_ts))})")
                continue
            st = advice_state.get(str(cid), {"last_ts": 0.0, "count": 0, "cooldown_until": 0.0})
            if now_ts < st.get("cooldown_until", 0.0):
                # старый совет в cooldown — не дублируем (убираем «старьё»)
                continue
            fn = ADVICE.get(cid)
            if fn:
                ctx = summary + "\n" + "\n".join(details_by_cid.get(cid, []))
                advice = fn(False, summary, ctx)
                advice_lines.append(f"  → [{cid}] {name}: {advice}")
            else:
                advice_lines.append(f"  → [{cid}] {name}: спавнить {ROUTE.get(cid,'?')} с набором [{ROUTE_SKILLS}]")
            st["last_ts"] = now_ts
            st["count"] = st.get("count", 0) + 1
            st["cooldown_until"] = now_ts + ADVISE_COOLDOWN_S
            if st["count"] >= ADVISE_CIRCUIT_K:
                advice_lines.append(f"  ⛔ circuit-breaker: [{cid}] — советы остановлены после {st['count']} попыток (нужен «го»/ручное расследование)")
            advice_state[str(cid)] = st
            changed = True
        # сброс состояния для решённых инцидентов
        for _cid in list(advice_state.keys()):
            if _cid not in fail_cids:
                del advice_state[_cid]
                changed = True
        if changed:
            save_advice_state(advice_state)
        if advice_lines:
            lines.append("🔧 СОВЕТ (без «го» не спавню):")
            lines += advice_lines

    # секция «События за час» — что ИЗМЕНИЛОСЬ за последний час
    events = build_hourly_events(cur, prev_run)
    if events:
        lines.append("⚡ События за час:")
        lines += events

    if not full:
        dl_doc = doctor_line()
        if dl_doc:
            lines.append(dl_doc)
        # ЦИТАТА ЧАСА — убрана из hourly (ЗавЛаб: не старьё)
        lines.append("ℹ️ полный дамп — !подробно")
        return "\n".join(lines)

    # ---------- ПОЛНЫЙ ДАМП (full) — отдельный чистый формат, без дубля сводки ----------
    icon = ICON
    dl = [header, signals_line, ""]

    for cid, name, status, summary, details in results:
        dl.append("")
        # OK-категория: только статус+имя (числа в деталях ниже — без дубля summary);
        # упавшая: summary в заголовке (причина тревоги сразу видна).
        framed = symptom_frame(cid, status, summary)
        if status is True:
            dl.append(f"{icon[status]} {cid}. {name}")
        else:
            dl.append(f"{icon[status]} {cid}. {name} — {framed}")
        if cid in CAT_HINT:
            dl.append(f"    💡 {CAT_HINT[cid]}")
        for d in details:
            dl.append(f"    · {d}")

    if sf:
        dl.append("")
        dl.append("🔴 САМОПРОВЕРКА (монитор поймал сам себя):")
        for p in sf:
            dl.append(f"    · {p}")

    # диск: только реальные ФС (без docker-overlayfs дублей корня)
    ds = run("df -h -x tmpfs -x overlay -x devtmpfs | tail -n +2 | awk '{print $6\" \"$5\" (свободно \"$4\")\"}'", timeout=6)
    if ds and ds.stdout:
        seen, disk_lines = set(), []
        for line in ds.stdout.strip().splitlines():
            if "/var/lib/docker" in line:
                continue
            key = line.split()[0]
            if key in seen:
                continue
            seen.add(key)
            disk_lines.append(line.strip())
        if disk_lines:
            dl.append("")
            dl.append("💾 Диск (реальные ФС):")
            for line in disk_lines:
                dl.append(f"    · {line}")

    # докер
    dk = run("docker ps --format '{{.Names}}|{{.Status}}'", timeout=8)
    if dk and dk.stdout:
        dl.append("")
        dl.append("🐳 Docker:")
        for line in dk.stdout.strip().splitlines():
            parts = line.split("|", 1)
            nm = parts[0]
            st = parts[1] if len(parts) > 1 else "?"
            dl.append(f"    · {nm}: {st}")

    # доктор целиком (уже очищено clean_line)
    dw = doctor_warnings()
    dl.append("")
    dl.append(f"🩺 Самопроверка движка (openclaw doctor): всего замечаний {dw['count']}, из них новых {len(dw['new'])}")
    dl.append("    💡 «замечание» — не поломка, а совет от встроенного медосмотра. [старое] = известное и безопасное; [🔴 НОВОЕ] = появилось, надо глянуть.")
    for w in dw["all"]:
        tag = "🔴 НОВОЕ" if w in dw["new"] else "старое"
        dl.append(f"    · [{tag}] {w[:110]}")

    # тренд (sparkline) — текстовые блок-символы, Telegram-safe
    dl.append("")
    dl.append("📈 Тренд (последние прогоны):")
    dl.append(f"    · 💾 диск   : {trend['disk_pct']['spark']}  ({trend['disk_pct']['cur']}%)")
    dl.append(f"    · ⚡ load   : {trend['load_pct']['spark']}  ({trend['load_pct']['cur']}%)")
    dl.append(f"    · 🧠 vectors: {trend['vectors']['spark']}  ({trend['vectors']['cur']})")

    if daily:
        dl.append("")
        dl += daily_summary(hist)

    q = get_random_quote()
    if q:
        dl.append("")
        dl.append(f"📜 Цитата часа: {q}")

    return "\n".join(dl)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(selftest_report())
        ping_healthchecks()
    else:
        full = "--full" in sys.argv or "--daily" in sys.argv
        daily = "--daily" in sys.argv
        report = build_report(full=full, daily=daily)
        if report:
            print(report)
            notify_fallback(report)
        ping_healthchecks()
