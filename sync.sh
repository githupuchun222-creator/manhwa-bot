#!/usr/bin/env bash
# Asosiy bot kodini (../manhwa-tarjima-bot) shu repoga (@manhwatarjima_bot) ko'chirib, GitHub'ga yuboradi.
set -e
cd "$(dirname "$0")"
for f in admins.py bigfile.py bot.py shop.py bubble_refine.py config.py fast_ocr.py image_editor.py \
         image_utils.py pdf_utils.py translator.py uz_translate.py requirements.txt; do
  cp ../manhwa-tarjima-bot/$f .
done
cp -r ../manhwa-tarjima-bot/assets .
rm -f assets/fonts/digistrip*.ttf
git add -A && git commit -qm "Bot kodini yangilash" && git push -q || echo "O'zgarish yo'q"
