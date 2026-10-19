#!/bin/bash
# Disk to'lmasin (2026-10-19): har soatda cron (/etc/cron.d/manhwa-cleanup) ishga tushiradi.
# Arxivni bot o'zi ARCHIVE_KEEP ta bilan cheklaydi; bu yerda qolgan hamma narsa + favqulodda tozalash.
APP=/opt/manhwa-bot4
LIMIT=${CLEAN_LIMIT:-80}   # disk shu foizdan oshsa arxiv 3 tagacha qisqaradi

used() { df --output=pcent / | tail -1 | tr -dc 0-9; }

# 3 kundan eski vaqtinchalik fayllar (systemd xizmatlarining shaxsiy /tmp papkalariga tegilmaydi)
find /tmp /var/tmp -xdev -path '*/systemd-private-*' -prune -o -type f -mtime +3 -print -delete >/dev/null 2>&1
find "$APP" -maxdepth 1 -name '*.tmp' -mmin +60 -delete 2>/dev/null
find "$APP" -name __pycache__ -prune -o -name '*.part' -mmin +1440 -print -delete 2>/dev/null >/dev/null
# o'z Bot API serverimiz yuklangan fayllarni diskda saqlaydi; 12 soatdan eskisi o'chadi
# (bot papkasining ichki papkalari - td.binlog va bazasi 1-darajada, ularga tegilmaydi)
find /var/lib/telegram-bot-api/*/ -mindepth 2 -type f -mmin +720 -delete 2>/dev/null
journalctl --vacuum-size=200M >/dev/null 2>&1
apt-get clean >/dev/null 2>&1
rm -rf /root/.cache/pip

if [ "$(used)" -ge "$LIMIT" ]; then
  logger -t manhwa-cleanup "disk $(used)% - favqulodda tozalash"
  ls -1d "$APP"/archive*/*/ 2>/dev/null | sort | head -n -3 | xargs -r rm -rf
  rm -f "$APP"/logs/*.log.[0-9]
  journalctl --vacuum-size=50M >/dev/null 2>&1
fi
