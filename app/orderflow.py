"""
محرك تدفق الأوامر وتتبع الحيتان:
- عدم توازن دفتر الأوامر (Order Book Imbalance)
- صفقات الحيتان الكبيرة (Whale prints)
- عناقيد التصفية (Liquidation clusters) من بث Binance Futures المباشر
- معدل التمويل والاهتمام المفتوح
"""
import asyncio
import json
import logging
import time
from collections import defaultdict, deque

log = logging.getLogger("orderflow")


def book_imbalance(ob: dict, price: float, band: float = 0.01) -> float:
    """من -1 (ضغط بيع) إلى +1 (ضغط شراء) ضمن ±1% من السعر."""
    lo, hi = price * (1 - band), price * (1 + band)
    bids = sum(p * a for p, a, *_ in ob.get("bids", []) if p >= lo)
    asks = sum(p * a for p, a, *_ in ob.get("asks", []) if p <= hi)
    tot = bids + asks
    return 0.0 if tot == 0 else (bids - asks) / tot


def whale_flow(trades: list, min_usd: float = 100_000) -> dict:
    """صافي تدفق الصفقات الكبيرة (شراء - بيع) بالدولار."""
    buy = sell = 0.0
    n = 0
    for t in trades:
        cost = t.get("cost") or (t.get("price", 0) * t.get("amount", 0))
        if cost >= min_usd:
            n += 1
            if t.get("side") == "buy":
                buy += cost
            else:
                sell += cost
    tot = buy + sell
    return {"count": n, "net_usd": buy - sell, "ratio": 0.0 if tot == 0 else (buy - sell) / tot}


class LiquidationTracker:
    """يستمع لبث التصفيات الإجباري من Binance Futures ويجمعها حسب العملة."""

    URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

    def __init__(self, window_sec: int = 3600):
        self.window = window_sec
        self.events = defaultdict(deque)  # symbol -> (ts, side, price, usd)
        self.connected = False
        self.on_event = None  # رادار الحيتان

    async def run(self):
        try:
            import websockets
        except ImportError:
            log.warning("websockets غير مثبتة — تتبع التصفيات معطل")
            return
        while True:
            try:
                async with websockets.connect(self.URL, ping_interval=20) as ws:
                    self.connected = True
                    async for msg in ws:
                        d = json.loads(msg).get("o", {})
                        sym = d.get("s", "")
                        price = float(d.get("ap") or d.get("p") or 0)
                        usd = price * float(d.get("z") or d.get("q") or 0)
                        # side=SELL تعني تصفية مركز شراء (long)
                        side = "long" if d.get("S") == "SELL" else "short"
                        self.events[sym].append((time.time(), side, price, usd))
                        if self.on_event and sym.endswith("USDT"):
                            self.on_event(sym[:-4], side, price, usd)
                        self._trim(sym)
            except Exception as e:  # إعادة اتصال تلقائية
                self.connected = False
                log.warning(f"انقطع بث التصفيات: {e} — إعادة المحاولة بعد 10 ثوانٍ")
                await asyncio.sleep(10)

    def _trim(self, sym):
        cut = time.time() - self.window
        q = self.events[sym]
        while q and q[0][0] < cut:
            q.popleft()

    def recent(self, symbol: str, sec=900) -> dict:
        """تصفيات آخر sec ثانية (لاستراتيجية صيد التصفيات)."""
        raw = symbol.replace("/", "").split(":")[0]
        cut = time.time() - sec
        q = [e for e in self.events.get(raw, []) if e[0] >= cut]
        base = raw[:-4]
        thr = 2_000_000 if base in ("BTC", "ETH") else 500_000 if base in ("SOL", "XRP", "BNB", "DOGE") else 200_000
        return {"long_usd": sum(e[3] for e in q if e[1] == "long"),
                "short_usd": sum(e[3] for e in q if e[1] == "short"), "thr": thr} if self.connected else None

    def summary(self, symbol: str) -> dict:
        """symbol بصيغة ccxt مثل BTC/USDT."""
        raw = symbol.replace("/", "").split(":")[0]
        self._trim(raw)
        q = self.events.get(raw, [])
        longs = sum(e[3] for e in q if e[1] == "long")
        shorts = sum(e[3] for e in q if e[1] == "short")
        # عنقود: متوسط السعر المرجح للتصفيات
        tot = longs + shorts
        cluster = sum(e[2] * e[3] for e in q) / tot if tot else None
        return {"long_liq_usd": longs, "short_liq_usd": shorts, "cluster_price": cluster,
                "bias": 0.0 if tot == 0 else (shorts - longs) / tot}


async def snapshot(spot, fut, symbol: str, price: float, liq: LiquidationTracker) -> dict:
    """يجمع كل بيانات التدفق لعملة واحدة. أي فشل جزئي لا يوقف التحليل."""
    out = {"imbalance": 0.0, "whale": {"count": 0, "net_usd": 0, "ratio": 0.0},
           "funding": 0.0, "liq": liq.summary(symbol) if liq else {}}
    try:
        ob = await spot.fetch_order_book(symbol, limit=100)
        out["imbalance"] = book_imbalance(ob, price)
    except Exception as e:
        log.debug(f"orderbook {symbol}: {e}")
    try:
        trades = await spot.fetch_trades(symbol, limit=500)
        thr = 250_000 if symbol.startswith(("BTC", "ETH")) else 50_000
        out["whale"] = whale_flow(trades, thr)
    except Exception as e:
        log.debug(f"trades {symbol}: {e}")
    if fut is not None:
        try:
            fr = await fut.fetch_funding_rate(symbol + ":USDT")
            out["funding"] = float(fr.get("fundingRate") or 0)
        except Exception:
            pass
    return out
