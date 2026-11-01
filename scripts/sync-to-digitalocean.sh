#!/usr/bin/env bash
# Push local SQLite DBs + frame folders to the DigitalOcean droplet (option B).
# Scouts stay on this Mac; cloud dashboard/analyzer/trends read the synced copy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IP="${RTA_DROPLET_IP:-139.59.43.4}"
SSH_KEY="${RTA_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
RSYNC_SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new"

cd "$ROOT"
for db in data/app.db data/concert.db data/fitness.db; do
  [[ -f "$db" ]] || continue
  sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
done

rsync -az --delete \
  -e "$RSYNC_SSH" \
  data/app.db data/concert.db data/fitness.db data/tmp \
  "root@$IP:/opt/reel-trend-agent/data/"

# Soft-restart analyzers so they reopen DBs after replace (dashboard re-reads each request).
"${SSH[@]}" "root@$IP" 'systemctl try-reload-or-restart reel-analyzer@street_food reel-analyzer@concert reel-analyzer@fitness reel-dashboard || true'
echo "Synced to http://$IP/  $(date -u +%Y-%m-%dT%H:%MZ)"
