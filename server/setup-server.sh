#!/bin/bash
# Ubuntu/Debian serverda ISHGA TUSHIRING (root yoki sudo): bash setup-server.sh
# Oldin: repo klon qilingan (git clone <repo> /opt/manhwa-bot4) va server/bot.env shu papkaga ko'chirilgan.
set -e
APP=/opt/manhwa-bot4
cd "$APP"
apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip git curl libgl1 libglib2.0-0
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
cp server/manhwa-bot.service /etc/systemd/system/manhwa-bot.service
systemctl daemon-reload && systemctl enable --now manhwa-bot
echo "Tayyor. Log: journalctl -u manhwa-bot -f"
