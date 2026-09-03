#!/usr/bin/env bash
# Sauvegarde de la base SQLite du backend, avec rotation.
#
# Utilise `sqlite3 .backup` (et non `cp`) pour obtenir un snapshot cohérent
# même si l'application écrit pendant la copie.
#
# Installation (crontab de l'utilisateur applicatif) :
#   crontab -e
#   0 3 * * * /home/nexorus/deriv-bot-backend/backup_db.sh >> /home/nexorus/backups/backup.log 2>&1

set -euo pipefail

CONTAINER="${CONTAINER:-deriv-bot-backend}"
DB_IN_CONTAINER="/app/data/app.db"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/deriv-bot}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

timestamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

target="$BACKUP_DIR/app-$timestamp.db"

# Snapshot cohérent depuis l'intérieur du conteneur, puis extraction.
if docker exec "$CONTAINER" sh -c "command -v sqlite3 >/dev/null 2>&1"; then
  docker exec "$CONTAINER" sqlite3 "$DB_IN_CONTAINER" ".backup '/tmp/backup.db'"
  docker cp "$CONTAINER:/tmp/backup.db" "$target"
  docker exec "$CONTAINER" rm -f /tmp/backup.db
else
  # sqlite3 absent de l'image : repli sur une copie via l'API Python du conteneur.
  docker exec "$CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('$DB_IN_CONTAINER')
dst = sqlite3.connect('/tmp/backup.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
"
  docker cp "$CONTAINER:/tmp/backup.db" "$target"
  docker exec "$CONTAINER" rm -f /tmp/backup.db
fi

gzip -f "$target"
echo "$(date -Is) Sauvegarde OK : ${target}.gz ($(du -h "${target}.gz" | cut -f1))"

# Rotation : supprime les sauvegardes plus vieilles que RETENTION_DAYS.
deleted="$(find "$BACKUP_DIR" -name 'app-*.db.gz' -type f -mtime "+$RETENTION_DAYS" -print -delete | wc -l)"
if [ "$deleted" -gt 0 ]; then
  echo "$(date -Is) Rotation : $deleted sauvegarde(s) de plus de $RETENTION_DAYS jours supprimée(s)"
fi
