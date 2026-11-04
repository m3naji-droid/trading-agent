#!/usr/bin/env bash
# تثبيت الوكيل بأمر واحد على سيرفر Ubuntu جديد
set -e
cd "$(dirname "$0")"

echo "== 1/4 تثبيت Docker =="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

echo "== 2/4 الإعدادات =="
if [ ! -f .env ]; then
  cp .env.example .env
  read -rp "اختر كلمة مرور للوحة التحكم: " PW </dev/tty
  sed -i "s|^DASHBOARD_PASSWORD=.*|DASHBOARD_PASSWORD=${PW}|" .env
fi
IP=$(curl -fsS https://api.ipify.org || curl -fsS ifconfig.me)
DOMAIN="$(echo "$IP" | tr '.' '-').sslip.io"
grep -q '^DOMAIN=' .env && sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" .env || echo "DOMAIN=${DOMAIN}" >> .env
mkdir -p data
rm -f /etc/cron.d/trading-agent-update  # لم يعد مطلوباً: الوكيل يحدّث نفسه

echo "== 3/4 الجدار الناري =="
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null || true
fi

echo "== 4/4 تشغيل الوكيل =="
docker compose --env-file .env up -d --build

echo ""
echo "✅ تم! افتح لوحة التحكم من جوالك:"
echo "   https://${DOMAIN}"
echo "   (قد تحتاج الشهادة دقيقة أو دقيقتين في أول مرة)"
echo ""
echo "عنوان IP السيرفر (لتقييد مفتاح Binance لاحقاً): ${IP}"
