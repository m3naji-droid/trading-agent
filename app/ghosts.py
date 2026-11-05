"""
الوكلاء الأشباح: 3 محافظ وهمية تعمل بالتوازي على نفس الفرص، بمستويات جرأة مختلفة.
الهدف: معرفة أي مستوى جرأة يربح أكثر فعلياً — بالأدلة لا بالتخمين.
"""
import math
import time
import uuid

from . import memory
from .config import settings

PROFILES = {
    "conservative": {"name": "المحافظ", "icon": "🐢", "min_conf": 0.62, "risk": 0.01, "max_lev": 2,
                     "council": 3, "max_pos": 3, "respect_risk_off": True,
                     "desc": "ثقة عالية فقط، إجماع المجلس، مخاطرة 1%، رافعة حتى x2"},
    "balanced": {"name": "المتوازن", "icon": "⚖️", "min_conf": 0.55, "risk": 0.015, "max_lev": 3,
                 "council": 2, "max_pos": 4, "respect_risk_off": True,
                 "desc": "موافقة صوتين، مخاطرة 1.5%، رافعة حتى x3"},
    "aggressive": {"name": "المغامر", "icon": "🔥", "min_conf": 0.48, "risk": 0.025, "max_lev": 5,
                   "council": 0, "max_pos": 6, "respect_risk_off": False,
                   "desc": "يتجاهل المجلس وخطر السوق، مخاطرة 2.5%، رافعة حتى x5"},
}
SLIP = 0.0003


