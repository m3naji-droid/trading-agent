"""
الوسطاء (Brokers):
- PaperBroker: محاكاة كاملة بأسعار Binance الحقيقية (رصيد افتراضي).
- LiveBroker : تنفيذ فعلي على Binance (testnet أو حقيقي) مع وقف خسارة على المنصة نفسها،
               حتى لو توقف السيرفر تبقى الصفقة محمية.
"""
import logging
import time
import uuid

from . import memory
from .config import settings
from .exchange import fut_symbol

log = logging.getLogger("broker")
SLIPPAGE = 0.0003


class PaperBroker:
    name = "paper"

    def __init__(self, spot, fut):
        self.spot, self.fut = spot, fut
        st = memory.kv_get("paper", None)
        if st is None:
            st = {"balance": settings.paper_start_balance, "positions": {}}
        self.balance = st["balance"]
        self.positions = st["positions"]

    def _save(self):
        memory.kv_set("paper", {"balance": self.balance, "positions": self.positions})

    async def equity(self, prices: dict) -> float:
        return self.balance + sum(self.upnl(p, prices.get(p["symbol"], p["entry"])) for p in self.positions.values())

    @staticmethod
    def upnl(p, price):
        d = 1 if p["side"] == "long" else -1
        return (price - p["entry"]) * p["qty"] * d

    def used_margin(self):
        return sum(p["notional"] / p["leverage"] for p in self.positions.values())

    async def open(self, pos: dict, price: float):
        d = 1 if pos["side"] == "long" else -1
        fill = price * (1 + d * SLIPPAGE)
        notional = pos["qty"] * fill
        if self.used_margin() + notional / pos["leverage"] > self.balance:
            raise RuntimeError("هامش غير كافٍ")
        self.balance -= notional * settings.fee_rate
        pos |= {"id": uuid.uuid4().hex[:10], "entry": fill, "notional": notional, "opened": time.time(),
                "best": fill, "fees": notional * settings.fee_rate}
        self.positions[pos["id"]] = pos
        self._save()
        return pos

    async def update_stop(self, pos, new_stop):
        pos["stop"] = new_stop
        self._save()

    async def close(self, pos, price, reason):
        d = 1 if pos["side"] == "long" else -1
        fill = price * (1 - d * SLIPPAGE)
        fee = pos["qty"] * fill * settings.fee_rate
        pnl = (fill - pos["entry"]) * pos["qty"] * d - fee
        self.balance += pnl
        self.positions.pop(pos["id"], None)
        self._save()
        return fill, pnl - pos.get("fees", 0)

    async def sync(self, prices):
        return []  # لا شيء يُغلق خارج سيطرة الوكيل في المحاكاة


