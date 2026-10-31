#!/usr/bin/env bash
# Demo backup: copy the live DB every 30 min.   crontab:  */30 * * * * /path/to/reel-trend-agent/scripts/snapshot_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
# sqlite3 .backup is safe while other processes write (WAL); fall back to cp
if command -v sqlite3 >/dev/null; then
  sqlite3 data/app.db ".backup backups/app-$(date +%H%M).db"
else
  cp data/app.db "backups/app-$(date +%H%M).db"
fi
echo "saved backups/app-$(date +%H%M).db"
