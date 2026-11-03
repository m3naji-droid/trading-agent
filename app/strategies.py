"""
الاستراتيجيات. كل استراتيجية تُرجع Signal أو None.
الوكيل يشغّلها كلها بالتوازي، والمتعلّم (learner) يقرر أيها يستحق الثقة في كل حالة سوق.
"""
from dataclasses import dataclass, field
import math
import numpy as np

from .regime import TREND_UP, TREND_DOWN, RANGE, HIGH_RISK


@dataclass
class Signal:
    strategy: str
    side: str            # long / short
    entry: float
    stop: float
    target: float
    confidence: float    # 0..1
    reasons: list = field(default_factory=list)

    @property
    def rr(self):
        risk = abs(self.entry - self.stop)
        return abs(self.target - self.entry) / risk if risk else 0


def _mk(name, side, price, atr, stop_atr, rr, conf, reasons, stop_override=None):
    if side == "long":
        stop = stop_override if stop_override else price - stop_atr * atr
        stop = min(stop, price - 0.6 * atr)
        target = price + rr * (price - stop)
    else:
        stop = stop_override if stop_override else price + stop_atr * atr
        stop = max(stop, price + 0.6 * atr)
        target = price - rr * (stop - price)
    return Signal(name, side, price, stop, target, max(0.0, min(1.0, conf)), reasons)


# ---------------------------------------------------------------- الزخم والاختراق
def momentum_breakout(ctx):
    df, reg = ctx["df"], ctx["regime"]["regime"]
    if reg not in (TREND_UP, TREND_DOWN):
        return None
    l = df.iloc[-1]
    htf = ctx["htf"].iloc[-1]
    if reg == TREND_UP and l["close"] > l["hh20"] and l["vol_z"] > 0.8 and htf["close"] > htf["ema50"]:
        conf = 0.55 + min(0.2, (l["adx"] - 23) / 100) + min(0.1, l["vol_z"] / 20)
        return _mk("momentum_breakout", "long", l["close"], l["atr"], 1.6, 2.2, conf,
                   [f"اختراق قمة 20 شمعة بحجم مرتفع (z={l['vol_z']:.1f})", "الفريم الأكبر صاعد"])
    if reg == TREND_DOWN and l["close"] < l["ll20"] and l["vol_z"] > 0.8 and htf["close"] < htf["ema50"]:
        conf = 0.55 + min(0.2, (l["adx"] - 23) / 100) + min(0.1, l["vol_z"] / 20)
        return _mk("momentum_breakout", "short", l["close"], l["atr"], 1.6, 2.2, conf,
                   [f"كسر قاع 20 شمعة بحجم مرتفع (z={l['vol_z']:.1f})", "الفريم الأكبر هابط"])
    return None


# ---------------------------------------------------------------- الارتداد مع الاتجاه
def trend_pullback(ctx):
    df, reg, smc = ctx["df"], ctx["regime"]["regime"], ctx["smc"]
    if reg not in (TREND_UP, TREND_DOWN):
        return None
    l, p = df.iloc[-1], df.iloc[-2]
    if reg == TREND_UP:
        touched = l["low"] <= l["ema20"] * 1.002 or smc["in_bull_fvg"] or smc["in_bull_ob"]
        if touched and l["close"] > l["open"] and 38 < l["rsi"] < 60 and l["close"] > l["ema50"]:
            r = ["ارتداد من EMA20 داخل اتجاه صاعد"]
            conf = 0.56
            if smc["in_bull_ob"]:
                r.append("داخل Order Block صاعد"); conf += 0.07
            if smc["in_bull_fvg"]:
                r.append("داخل FVG صاعدة"); conf += 0.05
            return _mk("trend_pullback", "long", l["close"], l["atr"], 1.4, 2.5, conf, r,
                       stop_override=min(l["low"], p["low"]) - 0.3 * l["atr"])
    else:
        touched = l["high"] >= l["ema20"] * 0.998 or smc["in_bear_fvg"] or smc["in_bear_ob"]
        if touched and l["close"] < l["open"] and 40 < l["rsi"] < 62 and l["close"] < l["ema50"]:
            r = ["ارتداد من EMA20 داخل اتجاه هابط"]
            conf = 0.56
            if smc["in_bear_ob"]:
                r.append("داخل Order Block هابط"); conf += 0.07
            if smc["in_bear_fvg"]:
                r.append("داخل FVG هابطة"); conf += 0.05
            return _mk("trend_pullback", "short", l["close"], l["atr"], 1.4, 2.5, conf, r,
                       stop_override=max(l["high"], p["high"]) + 0.3 * l["atr"])
    return None


# ---------------------------------------------------------------- الارتداد للمتوسط (عرضي)
def mean_reversion(ctx):
    df, reg = ctx["df"], ctx["regime"]["regime"]
    if reg != RANGE:
        return None
    l = df.iloc[-1]
    if l["adx"] > 22:
        return None
    if l["close"] < l["bb_low"] and l["rsi"] < 32:
        tgt_rr = max(1.2, (l["bb_mid"] - l["close"]) / (1.2 * l["atr"]))
        return _mk("mean_reversion", "long", l["close"], l["atr"], 1.2, min(tgt_rr, 3), 0.58,
                   [f"تحت البولنجر السفلي وRSI={l['rsi']:.0f} في سوق عرضي"])
    if l["close"] > l["bb_up"] and l["rsi"] > 68:
        tgt_rr = max(1.2, (l["close"] - l["bb_mid"]) / (1.2 * l["atr"]))
        return _mk("mean_reversion", "short", l["close"], l["atr"], 1.2, min(tgt_rr, 3), 0.58,
                   [f"فوق البولنجر العلوي وRSI={l['rsi']:.0f} في سوق عرضي"])
    return None


