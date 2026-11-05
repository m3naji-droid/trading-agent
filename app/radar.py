"""
رادار الحيتان: بث مباشر لصفقات الحيتان الكبيرة (Binance aggTrade) + التصفيات الإجبارية.
"""
import asyncio
import json
import logging
import time
from collections import deque

log = logging.getLogger("radar")
WS = "wss://stream.binance.com:9443/stream?streams="


def threshold(base):
    if base in ("BTC", "ETH"):
        return 250_000
    if base in ("SOL", "XRP", "BNB", "DOGE"):
        return 100_000
    return 50_000


class WhaleRadar:
    def __init__(self, maxlen=800):
        self.events = deque(maxlen=maxlen)  # {"ts","base","kind","usd","price"}
        self.symbols = []
        self.connected = False
        self._want = []

    def set_symbols(self, syms):
        self._want = sorted(s.split("/")[0] for s in syms)

    def add_liquidation(self, base, side, price, usd):
        """side: long (تصفية مشترين) / short (تصفية بائعين)."""
        if usd >= 20_000:
            self.events.append({"ts": time.time(), "base": base, "kind": f"liq_{side}", "usd": usd, "price": price})

    async def run(self):
        try:
            import websockets
        except ImportError:
            log.warning("websockets غير مثبتة — الرادار معطل")
            return
        while True:
            if not self._want:
                await asyncio.sleep(5)
                continue
            self.symbols = list(self._want)
            url = WS + "/".join(f"{b.lower()}usdt@aggTrade" for b in self.symbols)
            try:
                async with websockets.connect(url, ping_interval=20, max_queue=2000) as ws:
                    self.connected = True
                    async for msg in ws:
                        d = json.loads(msg).get("data") or {}
                        base = d.get("s", "")[:-4]
                        usd = float(d.get("p", 0)) * float(d.get("q", 0))
                        if usd >= threshold(base):
                            self.events.append({"ts": d.get("T", time.time() * 1000) / 1000, "base": base,
                                                "kind": "sell" if d.get("m") else "buy", "usd": usd,
                                                "price": float(d.get("p", 0))})
                        if self._want != self.symbols:  # تغيّرت قائمة العملات => إعادة الاشتراك
                            break
            except Exception as e:
                log.warning(f"انقطع بث الحيتان: {e}")
                await asyncio.sleep(10)
            finally:
                self.connected = False

    def snapshot(self, window=1800):
        now = time.time()
        ev = [e for e in self.events if now - e["ts"] <= window]
        flow = {}
        for e in self.events:
            if now - e["ts"] > 3600:
                continue
            f = flow.setdefault(e["base"], {"buy": 0.0, "sell": 0.0, "liq_long": 0.0, "liq_short": 0.0})
            f[e["kind"]] += e["usd"]
        net = sorted(({"base": b, **v, "net": v["buy"] - v["sell"]} for b, v in flow.items()),
                     key=lambda x: -abs(x["net"]))
        return {"now": now, "window": window, "events": ev[-300:], "flow": net[:12],
                "symbols": self.symbols, "connected": self.connected}