class GhostBook:
    def __init__(self, key, st=None):
        self.key, self.p = key, PROFILES[key]
        st = st or {}
        self.balance = st.get("balance", settings.paper_start_balance)
        self.start = st.get("start", settings.paper_start_balance)
        self.positions = st.get("positions", {})
        self.n, self.wins, self.pnl = st.get("n", 0), st.get("wins", 0), st.get("pnl", 0.0)
        self.gross_win, self.gross_loss = st.get("gross_win", 0.0), st.get("gross_loss", 0.0)
        self.peak, self.max_dd = st.get("peak", self.balance), st.get("max_dd", 0.0)
        self.created = st.get("created", time.time())

    def dump(self):
        return {k: getattr(self, k) for k in ("balance", "start", "positions", "n", "wins", "pnl", "gross_win",
                                              "gross_loss", "peak", "max_dd", "created")}

    def equity(self, prices):
        eq = self.balance
        for p in self.positions.values():
            px = prices.get(p["symbol"], p["entry"])
            eq += (px - p["entry"]) * p["qty"] * (1 if p["side"] == "long" else -1)
        return eq

    def wants(self, conf, council_yes, risk_off, blackout, meme=False):
        p = self.p
        if meme and sum(1 for x in self.positions.values() if x.get("meme")) >= settings.meme_max_positions:
            return False
        if blackout and p["respect_risk_off"]:
            return False
        if risk_off and p["respect_risk_off"]:
            return False
        return conf >= p["min_conf"] and council_yes >= p["council"] and len(self.positions) < p["max_pos"]

    def open(self, sym, sig, price, atr, genome, prices, meme=False, spot_only=False):
        if spot_only and sig.side == "short":
            return
        if any(x["symbol"] == sym for x in self.positions.values()):
            return
        g = genome
        d = 1 if sig.side == "long" else -1
        risk0 = abs(sig.entry - sig.stop)
        if risk0 <= 0:
            return
        rr0 = abs(sig.target - sig.entry) / risk0
        dist = risk0 * g["stop"]
        fill = price * (1 + d * SLIP)
        eq = self.equity(prices)
        qty = eq * self.p["risk"] * (settings.meme_risk_mult if meme else 1) / dist
        notional = qty * fill
        budget = eq / self.p["max_pos"]
        max_lev = 1 if spot_only else min(self.p["max_lev"], settings.meme_max_leverage) if meme else self.p["max_lev"]
        lev = min(max_lev, max(1, math.ceil(notional / max(budget, 1e-9))))
        if notional > budget * lev:
            notional = budget * lev
            qty = notional / fill
        if notional < 5:
            return
        fee = notional * settings.fee_rate
        self.balance -= fee
        pid = uuid.uuid4().hex[:8]
        self.positions[pid] = {"symbol": sym, "side": sig.side, "entry": fill, "qty": qty, "lev": lev,
                               "stop": fill - d * dist, "init_stop": fill - d * dist,
                               "target": fill + d * dist * rr0 * g["rr"], "atr": atr, "best": fill,
                               "genome": g, "risk_usd": qty * dist, "opened": time.time(), "fees": fee,
                               "strategy": sig.strategy, "meme": meme}

    def manage(self, prices):
        closed = []
        for pid, p in list(self.positions.items()):
            px = prices.get(p["symbol"])
            if not px:
                continue
            long = p["side"] == "long"
            if (px <= p["stop"]) if long else (px >= p["stop"]):
                closed.append((pid, p["stop"]))
                continue
            if (px >= p["target"]) if long else (px <= p["target"]):
                closed.append((pid, p["target"]))
                continue
            risk = abs(p["entry"] - p["init_stop"])
            p["best"] = max(p["best"], px) if long else min(p["best"], px)
            gain = (p["best"] - p["entry"]) if long else (p["entry"] - p["best"])
            g = p["genome"]
            if gain >= g["be"] * risk:
                trail = g["trail"] * p["atr"] * (0.67 if gain >= 2 * risk else 1.0)
                p["stop"] = max(p["stop"], p["entry"], p["best"] - trail) if long else \
                    min(p["stop"], p["entry"], p["best"] + trail)
            if time.time() - p["opened"] > 72 * 3600:
                closed.append((pid, px))
        for pid, px in closed:
            self._close(pid, px)
        eq = self.equity(prices)
        self.peak = max(self.peak, eq)
        if self.peak > 0:
            self.max_dd = max(self.max_dd, 1 - eq / self.peak)

    def _close(self, pid, px):
        p = self.positions.pop(pid)
        d = 1 if p["side"] == "long" else -1
        fill = px * (1 - d * SLIP)
        fee = p["qty"] * fill * settings.fee_rate
        pnl = (fill - p["entry"]) * p["qty"] * d - fee
        self.balance += pnl
        net = pnl - p["fees"]
        self.n += 1
        self.pnl += net
        if net > 0:
            self.wins += 1
            self.gross_win += net
        else:
            self.gross_loss += -net

    def summary(self, prices):
        eq = self.equity(prices)
        return {"key": self.key, **{k: self.p[k] for k in ("name", "icon", "desc")},
                "equity": eq, "return": eq / self.start - 1 if self.start else 0, "trades": self.n,
                "winrate": self.wins / self.n if self.n else 0, "pnl": self.pnl, "max_dd": self.max_dd,
                "profit_factor": self.gross_win / self.gross_loss if self.gross_loss else None,
                "open": len(self.positions), "days": (time.time() - self.created) / 86400}


class Ghosts:
    def __init__(self):
        st = memory.kv_get("ghosts", {}) or {}
        self.books = {k: GhostBook(k, st.get(k)) for k in PROFILES}
        self._last_rec = 0

    def save(self):
        memory.kv_set("ghosts", {k: b.dump() for k, b in self.books.items()})

    def consider(self, sym, sig, conf, council_yes, price, atr, genome, risk_off, blackout, prices,
                 meme=False, spot_only=False):
        for b in self.books.values():
            if b.wants(conf, council_yes, risk_off, blackout, meme):
                b.open(sym, sig, price, atr, genome, prices, meme, spot_only)

    def tick(self, prices, btc):
        for b in self.books.values():
            b.manage(prices)
        if time.time() - self._last_rec > 300:
            for k, b in self.books.items():
                memory.record_equity(b.equity(prices), btc, f"ghost:{k}")
            self._last_rec = time.time()
        self.save()

    def summary(self, prices):
        return [b.summary(prices) for b in self.books.values()]
