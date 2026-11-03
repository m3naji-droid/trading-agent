#!/usr/bin/env bash
# يعمل كل دقيقتين: إذا وُجد تحديث جديد في GitHub يحمّله ويعيد بناء الوكيل.
# ملف .env ومجلد data (الذاكرة والصفقات) لا يتأثران أبداً.
cd "$(dirname "$0")" || exit 1
exec 9>/tmp/agent-update.lock; flock -n 9 || exit 0
git fetch -q origin main || exit 0
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  git reset -q --hard origin/main
  git log -1 --format='%h · %cd' --date=format:'%Y-%m-%d %H:%M' > VERSION
  docker compose --env-file .env up -d --build >> /var/log/agent-update.log 2>&1
  echo "$(date) updated to $(cat VERSION)" >> /var/log/agent-update.log
fi
