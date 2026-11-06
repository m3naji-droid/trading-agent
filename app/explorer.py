"""
🧭 المستكشف: وكيل تعليمي بمحفظة وهمية منفصلة — لا يلمس المال الحقيقي أبداً، في أي وضع.

يدخل صفقات كثيرة لأن المشاركة تعلّم أسرع من الانتظار:
  - كل الإشارات الكاملة، حتى التي رفضها المجلس أو البوابة أو فلتر داروين أو ذاكرة الأنماط.
  - والإشارات "القريبة" (شروط أخف قليلاً).
  - حتى 25 صفقة مفتوحة، و3 استراتيجيات مختلفة على نفس العملة.

وتُدار صفقاته لحظياً كالوكيل الحقيقي (رسوم، انزلاق، وقف متحرك)، فنتائجه أصدق من المحاكاة الظلّية.
ما يتعلمه يذهب للوكيل الرئيسي: ذاكرة الأنماط، ثقة الاستراتيجيات، وأدلة اعتماد الاستراتيجيات.
أما الوكيل الرئيسي فيبقى منضبطاً — حتى تبقى نتائجه دليلاً صادقاً على جاهزيته للمال الحقيقي.
"""
import time
import uuid

from . import memory
from .config import settings
from . import exits

START = 1000.0
RISK = 0.005          # 0.5% من المحفظة الوهمية لكل صفقة
MAX_POS = 25
MAX_PER_SYMBOL = 3
SLIP = 0.0004
MAX_HOURS = 48


