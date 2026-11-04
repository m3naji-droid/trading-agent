#!/usr/bin/env bash
# ترقية الوكيل على نفس السيرفر (بدون حذفه)، مع الحفاظ على كلمة المرور والذاكرة.
# الاستخدام: curl -sL https://raw.githubusercontent.com/m3naji-droid/trading-agent/main/setup.sh | bash
set -e
D=/root/trading-agent
REPO=https://github.com/m3naji-droid/trading-agent

apt-get install -y git curl >/dev/null 2>&1 || (apt-get update >/dev/null && apt-get install -y git curl >/dev/null)

if [ -d "$D/.git" ]; then
  echo "== تحديث النسخة الموجودة =="
  cd "$D" && git fetch -q origin main && git reset -q --hard origin/main
else
  if [ -d "$D" ]; then
    echo "== نقل الإعدادات والذاكرة من التثبيت القديم =="
    (cd "$D" && docker compose down >/dev/null 2>&1) || true
    rm -rf /root/trading-agent.old && mv "$D" /root/trading-agent.old
  fi
  git clone -q "$REPO" "$D"
  [ -f /root/trading-agent.old/.env ] && cp /root/trading-agent.old/.env "$D/.env"
  [ -d /root/trading-agent.old/data ] && cp -a /root/trading-agent.old/data "$D/"
fi

cd "$D"
bash install.sh   # يطلب كلمة مرور فقط إذا لم توجد إعدادات سابقة
