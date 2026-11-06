"""
الحماية والصيانة:
- 🩺 مراقب خارجي: يرسل "نبضة" كل 5 دقائق لرابط Healthchecks.io (مجاني).
  إذا توقف السيرفر أو علق الوكيل، تتوقف النبضات فيصلك تنبيه من Healthchecks حتى لو كان الوكيل ميتاً.
- 💾 نسخ احتياطي: نسخة يومية تلقائية على السيرفر (آخر 7 أيام) + تنزيلها لجوالك + استعادتها.
"""
import asyncio
import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

from . import memory
from .config import settings

log = logging.getLogger("maint")
BACKUP_DIR = os.path.join(settings.data_dir, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
KEEP = 7
REQUIRED_TABLES = {"trades", "events", "kv", "equity"}


# ------------------------------------------------------------------ المراقب الخارجي
def hc_url():
    return memory.kv_get("hc_url") or os.getenv("HEALTHCHECK_URL", "")


def set_hc_url(url):
    url = (url or "").strip()
    if url and not url.startswith("https://"):
        raise ValueError("الرابط يجب أن يبدأ بـ https://")
    memory.kv_set("hc_url", url)


def ping(suffix=""):
    url = hc_url()
    if not url:
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(url + suffix, headers={"User-Agent": "trading-agent"}),
                               timeout=10).read()
        memory.kv_set("hc_last", time.time())
        return True
    except Exception as e:
        log.warning(f"healthcheck: {e}")
        return False


async def monitor_loop(agent):
    while True:
        try:
            if time.time() - agent.heartbeat < 600:  # فقط إذا كان الوكيل يعمل فعلاً
                await asyncio.to_thread(ping)
            if time.time() - agent.heartbeat < 600:
                maybe_daily_backup()
        except Exception as e:
            log.warning(f"monitor: {e}")
        await asyncio.sleep(300)


# ------------------------------------------------------------------ النسخ الاحتياطي
def snapshot(dest):
    """نسخة متسقة من قاعدة البيانات حتى أثناء عمل الوكيل."""
    src = sqlite3.connect(memory.DB)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()


def maybe_daily_backup():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if memory.kv_get("backup_day") == today or datetime.now(timezone.utc).hour < 2:
        return
    path = os.path.join(BACKUP_DIR, f"agent-{today}.db.gz")
    make_gz(path)
    memory.kv_set("backup_day", today)
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("agent-"))
    for f in files[:-KEEP]:
        os.remove(os.path.join(BACKUP_DIR, f))
    memory.log_event(f"💾 نسخة احتياطية يومية: {os.path.getsize(path) / 1e6:.1f} MB")


def make_gz(path):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=BACKUP_DIR) as t:
        tmp = t.name
    try:
        snapshot(tmp)
        with open(tmp, "rb") as fi, gzip.open(path, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
    finally:
        os.remove(tmp)
    return path


def backups():
    out = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.startswith("agent-"):
            p = os.path.join(BACKUP_DIR, f)
            out.append({"name": f, "mb": round(os.path.getsize(p) / 1e6, 1), "ts": os.path.getmtime(p)})
    return out


def restore(data: bytes):
    """يتحقق من الملف، يحفظ نسخة من الحالي، ثم يستبدل قاعدة البيانات. يتطلب إعادة تشغيل."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("الملف ليس نسخة احتياطية صالحة")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=BACKUP_DIR) as t:
        t.write(data)
        tmp = t.name
    try:
        c = sqlite3.connect(tmp)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        if not REQUIRED_TABLES <= tables:
            raise ValueError("النسخة ناقصة: جداول مفقودة")
        make_gz(os.path.join(BACKUP_DIR, f"before-restore-{int(time.time())}.db.gz"))
        os.replace(tmp, memory.DB)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def status():
    return {"hc_set": bool(hc_url()), "hc_last": memory.kv_get("hc_last"), "backups": backups()[:7],
            "last_backup_day": memory.kv_get("backup_day")}
