#!/bin/bash
# Автоматический бэкап PostgreSQL — все пользовательские БД
# Запускается через systemd timer каждые 6 часов

set -euo pipefail

BACKUP_DIR="/var/backups/postgresql"
RETENTION_DAYS=7
LOG="/var/log/pg_backup.log"
LOG_MAX_SIZE=$((10 * 1024 * 1024))  # 10 MB — максимальный размер лога
LOG_ROTATE_COUNT=5                   # хранить 5 ротированных логов
MIN_FREE_SPACE_GB=5                  # минимум свободного места (GB)
DATE=$(date +%Y%m%d_%H%Ms%S)
HOST="${PGHOST:-localhost}"
PORT="${PGPORT:-5432}"

mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

# ─── Ротация логов ───────────────────────────────────────────────────────────

rotate_log() {
    if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX_SIZE" ]; then
        for i in $(seq $((LOG_ROTATE_COUNT - 1)) -1 1); do
            [ -f "${LOG}.$i" ] && mv "${LOG}.$i" "${LOG}.$((i + 1))"
        done
        mv "$LOG" "${LOG}.1"
        touch "$LOG"
    fi
}

rotate_log

# ─── Проверка свободного места ───────────────────────────────────────────────

check_free_space() {
    local free_gb
    free_gb=$(df -BG "$BACKUP_DIR" | awk 'NR==2 {print $4}' | tr -d 'G')
    if [ "$free_gb" -lt "$MIN_FREE_SPACE_GB" ]; then
        log "ERROR: Недостаточно свободного места: ${free_gb}GB < ${MIN_FREE_SPACE_GB}GB"
        exit 1
    fi
    log "Свободно ${free_gb}GB на диске бэкапов"
}

check_free_space

log "=== Начало бэкапа PostgreSQL ==="

# Список БД для бэкапа (исключаем template и system)
DBS=$(sudo -u postgres psql -t -c \
    "SELECT datname FROM pg_database WHERE datistemplate = false AND datname NOT IN ('postgres')" 2>/dev/null || true)

if [ -z "$DBS" ]; then
    log "ERROR: Не удалось получить список БД"
    exit 1
fi

BACKUP_COUNT=0
for DB in $DBS; do
    DB=$(echo "$DB" | xargs)  # trim
    [ -z "$DB" ] && continue

    FILE="$BACKUP_DIR/${DB}_${DATE}.sql.gz"

    log "Бэкап БД: $DB → $FILE"
    if sudo -u postgres pg_dump -Fc "$DB" 2>>"$LOG" | gzip > "$FILE"; then
        SIZE=$(du -h "$FILE" | cut -f1)
        log "OK: $DB ($SIZE)"
        BACKUP_COUNT=$((BACKUP_COUNT + 1))
    else
        log "ERROR: Не удалось сделать бэкап $DB"
        rm -f "$FILE"
    fi
done

# Очистка старых бэкапов
DELETED=$(find "$BACKUP_DIR" -name "*.sql.gz" -mtime +$RETENTION_DAYS -delete -print 2>/dev/null | wc -l)
if [ "$DELETED" -gt 0 ]; then
    log "Очистка: удалено $DELETED старых бэкапов (>$RETENTION_DAYS дней)"
fi

TOTAL=$(find "$BACKUP_DIR" -name "*.sql.gz" | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)

log "=== Бэкап завершён: $BACKUP_COUNT БД, всего $TOTAL файлов ($TOTAL_SIZE) ==="
