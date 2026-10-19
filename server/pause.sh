#!/bin/bash
# Botni egasi aytguncha to'xtatish: bash pause.sh stop | bash pause.sh start
# stop - qo'ldagi boblar tugaydi (restart.flag), keyin xizmat to'xtatiladi; Telegram xabarlari navbatda kutadi.
APP=/opt/manhwa-bot4
STATE=/var/lib/manhwa-guard
mkdir -p "$STATE"
case "$1" in
  stop)
    touch "$STATE/paused" "$APP/restart.flag"
    while [ -f "$APP/restart.flag" ] && systemctl is-active -q manhwa-bot; do sleep 1; done
    systemctl stop manhwa-bot
    logger -t manhwa-pause "bot to'xtatildi"
    ;;
  start)
    rm -f "$STATE/paused" "$APP/restart.flag"
    systemctl start manhwa-bot
    ;;
  *) echo "bash pause.sh stop|start"; exit 1 ;;
esac
