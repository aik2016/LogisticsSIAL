#!/bin/sh
set -e
DB_PATH="${LOGISTICS_DB:-/data/logistics.db}"
DB_DIR="$(dirname "$DB_PATH")"
export LOGISTICS_DB="$DB_PATH"
export PYTHONPATH=/app
LISTEN_PORT="${PORT:-8080}"
cd /app
mkdir -p "$DB_DIR"
echo "logistics: db=$LOGISTICS_DB port=$LISTEN_PORT"
python scripts/bootstrap_admin.py
exec uvicorn app:app --host 0.0.0.0 --port "$LISTEN_PORT"