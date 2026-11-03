#!/usr/bin/env bash
# إعادة بناء وتشغيل الوكيل بعد تعديل الإعدادات أو الكود (الذاكرة لا تُمسح)
cd "$(dirname "$0")"
docker compose --env-file .env up -d --build
docker compose logs --tail=30 app
