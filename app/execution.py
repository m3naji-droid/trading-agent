"""
جودة التنفيذ:
- إحصاءات أوامر Limit (Maker) مقابل Market (Taker) والرسوم الموفّرة.
- قياس الانزلاق الفعلي: الفرق بين السعر المتوقع وسعر التنفيذ (بنقاط الأساس bps).
- مرحلة Testnet الإلزامية قبل المال الحقيقي.
"""
import time

from . import memory
from .config import settings

TESTNET_DAYS = 7
TESTNET_TRADES = 10


def _st():
    return memory.kv_get("exec", {}) or {}


def record_fill(mode, kind, expected, fill, side, notional):
    """kind: maker / taker. يسجل الانزلاق والرسوم الموفّرة."""
    st = _st()
    m = st.setdefault(mode, {"maker": 0, "taker": 0, "saved": 0.0, "slip_sum": 0.0, "slip_n": 0, "slip_max": 0.0})
    m[kind] += 1
    if kind == "maker":
        m["saved"] += notional * (settings.fee_rate - settings.maker_fee)
    d = 1 if side == "long" else -1
    if expected:
        bps = (fill - expected) / expected * d * 10_000  # موجب = تنفيذ أسوأ من المتوقع
        m["slip_sum"] += bps
        m["slip_n"] += 1
        m["slip_max"] = max(m["slip_max"], bps)
    memory.kv_set("exec", st)


def summary(mode):
    m = _st().get(mode) or {}
    n = m.get("maker", 0) + m.get("taker", 0)
    return {"orders": n, "maker": m.get("maker", 0), "taker": m.get("taker", 0),
            "maker_share": m.get("maker", 0) / n if n else 0, "saved": m.get("saved", 0.0),
            "slip_avg": m["slip_sum"] / m["slip_n"] if m.get("slip_n") else None, "slip_max": m.get("slip_max")}


def testnet_status():
    t = memory.kv_get("testnet", {}) or {}
    start = t.get("start")
    with memory._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM trades WHERE mode='testnet' AND shadow=0 AND closed IS NOT NULL").fetchone()[0]
    days = (time.time() - start) / 86400 if start else 0.0
    ok = days >= TESTNET_DAYS and n >= TESTNET_TRADES
    return {"started": start, "days": days, "trades": n, "need_days": TESTNET_DAYS, "need_trades": TESTNET_TRADES,
            "passed": ok}


def mark_testnet_start():
    t = memory.kv_get("testnet", {}) or {}
    if not t.get("start"):
        t["start"] = time.time()
        memory.kv_set("testnet", t)