class Explorer:
    def __init__(self, on_close=None):
        st = memory.kv_get("explorer", {}) or {}
        self.balance = st.get("balance", START)
        self.positions = st.get("positions", {})
        self.stats = st.get("stats", {})       # strategy -> {n, wins, r, near_n, near_r}
        self.total = st.get("total", {"n": 0, "wins": 0, "r": 0.0, "pnl": 0.0})
        self.created = st.get("created", time.time())
        self.on_close = on_close
        self._dirty = False

    def save(self):
        if self._dirty:
            memory.kv_set("explorer", {"balance": self.balance, "positions": self.positions, "stats": self.stats,
                                       "total": self.total, "created": self.created})
            self._dirty = False

    def equity(self, prices):
        return self.balance + sum((prices.get(p["symbol"], p["entry"]) - p["entry"]) * p["qty"] *
                                  (1 if p["side"] == "long" else -1) for p in self.positions.values())

    def consider(self, sym, sig, ctx, price, genome, near, extra):
        """extra: معلومات تُحفظ مع الصفقة (features, regime, council, reasons)."""
        if len(self.positions) >= MAX_POS:
            return False
        same = [p for p in self.positions.values() if p["symbol"] == sym]
        if len(same) >= MAX_PER_SYMBOL or any(p["strategy"] == sig.strategy for p in same):
            return False
        d = 1 if sig.side == "long" else -1
        risk0 = abs(sig.entry - sig.stop)
        if risk0 <= 0:
            return False
        rr0 = abs(sig.target - sig.entry) / risk0
        dist = risk0 * genome["stop"]
        fill = price * (1 + d * SLIP)
        qty = START * RISK / dist  # حجم ثابت نسبةً للبداية => نتائج قابلة للمقارنة
        notional = qty * fill
        if notional > START * 0.5:
            qty = START * 0.5 / fill
        fee = qty * fill * settings.fee_rate
        self.balance -= fee
        pid = uuid.uuid4().hex[:8]
        self.positions[pid] = {"id": pid, "symbol": sym, "side": sig.side, "strategy": sig.strategy, "near": near,
                               "entry": fill, "qty": qty, "stop": fill - d * dist, "init_stop": fill - d * dist,
                               "target": fill + d * dist * rr0 * genome["rr"], "atr": float(ctx["df"]["atr"].iloc[-1]),
                               "best": fill, "genome": genome, "risk_usd": qty * dist, "fees": fee,
                               "opened": time.time(), **extra}
        self._dirty = True
        return True

    def on_tick(self, sym, price):
        for pid, p in list(self.positions.items()):
            if p["symbol"] == sym:
                self._manage(pid, p, price)

    def tick_all(self, prices, lastbars=None):
        for pid, p in list(self.positions.items()):
            px = prices.get(p["symbol"])
            if px:
                self._manage(pid, p, px, full=True, bars=(lastbars or {}).get(p["symbol"]))
        self.save()

    def _manage(self, pid, p, px, full=False, bars=None):
        long = p["side"] == "long"
        if (px <= p["stop"]) if long else (px >= p["stop"]):
            moved = abs(p["stop"] - p["init_stop"]) > 1e-12
            return self._close(pid, p["stop"], "trailing" if moved else "stop")
        if (px >= p["target"]) if long else (px <= p["target"]):
            return self._close(pid, p["target"], "target")
        risk = abs(p["entry"] - p["init_stop"])
        g = {**{"pg": 2.0, "pg_sig": 2.0, "tp1": 2.0, "gb": 1.0}, **p["genome"]}
        d = 1 if long else -1
        if exits.tp1_on(g) and not p.get("partial") and (px - p["entry"]) * d >= g["tp1"] * risk:
            q = p["qty"] * exits.PARTIAL
            fill = px * (1 - d * SLIP)
            pnl = (fill - p["entry"]) * q * d - q * fill * settings.fee_rate
            self.balance += pnl
            p["qty"] -= q
            p["realized"] = p.get("realized", 0.0) + pnl
            p["partial"] = True
            p["stop"] = max(p["stop"], p["entry"]) if long else min(p["stop"], p["entry"])
            self._dirty = True
        p["best"] = max(p["best"], px) if long else min(p["best"], px)
        gain = (p["best"] - p["entry"]) if long else (p["entry"] - p["best"])
        if gain >= g["be"] * risk:
            trail = g["trail"] * p["atr"] * (0.67 if gain >= 2 * risk else 1.0)
            ns = max(p["stop"], p["entry"], p["best"] - trail) if long else min(p["stop"], p["entry"], p["best"] + trail)
            if ns != p["stop"]:
                p["stop"] = ns
                self._dirty = True
        if full and bars is not None and len(bars) >= 2 and risk and bars["ts"].iloc[-1] != p.get("_bar_ts"):
            p["_bar_ts"] = int(bars["ts"].iloc[-1])
            bar, prev = bars.iloc[-1], bars.iloc[-2]
            r_close = (bar["close"] - p["entry"]) * d / risk
            peak = gain / risk
            if exits.gb_on(g) and peak >= exits.GB_ARM_R and r_close < peak * (1 - g["gb"]):
                return self._close(pid, px, "giveback")
            if exits.pg_on(g) and peak >= g["pg"] and r_close > exits.PG_FLOOR:
                n, _ = exits.warnings(bar, prev, p["side"])
                if n >= int(round(g["pg_sig"])):
                    return self._close(pid, px, "guard")
        if full and time.time() - p["opened"] > MAX_HOURS * 3600:
            self._close(pid, px, "timeout")

    def _close(self, pid, px, reason):
        p = self.positions.pop(pid, None)
        if not p:
            return
        d = 1 if p["side"] == "long" else -1
        fill = px * (1 - d * SLIP)
        fee = p["qty"] * fill * settings.fee_rate
        pnl = (fill - p["entry"]) * p["qty"] * d - fee
        self.balance += pnl
        net = pnl - p["fees"] + p.get("realized", 0.0)
        if reason == "stop" and p.get("partial") and abs(p["stop"] - p["entry"]) < 1e-12:
            reason = "breakeven"
        r = net / p["risk_usd"] if p["risk_usd"] else 0.0
        s = self.stats.setdefault(p["strategy"], {"n": 0, "wins": 0, "r": 0.0, "near_n": 0, "near_r": 0.0})
        if p["near"]:
            s["near_n"] += 1
            s["near_r"] += r
        else:
            s["n"] += 1
            s["r"] += r
            s["wins"] += 1 if r > 0 else 0
        t = self.total
        t["n"] += 1
        t["r"] += r
        t["pnl"] += net
        t["wins"] += 1 if r > 0 else 0
        self._dirty = True
        if self.on_close:
            try:
                self.on_close(p, fill, net, r, reason)
            except Exception:
                pass

    def summary(self, prices):
        eq = self.equity(prices)
        t = self.total
        rows = []
        for k, s in self.stats.items():
            rows.append({"strategy": k, "n": s["n"], "winrate": s["wins"] / s["n"] if s["n"] else 0,
                         "avg_r": s["r"] / s["n"] if s["n"] else 0, "near_n": s["near_n"],
                         "near_avg": s["near_r"] / s["near_n"] if s["near_n"] else 0})
        rows.sort(key=lambda x: -(x["n"] + x["near_n"]))
        return {"equity": eq, "start": START, "return": eq / START - 1, "trades": t["n"],
                "winrate": t["wins"] / t["n"] if t["n"] else 0, "avg_r": t["r"] / t["n"] if t["n"] else 0,
                "open": len(self.positions), "max_pos": MAX_POS, "risk": RISK,
                "days": (time.time() - self.created) / 86400, "strategies": rows}