class LiveBroker(PaperBroker):
    """تنفيذ حقيقي. يرث حفظ المراكز من PaperBroker لكن الرصيد يُقرأ من المنصة."""
    name = "live"

    def __init__(self, spot, fut, mode):
        self.spot, self.fut, self.mode = spot, fut, mode
        st = memory.kv_get(f"live_{mode}", {"positions": {}})
        self.positions = st["positions"]
        self.balance = 0.0
        self._last_equity = 0.0

    def _save(self):
        memory.kv_set(f"live_{self.mode}", {"positions": self.positions})

    async def equity(self, prices: dict) -> float:
        try:
            sb = await self.spot.fetch_balance()
            usdt = float(sb.get("USDT", {}).get("total") or 0)
            spot_val = sum(p["qty"] * prices.get(p["symbol"], p["entry"])
                           for p in self.positions.values() if p["market"] == "spot")
            fb = await self.fut.fetch_balance()
            fut_eq = float(fb.get("info", {}).get("totalMarginBalance") or fb.get("USDT", {}).get("total") or 0)
            self._last_equity = usdt + spot_val + fut_eq
            self.balance = usdt + fut_eq
        except Exception as e:
            log.warning(f"تعذر قراءة الرصيد: {e}")
        return self._last_equity

    def _ex(self, pos):
        return (self.fut, fut_symbol(pos["symbol"])) if pos["market"] == "futures" else (self.spot, pos["symbol"])

    async def _place_stop(self, pos):
        ex, sym = self._ex(pos)
        side = "sell" if pos["side"] == "long" else "buy"
        qty = float(ex.amount_to_precision(sym, pos["qty"]))
        stop = float(ex.price_to_precision(sym, pos["stop"]))
        if pos["market"] == "futures":
            o = await ex.create_order(sym, "STOP_MARKET", side, qty, None,
                                      {"stopPrice": stop, "reduceOnly": True, "workingType": "MARK_PRICE"})
        else:
            limit = float(ex.price_to_precision(sym, stop * 0.995))
            o = await ex.create_order(sym, "STOP_LOSS_LIMIT", side, qty, limit,
                                      {"stopPrice": stop, "timeInForce": "GTC"})
        pos["stop_order"] = o.get("id")

    async def _cancel_stop(self, pos):
        if pos.get("stop_order"):
            ex, sym = self._ex(pos)
            try:
                await ex.cancel_order(pos["stop_order"], sym)
            except Exception as e:
                log.debug(f"cancel stop: {e}")
            pos["stop_order"] = None

    def min_cost(self, symbol, market):
        ex, sym = (self.fut, fut_symbol(symbol)) if market == "futures" else (self.spot, symbol)
        m = ex.markets.get(sym, {})
        return float(((m.get("limits") or {}).get("cost") or {}).get("min") or 5)

    async def open(self, pos, price):
        ex, sym = self._ex(pos)
        side = "buy" if pos["side"] == "long" else "sell"
        if pos["market"] == "futures":
            try:
                await ex.set_margin_mode("isolated", sym)
            except Exception:
                pass
            try:
                await ex.set_leverage(pos["leverage"], sym)
            except Exception as e:
                log.warning(f"set_leverage: {e}")
        qty = float(ex.amount_to_precision(sym, pos["qty"]))
        if qty * price < self.min_cost(pos["symbol"], pos["market"]):
            raise RuntimeError("الحجم أقل من الحد الأدنى للمنصة")
        o = await ex.create_order(sym, "market", side, qty)
        fill = float(o.get("average") or o.get("price") or price)
        filled = float(o.get("filled") or qty)
        if pos["market"] == "spot":
            filled *= (1 - 0.001)  # عمولة spot تُخصم من العملة المشتراة
        pos |= {"id": uuid.uuid4().hex[:10], "entry": fill, "qty": filled, "notional": filled * fill,
                "opened": time.time(), "best": fill, "fees": filled * fill * settings.fee_rate}
        try:
            await self._place_stop(pos)
        except Exception as e:
            memory.log_event(f"⚠️ تعذر وضع وقف الخسارة على المنصة لـ {sym}: {e} — الوكيل سيديره برمجياً", "warn")
        self.positions[pos["id"]] = pos
        self._save()
        return pos

    async def update_stop(self, pos, new_stop):
        pos["stop"] = new_stop
        await self._cancel_stop(pos)
        try:
            await self._place_stop(pos)
        except Exception as e:
            log.warning(f"تحديث الوقف: {e}")
        self._save()

    async def close(self, pos, price, reason):
        ex, sym = self._ex(pos)
        await self._cancel_stop(pos)
        side = "sell" if pos["side"] == "long" else "buy"
        params = {"reduceOnly": True} if pos["market"] == "futures" else {}
        qty = float(ex.amount_to_precision(sym, pos["qty"]))
        o = await ex.create_order(sym, "market", side, qty, None, params)
        fill = float(o.get("average") or price)
        d = 1 if pos["side"] == "long" else -1
        pnl = (fill - pos["entry"]) * pos["qty"] * d - 2 * pos.get("fees", 0)
        self.positions.pop(pos["id"], None)
        self._save()
        return fill, pnl

    async def sync(self, prices):
        """يكتشف الصفقات التي أغلقها وقف المنصة أثناء انقطاع أو بين الدورات."""
        closed = []
        for pos in list(self.positions.values()):
            if not pos.get("stop_order"):
                continue
            ex, sym = self._ex(pos)
            try:
                o = await ex.fetch_order(pos["stop_order"], sym)
            except Exception:
                continue
            if o.get("status") == "closed":
                fill = float(o.get("average") or pos["stop"])
                d = 1 if pos["side"] == "long" else -1
                pnl = (fill - pos["entry"]) * pos["qty"] * d - 2 * pos.get("fees", 0)
                self.positions.pop(pos["id"], None)
                closed.append((pos, fill, pnl))
        if closed:
            self._save()
        return closed
