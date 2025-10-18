#!/usr/bin/env bash
set -euo pipefail

HOST="${MARIADB_HEALTH_HOST:-127.0.0.1}"
PORT="${MARIADB_HEALTH_PORT:-3306}"
DATA_DIR="${MARIADB_HEALTH_DATA_DIR:-/var/lib/mysql}"
WEBHOOK_URL="${MARIADB_HEALTH_WEBHOOK_URL:-}"
REPLICATION_ENABLED="${MARIADB_HEALTH_REPLICATION_ENABLED:-false}"
THREAD_THRESHOLD_RAW="${MARIADB_HEALTH_CONNECTION_THRESHOLD:-}"

notify_webhook() {
  local payload="$1"
  local attempt=1
  local max_attempts=3

  if [[ -z "$WEBHOOK_URL" ]] || ! command -v curl >/dev/null 2>&1; then
    return 0
  fi

  while (( attempt <= max_attempts )); do
    if curl -fsS -X POST \
      -H 'Content-Type: application/json' \
      -d "{\"service\":\"${SERVICE_NAME:-mariadb}\",\"status\":\"unhealthy\",\"message\":\"${payload//\"/\\\"}\"}" \
      "$WEBHOOK_URL"; then
      return 0
    fi
    sleep $(( attempt * 2 ))
    attempt=$(( attempt + 1 ))
  done

  return 0
}

fail() {
  local message="$1"
  echo "$message" >&2
  notify_webhook "$message" || true
  exit 1
}

MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-}"
if [[ -z "$MYSQL_ROOT_PASSWORD" ]]; then
  fail "MYSQL_ROOT_PASSWORD is not available to health check"
fi
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"

if ! mysqladmin ping -h "$HOST" -P "$PORT" -uroot --connect-timeout=5 >/dev/null 2>&1; then
  fail "mysqladmin ping failed"
fi

threads=$(mysql -h "$HOST" -P "$PORT" -uroot -Nse "SHOW GLOBAL STATUS LIKE 'Threads_connected';" 2>/dev/null | awk '{print $2}')
threads=${threads:-0}
if [[ -z "$threads" ]]; then
  fail "Unable to read Threads_connected"
fi

max_connections=$(mysql -h "$HOST" -P "$PORT" -uroot -Nse "SHOW VARIABLES LIKE 'max_connections';" 2>/dev/null | awk '{print $2}')
max_connections=${max_connections:-0}
if [[ -z "$max_connections" || "$max_connections" == "0" ]]; then
  fail "Unable to determine max_connections"
fi

threshold="$THREAD_THRESHOLD_RAW"
if [[ -z "$threshold" ]]; then
  threshold=$(( max_connections ))
fi
if ! [[ "$threshold" =~ ^[0-9]+$ ]]; then
  fail "MARIADB_HEALTH_CONNECTION_THRESHOLD must be an integer"
fi
if (( threshold < 1 )); then
  threshold=1
fi

if (( threads >= max_connections )); then
  fail "Threads_connected $threads reached configured max_connections $max_connections"
fi

if (( threads >= threshold )); then
  fail "Threads_connected $threads exceeded alert threshold $threshold"
fi

disk_pct=$(df --output=pcent "$DATA_DIR" 2>/dev/null | tail -1 | tr -dc '0-9')
disk_pct=${disk_pct:-0}
if (( disk_pct >= ${MARIADB_HEALTH_DISK_THRESHOLD:-90} )); then
  fail "Disk utilization ${disk_pct}% at $DATA_DIR exceeds ${MARIADB_HEALTH_DISK_THRESHOLD:-90}% threshold"
fi

if [[ "$REPLICATION_ENABLED" == "true" ]]; then
  replication_status=$(mysql -h "$HOST" -P "$PORT" -uroot -e "SHOW REPLICA STATUS\\G" 2>/dev/null || true)
  if [[ -z "$replication_status" ]]; then
    replication_status=$(mysql -h "$HOST" -P "$PORT" -uroot -e "SHOW SLAVE STATUS\\G" 2>/dev/null || true)
  fi
  if [[ -z "$replication_status" ]]; then
    fail "Replication status unavailable"
  fi
  if ! grep -q "Slave_IO_Running: Yes" <<<"$replication_status"; then
    fail "Replication IO thread not running"
  fi
  if ! grep -q "Slave_SQL_Running: Yes" <<<"$replication_status"; then
    fail "Replication SQL thread not running"
  fi
fi

echo "ok"
