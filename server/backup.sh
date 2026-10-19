#!/bin/bash
# Kunlik zaxira (2026-10-19): admins.json (obunalar, buyurtmalar, paketlar) egasiga Telegram'da hujjat bo'lib boradi
# + serverda oxirgi 7 kunlik nusxa. Contabo'da avtomatik zaxira yo'q.
APP=/opt/manhwa-bot4
DIR=/var/backups/manhwa
mkdir -p "$DIR"
set -a; . "$APP/server/bot.env"; set +a
API="${TG_API_BASE:-https://api.telegram.org}/bot$BOT_TOKEN"

f="$DIR/admins-$(date +%Y%m%d).json.gz"
gzip -c "$APP/admins.json" > "$f" || exit 1
gzip -t "$f" || exit 1
find "$DIR" -name 'admins-*.json.gz' -mtime +7 -delete
curl -s -m 120 -o /dev/null -w '%{http_code}\n' "$API/sendDocument" -F "chat_id=$OWNER_ID" \
  -F "document=@$f" -F "caption=💾 Kunlik zaxira: admins.json ($(date '+%Y-%m-%d')). Server buzilsa shu fayldan tiklanadi." \
  -F "disable_notification=true"
