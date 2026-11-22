#!/bin/sh
set -e
DB_DIR="$(dirname "${LOGISTICS_DB:-/data/logistics.db}")"
mkdir -p "$DB_DIR"
python /app/scripts/bootstrap_admin.py
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"