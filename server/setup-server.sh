#!/bin/bash
# Ubuntu/Debian serverda ISHGA TUSHIRING (root yoki sudo): bash setup-server.sh
# Oldin: repo klon qilingan (git clone <repo> /opt/manhwa-bot4) va server/bot.env shu papkaga ko'chirilgan.
set -e
APP=/opt/manhwa-bot4
cd "$APP"
apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git curl unzip cron libgl1 libglib2.0-0
# 8 GB RAM: 32 sahifali bob ~3-4 GB; OOM bo'lmasin deb 4 GB swap
if ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
M=assets/models/lama_fp32.onnx
if [ ! -s "$M" ]; then
  mkdir -p assets/models
  curl -fsSL -o "$M.part" https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx && mv "$M.part" "$M"
fi
# Digital Strip shrifti (litsenziya sabab repoda yo'q; bot.yml dagi kabi)
if ! ls assets/fonts/digistrip*.ttf >/dev/null 2>&1; then
  curl -fsSL -o /tmp/ds.zip "https://dl.dafont.com/dl/?f=digital_strip" && unzip -oj /tmp/ds.zip '*.ttf' -d assets/fonts/ 2>/dev/null || echo "Digital Strip yuklanmadi (ixtiyoriy)"
fi
# disk to'lmasin: jurnal 200 MB bilan cheklanadi + har soatlik tozalash (server/cleanup.sh)
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/size.conf
systemctl restart systemd-journald
cp server/manhwa-cleanup.cron /etc/cron.d/manhwa-cleanup && chmod 644 /etc/cron.d/manhwa-cleanup
# xavfsizlik (2026-10-19): faqat SSH kalit bilan, firewall faqat 22, fail2ban; apt botni o'zi qayta yoqmasin
apt-get install -y -qq ufw fail2ban docker.io
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/00-hardening.conf
sshd -t && systemctl reload ssh
ufw allow 22/tcp && ufw --force enable
printf '[sshd]\nenabled = true\nmaxretry = 5\nbantime = 1h\n' > /etc/fail2ban/jail.d/sshd.local && systemctl restart fail2ban
echo '$nrconf{restart} = "l";' > /etc/needrestart/conf.d/no-auto-restart.conf
# o'z Bot API serverimiz (2 GB gacha fayl): faqat 127.0.0.1 da (docker ufw'ni chetlab o'tadi - tashqariga ochilmasin).
# bot.env da TG_API_BASE=http://127.0.0.1:8081, TG_LOCAL=1, MAX_OUTPUT_MB=1900; birinchi marta bulutdan logOut kerak.
set -a; . server/bot.env; set +a
docker inspect tg-bot-api >/dev/null 2>&1 || docker run -d --name tg-bot-api --restart unless-stopped \
  -p 127.0.0.1:8081:8081 -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
  -e TELEGRAM_API_ID="$TG_API_ID" -e TELEGRAM_API_HASH="$TG_API_HASH" -e TELEGRAM_LOCAL=1 aiogram/telegram-bot-api@sha256:5352e443615ef6711e59e7261f3ef0ad860dae9464905d7c7f3c1e650547d73b
cp server/manhwa-bot.service /etc/systemd/system/manhwa-bot.service
systemctl daemon-reload && systemctl enable --now manhwa-bot
echo "Tayyor. Log: journalctl -u manhwa-bot -f"
