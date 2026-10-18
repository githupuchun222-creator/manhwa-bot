#!/bin/bash
# phone/b4.env (sirlar shu yerda) dan 4 yadro / 8 GB server uchun server/bot.env yasaydi. Faylni GitHub'ga PUSH QILMANG.
set -e
cd "$(dirname "$0")"
grep -vE '^(RUNNER_ROLE|PARALLEL_JOBS|TG_API_ID_OPT|TG_API_HASH_OPT|POOL_SLOTS|LAMA_THREADS|OCR_THREADS|CONTACT_USERNAME)=' ../phone/b4.env > bot.env
cat >> bot.env <<EOT
RUNNER_ROLE=primary
CONTACT_USERNAME=Ibragimov_015
TG_API_ID=31168857
TG_API_HASH=cf0be941272089d2bfc2d8b9215f051d
# 4 yadro / 8 GB: 2 ta bob bir vaqtda (har biri ~2-3 GB), LaMa 2 oqim, OCR 1 oqim - yadrolar bir-biriga xalaqit bermasin
PARALLEL_JOBS=2
POOL_SLOTS=2
LAMA_THREADS=2
OCR_THREADS=1
ARCHIVE_KEEP=25
EOT
echo "server/bot.env tayyor"
