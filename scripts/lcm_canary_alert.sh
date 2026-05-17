#!/usr/bin/env bash
set -euo pipefail
DB="$HOME/.hermes/context/lcm/lcm.db"
WAL="${DB}-wal"
WARN_WAL=$((256*1024*1024))
WARN_DB=$((1024*1024*1024))

if [[ ! -f "$DB" ]]; then
  exit 0
fi

wal_size=0
[[ -f "$WAL" ]] && wal_size=$(stat -c%s "$WAL")
db_size=$(stat -c%s "$DB")

if (( wal_size > WARN_WAL || db_size > WARN_DB )); then
  echo "LCM CANARY ALERT: db_bytes=${db_size} wal_bytes=${wal_size} thresholds(db>${WARN_DB},wal>${WARN_WAL})"
fi
