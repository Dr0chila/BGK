#!/bin/bash
# Ежедневный дамп базы. Ставится таймером: deploy/bgk-backup.{service,timer}.
# Восстановить: gunzip -c FILE.sql.gz | sudo -u postgres psql bgk   (при остановленном боте)
set -euo pipefail
DIR=/root/bgk-bot/backups/db
KEEP_DAYS=14
mkdir -p "$DIR"
FILE="$DIR/bgk_$(date +%F_%H%M).sql.gz"
# Пишем во временный файл: оборванный дамп не должен выглядеть как целый
sudo -u postgres pg_dump --clean --if-exists bgk | gzip > "$FILE.part"
mv "$FILE.part" "$FILE"
find "$DIR" -name 'bgk_*.sql.gz' -mtime +$KEEP_DAYS -delete
echo "ok: $FILE ($(du -h "$FILE" | cut -f1))"
