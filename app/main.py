"""
سيرفر الويب (FastAPI): يشغّل الوكيل في الخلفية ويقدّم لوحة التحكم و API محمية بكلمة مرور.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent
from .config import settings
from . import memory, notify, performance, execution, maintenance
from fastapi.responses import Response
from .strategies import STRATEGY_AR
from .regime import LABELS_AR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
agent = Agent()
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
import json as _json
import urllib.request as _ur
try:
    VERSION = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")).read().strip()
except Exception:
    VERSION = "—"
from .version import VERSION_NUMBER
VERSION_DATE = VERSION.split("·")[1].strip() if "·" in VERSION else ""
VERSION_NAME = f"v{VERSION_NUMBER}"
RUNNING_SHA = VERSION.split()[0] if VERSION not in ("—", "baked") else ""
GITHUB_REPO = os.getenv("GITHUB_REPO", "m3naji-droid/trading-agent")
TOKEN = hmac.new(settings.dashboard_password.encode(), b"dashboard", hashlib.sha256).hexdigest()


def _remote_sha(etag):
    req = _ur.Request(f"https://api.github.com/repos/{GITHUB_REPO}/commits/main",
                      headers={"User-Agent": "trading-agent", "Accept": "application/vnd.github+json",
                               **({"If-None-Match": etag} if etag else {})})
    try:
        with _ur.urlopen(req, timeout=20) as r:
            return _json.loads(r.read())["sha"], r.headers.get("ETag")
    except _ur.HTTPError as e:
        if e.code == 304:  # لا تغيير (لا يُحتسب من حد الطلبات)
            return None, etag
        raise


async def update_watcher():
    """يفحص GitHub كل دقيقة. عند وجود نسخة جديدة يطلب إعادة تشغيل بين دورتين، فيحمّلها boot.py."""
    if os.getenv("AUTO_UPDATE", "1") != "1":
        return
    etag = None
    while True:
        await asyncio.sleep(60)
        try:
            sha, etag = await asyncio.to_thread(_remote_sha, etag)
        except Exception as e:
            log.debug(f"update check: {e}")
            continue
        if sha and sha[:7] != RUNNING_SHA and not agent.restart_requested:
            memory.log_event("⬆️ وُجد تحديث جديد — تثبيته وإعادة التشغيل خلال لحظات")
            agent.restart_requested = True


def watchdog():
    """إذا توقفت الحلقة الرئيسية أكثر من 10 دقائق، ينهي العملية ليعيد Docker تشغيلها تلقائياً."""
    while True:
        time.sleep(60)
        if time.time() - agent.heartbeat > 600:
            log.error("الوكيل لا يستجيب — إعادة تشغيل ذاتية")
            os._exit(1)


@asynccontextmanager
async def lifespan(app):
    await agent.start()
    threading.Thread(target=watchdog, daemon=True).start()
    asyncio.create_task(maintenance.monitor_loop(agent))
    asyncio.create_task(update_watcher())
    if RUNNING_SHA and memory.kv_get("last_version") != RUNNING_SHA:
        memory.log_event(f"✅ يعمل الآن الإصدار {VERSION_NAME}")
        memory.kv_set("last_version", RUNNING_SHA)
    yield
    for ex in (agent.spot, agent.fut):
        try:
            await ex.close()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def auth(req: Request):
    tok = req.headers.get("authorization", "").replace("Bearer ", "")
    if not hmac.compare_digest(tok, TOKEN):
        raise HTTPException(401, "غير مصرح")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/health")
async def health():
    ok = time.time() - agent.heartbeat < 600
    return JSONResponse({"ok": ok, "status": agent.status, "version": VERSION_NAME}, status_code=200 if ok else 503)


@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    if not hmac.compare_digest(str(body.get("password", "")), settings.dashboard_password):
        await asyncio.sleep(1.5)  # إبطاء محاولات التخمين
        raise HTTPException(401, "كلمة المرور غير صحيحة")
    return {"token": TOKEN}


@app.get("/api/state")
async def state(req: Request):
    auth(req)
    s = agent.state()
    s["events"] = memory.events(80)
    s["trades"] = memory.recent_trades(40)
    s["lessons"] = memory.lessons(8)
    s["equity_curve"] = memory.equity_curve(mode=agent.mode)
    s["perf"] = performance.summary(agent.mode)
    s["ghost_curves"] = {k: memory.equity_curve(mode=f"ghost:{k}") for k in ("conservative", "balanced", "aggressive")}
    s["reports"] = memory.kv_get("reports", [])[:7]
    s["notify"] = {"on": notify.enabled(), "topic": notify.topic(), "server": notify.SERVER}
    s["version"] = VERSION_NAME
    s["maint"] = maintenance.status()
    s["version_date"] = VERSION_DATE
    return s


@app.get("/api/candles")
async def candles(req: Request, symbol: str = "BTC/USDT", tf: str = "15m"):
    auth(req)
    if tf not in ("5m", "15m", "1h", "4h", "1d"):
        tf = "15m"
    return await agent.candles(symbol, tf)


EXIT_AR = {"guard": "حارس الأرباح", "giveback": "إرجاع الربح", "breakeven": "تعادل بعد ربح جزئي",
           "exchange_target": "هدف المنصة", "stop": "وقف الخسارة", "trailing": "الوقف المتحرك", "target": "الهدف", "timeout": "انتهاء المدة",
           "kill": "مفتاح الطوارئ", "regime": "تغيّر حالة السوق", "exchange_stop": "وقف المنصة"}


def explain(t: dict, closed: bool) -> list:
    """شرح القرار بالعربية: لماذا دخل، كيف خرج، وماذا تعلّم."""
    side = "شراء" if t["side"] == "long" else "بيع"
    out = [f"قرر الوكيل {side} {t['symbol']} باستراتيجية «{STRATEGY_AR.get(t['strategy'], t['strategy'])}» "
           f"لأن السوق كان في حالة «{LABELS_AR.get(t.get('regime'), t.get('regime') or '—')}»."]
    if t.get("reasons"):
        out.append("أسباب الدخول: " + "، ".join(t["reasons"]) + ".")
    risk = abs(t["entry"] - (t.get("init_stop") or t["stop"]))
    if risk:
        out.append(f"وضع وقف الخسارة على مسافة {risk / t['entry']:.2%} من الدخول، والهدف عند "
                   f"{abs(t['target'] - t['entry']) / risk:.1f} ضعف المخاطرة.")
    if closed:
        r = t.get("r_mult") or 0
        out.append(f"خرج عبر {EXIT_AR.get(t.get('exit_reason'), t.get('exit_reason'))} بنتيجة {t.get('pnl', 0):+.2f}$ ({r:+.2f}R).")
        if r > 0:
            out.append("🧠 التعلم: زادت ثقة الوكيل بهذه الاستراتيجية في هذه الحالة من السوق.")
        else:
            out.append("🧠 التعلم: حُفظت ظروف هذه الصفقة؛ إذا تكرر نمط مشابه وخسر غالباً سيرفضه الوكيل مستقبلاً.")
    else:
        out.append("الصفقة مفتوحة، والوكيل يديرها بوقف متحرك وفق جينات داروين الحالية.")
    if t.get("partial"):
        out.append(f"💰 جُني نصف الصفقة مبكراً بربح {t.get('realized', 0):+.2f}$، ونُقل وقف الباقي لسعر الدخول.")
    if t.get("exit_reason") == "guard":
        out.append("🛡️ حارس الأرباح أغلقها بعد ظهور علامات انعكاس، بدلاً من انتظار وقف الخسارة الكامل.")
    if t.get("exit_reason") == "giveback":
        out.append("📉 أُغلقت لأنها أعادت جزءاً كبيراً من أعلى ربح وصلت إليه.")
    g = t.get("genome")
    if g:
        out.append(f"🧬 جينات الإدارة: وقف ×{g['stop']} · هدف ×{g['rr']} · تتبع {g['trail']}×ATR يبدأ عند +{g['be']}R.")
    return out


@app.get("/api/trade/{tid}")
async def trade_detail(req: Request, tid: str):
    auth(req)
    pos = agent.broker.positions.get(tid) if agent.broker else None
    if pos:
        px = agent.prices.get(pos["symbol"], pos["entry"])
        d_ = 1 if pos["side"] == "long" else -1
        upnl = (px - pos["entry"]) * pos["qty"] * d_
        t, closed = dict(pos, price=px, upnl=upnl, r_now=upnl / pos["risk_usd"] if pos.get("risk_usd") else 0), False
        t.pop("features", None)
    else:
        try:
            t = memory.get_trade(int(tid))
        except ValueError:
            t = None
        closed = True
        if not t:
            raise HTTPException(404, "الصفقة غير موجودة")
    end = t.get("closed") or time.time()
    dur = end - t["opened"]
    tf = "15m" if dur < 86400 else "1h" if dur < 4 * 86400 else "4h"
    sec = {"15m": 900, "1h": 3600, "4h": 14400}[tf]
    since = int((t["opened"] - 40 * sec) * 1000)
    limit = min(500, int(dur / sec) + 80)
    try:
        rows = await agent.spot.fetch_ohlcv(t["symbol"], tf, since=since, limit=limit)
    except Exception:
        rows = []
    return {"trade": t, "closed": closed, "tf": tf, "explain": explain(t, closed), "council": t.get("council"),
            "candles": [{"time": r[0] / 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows]}


@app.get("/api/radar")
async def radar(req: Request, window: int = 1800):
    auth(req)
    return agent.radar.snapshot(max(300, min(window, 3600)))


@app.post("/api/maint")
async def maint(req: Request):
    auth(req)
    body = await req.json()
    a = body.get("action")
    if a == "hc_set":
        try:
            maintenance.set_hc_url(body.get("url", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        ok = await asyncio.to_thread(maintenance.ping) if body.get("url") else False
        memory.log_event("🩺 تم ربط المراقب الخارجي" if ok else "🩺 حُفظ رابط المراقب (لم تنجح النبضة الأولى)")
        return {"ok": ok}
    if a == "whale_key":
        memory.kv_set("whale_key", (body.get("key") or "").strip())
        memory.log_event("🐋 تم حفظ مفتاح Whale Alert" if body.get("key") else "🐋 أُزيل مفتاح Whale Alert")
        return {"ok": True}
    if a == "hc_test":
        return {"ok": await asyncio.to_thread(maintenance.ping)}
    if a == "backup_now":
        p = await asyncio.to_thread(maintenance.make_gz, os.path.join(
            maintenance.BACKUP_DIR, f"agent-{time.strftime('%Y-%m-%d')}-manual.db.gz"))
        return {"ok": True, "name": os.path.basename(p)}
    raise HTTPException(400, "أمر غير معروف")


@app.get("/api/backup")
async def backup_download(req: Request):
    auth(req)
    path = os.path.join(maintenance.BACKUP_DIR, "download.db.gz")
    await asyncio.to_thread(maintenance.make_gz, path)
    data = open(path, "rb").read()
    os.remove(path)
    name = f"agent-backup-{time.strftime('%Y-%m-%d')}.db.gz"
    return Response(data, media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/restore")
async def backup_restore(req: Request):
    auth(req)
    data = await req.body()
    if len(data) > 300 * 1024 * 1024:
        raise HTTPException(400, "الملف كبير جداً")
    try:
        await asyncio.to_thread(maintenance.restore, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    asyncio.get_event_loop().call_later(1.0, lambda: os._exit(0))  # إعادة تشغيل لتحميل الذاكرة المستعادة
    return {"ok": True}


@app.get("/api/trades")
async def trades_all(req: Request, limit: int = 500):
    auth(req)
    return {"trades": memory.recent_trades(max(1, min(limit, 2000)))}


@app.get("/api/export")
async def export_csv(req: Request):
    """كل الصفقات الفعلية كملف CSV يفتح في Excel (عناوين عربية)."""
    auth(req)
    import csv
    import io
    from datetime import datetime as _dt
    rows = memory.recent_trades(100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["رقم", "الوضع", "العملة", "السوق", "الاتجاه", "الاستراتيجية", "حالة السوق", "وقت الفتح (UTC)",
                "وقت الإغلاق (UTC)", "سعر الدخول", "وقف الخسارة", "الهدف", "سعر الخروج", "الكمية", "الرافعة",
                "الربح $", "الربح R", "سبب الخروج", "الأسباب"])
    fmt = lambda t: _dt.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
    for t in reversed(rows):
        w.writerow([t["id"], t["mode"], t["symbol"], t["market"], "شراء" if t["side"] == "long" else "بيع",
                    STRATEGY_AR.get(t["strategy"], t["strategy"]), LABELS_AR.get(t["regime"], t["regime"]),
                    fmt(t["opened"]), fmt(t["closed"]), t["entry"], t["stop"], t["target"], t["exit"], t["qty"],
                    t["leverage"], round(t["pnl"] or 0, 4), round(t["r_mult"] or 0, 3),
                    EXIT_AR.get(t["exit_reason"], t["exit_reason"]), " | ".join(t.get("reasons") or [])])
    data = "\ufeff" + buf.getvalue()  # BOM حتى يعرض Excel العربية صحيحاً
    name = f"trades-{time.strftime('%Y-%m-%d')}.csv"
    return Response(data.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/safe")
async def safe_mode(req: Request):
    auth(req)
    body = await req.json()
    hours = float(body.get("hours", 0))
    if hours <= 0:
        memory.kv_set("safe_mode", {})
        memory.log_event("🐢 أُطفئ الوضع الآمن — العودة للحجم الطبيعي")
    else:
        hours = min(hours, 24 * 30)
        memory.kv_set("safe_mode", {"until": time.time() + hours * 3600, "factor": 0.5, "since": time.time()})
        memory.log_event(f"🐢 الوضع الآمن مفعّل لمدة {hours:.0f} ساعة: نصف حجم الصفقات")
    return {"ok": True}


@app.post("/api/notify")
async def notify_ctl(req: Request):
    auth(req)
    body = await req.json()
    if body.get("action") == "toggle":
        notify.set_enabled(not notify.enabled())
    elif body.get("action") == "test":
        notify.send("🤖 إشعار تجريبي", "الإشعارات تعمل! ستصلك تنبيهات الصفقات والطوارئ والتقرير اليومي هنا.",
                    force=True, tags=["tada"])
    return {"on": notify.enabled()}


@app.post("/api/control")
async def control(req: Request):
    auth(req)
    body = await req.json()
    a = body.get("action")
    if a == "start":
        agent.running = True
        memory.kv_set("running", True)
        memory.log_event("▶️ تم تشغيل الدخول في صفقات جديدة")
    elif a == "stop":
        agent.running = False
        memory.kv_set("running", False)
        memory.log_event("⏸️ إيقاف الدخول في صفقات جديدة (الصفقات المفتوحة تُدار حتى الإغلاق)")
    elif a == "kill":
        await agent.kill("ضغط زر الطوارئ من لوحة التحكم", manual=True)
    elif a == "resume":
        agent.risk.unfreeze()
        memory.log_event("🔓 رُفع التجميد يدوياً")
    elif a == "mode":
        m = body.get("mode")
        if m not in ("paper", "testnet", "live"):
            raise HTTPException(400, "وضع غير معروف")
        if m != "paper" and not settings.api_key:
            raise HTTPException(400, "أضف مفاتيح API في ملف .env أولاً")
        if agent.broker and agent.broker.positions:
            raise HTTPException(400, "أغلق الصفقات المفتوحة (زر الطوارئ) قبل تغيير الوضع")
        if m == "live":
            ts = execution.testnet_status()
            if not ts["passed"]:
                raise HTTPException(400, f"المال الحقيقي مقفل حتى تكتمل مرحلة Testnet: "
                                         f"{ts['days']:.1f}/{ts['need_days']} أيام و{ts['trades']}/{ts['need_trades']} صفقات")
        if m == "testnet":
            execution.mark_testnet_start()
        memory.kv_set("mode", m)
        memory.log_event(f"🔁 تغيير الوضع إلى {m.upper()} — إعادة تشغيل")
        asyncio.get_event_loop().call_later(1.0, lambda: os._exit(0))  # Docker يعيد التشغيل
    else:
        raise HTTPException(400, "أمر غير معروف")
    return {"ok": True}
