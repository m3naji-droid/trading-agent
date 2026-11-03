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
from . import memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
agent = Agent()
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
try:
    VERSION = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")).read().strip()
except Exception:
    VERSION = "—"
TOKEN = hmac.new(settings.dashboard_password.encode(), b"dashboard", hashlib.sha256).hexdigest()


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
    return JSONResponse({"ok": ok, "status": agent.status}, status_code=200 if ok else 503)


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
    s["equity_curve"] = memory.equity_curve()
    s["version"] = VERSION
    return s


@app.get("/api/candles")
async def candles(req: Request, symbol: str = "BTC/USDT", tf: str = "15m"):
    auth(req)
    if tf not in ("5m", "15m", "1h", "4h", "1d"):
        tf = "15m"
    return await agent.candles(symbol, tf)


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
        memory.kv_set("mode", m)
        memory.log_event(f"🔁 تغيير الوضع إلى {m.upper()} — إعادة تشغيل")
        asyncio.get_event_loop().call_later(1.0, lambda: os._exit(0))  # Docker يعيد التشغيل
    else:
        raise HTTPException(400, "أمر غير معروف")
    return {"ok": True}
