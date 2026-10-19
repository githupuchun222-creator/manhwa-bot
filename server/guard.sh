#!/bin/bash
# Kuzatuvchi (2026-10-19): har 5 daqiqada cron. Muammo paydo bo'lganda va tuzalganda egasiga Telegram xabari.
APP=/opt/manhwa-bot4
STATE=/var/lib/manhwa-guard
mkdir -p "$STATE"
set -a; . "$APP/server/bot.env"; set +a
API="${TG_API_BASE:-https://api.telegram.org}/bot$BOT_TOKEN"

tell() {
  curl -s -m 20 -o /dev/null "$API/sendMessage" --data-urlencode "chat_id=$OWNER_ID" --data-urlencode "text=$1"
}

# check <nom> <muammo bormi 0/1> <matn>: holat o'zgargandagina xabar (har 5 daqiqada takror yozmaydi)
check() {
  local f="$STATE/$1" was=0
  [ -f "$f" ] && was=1
  if [ "$2" = 1 ] && [ "$was" = 0 ]; then touch "$f"; tell "⚠️ Server: $3"
  elif [ "$2" = 0 ] && [ "$was" = 1 ]; then rm -f "$f"; tell "✅ Server: tuzaldi - $3"
  fi
}

down=0; systemctl is-active -q manhwa-bot || down=1
[ -f "$STATE/paused" ] && down=0      # egasi so'rovi bilan ataylab to'xtatilgan (pause/davom: server/pause.sh)
check bot_down $down "@manhwatarjima_bot ishlamayapti (systemd qayta yoqishga urinmoqda)"

# shortcut: o'z Bot API (docker tg-bot-api) o'chsa xabar ham shu orqali ketadi - yuborib bo'lmaydi; docker uni o'zi qayta yoqadi
disk=$(df --output=pcent / | tail -1 | tr -dc 0-9)
check disk $([ "$disk" -ge 85 ] && echo 1 || echo 0) "disk ${disk}% band"

# admins.json butunlay darvozaga (Cloudflare D1, bitta qiymat ~2 MB chegarasi) yoziladi - oshib ketsa saqlanmay qoladi
adm=$(( $(stat -c %s "$APP/admins.json" 2>/dev/null || echo 0) / 1024 ))
check admins_big $([ "$adm" -ge 1700 ] && echo 1 || echo 0) "admins.json ${adm} KB - 2 MB ga yaqin, darvozaga yozilmay qolishi mumkin"

mem=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
check mem $([ "$mem" -lt 400 ] && echo 1 || echo 0) "bo'sh xotira ${mem} MB"

# bot yiqilib o'zi qayta yongan bo'lsa (xotira tugashi va h.k.) - har safar xabar
n=$(systemctl show -p NRestarts --value manhwa-bot)
old=$(cat "$STATE/restarts" 2>/dev/null || echo "$n")
echo "$n" > "$STATE/restarts"
# restart.flag bilan rejali qayta yonish (yangi kod) - yiqilish emas
last=$(grep -h "yangi kod bilan qayta" "$APP"/logs/bot.log* 2>/dev/null | tail -1 | cut -c1-19)
planned=0
[ -n "$last" ] && [ $(( $(date +%s) - $(date -d "$last" +%s) )) -lt 600 ] && planned=1
if [ "$n" -gt "$old" ] && [ "$planned" = 0 ]; then
  why=$(journalctl -u manhwa-bot -n 200 --no-pager 2>/dev/null | grep -iE 'oom|killed|error|exception' | tail -2 | cut -c1-300)
  tell "🔁 Server: bot $((n - old)) marta yiqilib, o'zi qayta yondi.${why:+
$why}"
fi