# ---------------------------------------------------------------- انعكاس الأموال الذكية
def smc_reversal(ctx):
    df, reg, smc = ctx["df"], ctx["regime"]["regime"], ctx["smc"]
    if reg == HIGH_RISK:
        return None
    l = df.iloc[-1]
    sw = smc["sweep"]
    if sw["bull_sweep"] and (smc["in_bull_ob"] or smc["in_bull_fvg"] or smc["phase"] == "accumulation"):
        r = ["سحب سيولة أسفل قاع سابق ثم إغلاق داخله"]
        if smc["phase"] == "accumulation":
            r.append("منطقة تجميع (OBV صاعد مع سعر مستقر)")
        return _mk("smc_reversal", "long", l["close"], l["atr"], 1.0, 2.8, 0.6, r,
                   stop_override=l["low"] - 0.25 * l["atr"])
    if sw["bear_sweep"] and (smc["in_bear_ob"] or smc["in_bear_fvg"] or smc["phase"] == "distribution"):
        r = ["سحب سيولة أعلى قمة سابقة ثم إغلاق داخلها"]
        if smc["phase"] == "distribution":
            r.append("منطقة تصريف (OBV هابط مع سعر مستقر)")
        return _mk("smc_reversal", "short", l["close"], l["atr"], 1.0, 2.8, 0.6, r,
                   stop_override=l["high"] + 0.25 * l["atr"])
    return None


STRATEGIES = {
    "momentum_breakout": momentum_breakout,
    "trend_pullback": trend_pullback,
    "mean_reversion": mean_reversion,
    "smc_reversal": smc_reversal,
}

STRATEGY_AR = {
    "momentum_breakout": "الزخم والاختراق",
    "trend_pullback": "الارتداد مع الاتجاه",
    "mean_reversion": "الارتداد للمتوسط",
    "smc_reversal": "انعكاس الأموال الذكية",
}


def apply_orderflow(sig: Signal, flow: dict) -> Signal:
    """تعديل الثقة بناءً على دفتر الأوامر والحيتان والتمويل والتصفيات."""
    if not flow:
        return sig
    d = 1 if sig.side == "long" else -1
    imb = flow.get("imbalance", 0) * d
    whale = flow.get("whale", {}).get("ratio", 0) * d
    fund = flow.get("funding", 0)
    liq_bias = flow.get("liq", {}).get("bias", 0) * d
    adj = 0.08 * imb + 0.08 * whale + 0.04 * liq_bias
    if imb > 0.25:
        sig.reasons.append(f"دفتر الأوامر يدعم ({imb:+.2f})")
    if imb < -0.25:
        sig.reasons.append(f"دفتر الأوامر يعاكس ({imb:+.2f})")
    if whale > 0.3:
        sig.reasons.append("الحيتان تدخل بنفس الاتجاه")
    if whale < -0.3:
        sig.reasons.append("الحيتان تعاكس الاتجاه")
    # تمويل مرتفع جداً = ازدحام شراء → خطر على الشراء
    if d == 1 and fund > 0.0006:
        adj -= 0.06; sig.reasons.append("تمويل مرتفع (ازدحام مشترين)")
    if d == -1 and fund < -0.0006:
        adj -= 0.06; sig.reasons.append("تمويل سالب جداً (ازدحام بائعين)")
    sig.confidence = max(0.0, min(1.0, sig.confidence + adj))
    return sig


REGIME_IDX = {TREND_UP: 0, TREND_DOWN: 1, RANGE: 2, HIGH_RISK: 3}
PHASE_IDX = {"accumulation": 1, "none": 0, "distribution": -1}


def features(ctx, sig: Signal) -> list:
    """بصمة رقمية لظروف الصفقة — تستخدمها الذاكرة لمقارنة الأنماط."""
    l = ctx["df"].iloc[-1]
    flow = ctx.get("flow") or {}
    reg = [0, 0, 0, 0]
    reg[REGIME_IDX[ctx["regime"]["regime"]]] = 1
    strat = [1 if sig.strategy == s else 0 for s in STRATEGIES]

    def safe(x, d=0.0):
        return d if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)

    return reg + strat + [
        1 if sig.side == "long" else -1,
        safe(l["adx"]) / 50,
        safe(l["rsi"]) / 100,
        min(3, safe(l["atr_pct"]) * 50),
        min(3, safe(l["bb_width"]) * 10),
        max(-2, min(2, safe(l["vol_z"]) / 3)),
        (l["close"] - l["ema50"]) / l["ema50"] * 20,
        safe(flow.get("imbalance")),
        safe((flow.get("whale") or {}).get("ratio")),
        max(-2, min(2, safe(flow.get("funding")) * 1000)),
        float(ctx["smc"]["bos"]),
        float(PHASE_IDX.get(ctx["smc"]["phase"], 0)),
    ]


def run_all(ctx) -> list:
    out = []
    for name, fn in STRATEGIES.items():
        try:
            s = fn(ctx)
        except Exception:
            s = None
        if s and s.rr >= 1.2:
            out.append(s)
    return out
