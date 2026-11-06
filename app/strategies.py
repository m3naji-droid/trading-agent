"""
الاستراتيجيات. كل استراتيجية تُرجع Signal أو None.
الوكيل يشغّلها كلها بالتوازي، والمتعلّم (learner) يقرر أيها يستحق الثقة في كل حالة سوق.
"""
from dataclasses import dataclass, field
import math
import numpy as np

from .regime import TREND_UP, TREND_DOWN, RANGE, HIGH_RISK
from .smc import swing_points


def _relax(ctx):
    """وضع "الفرص القريبة": شروط أخف قليلاً — للتعلم فقط، لا يتداول بها الوكيل فعلياً."""
    return bool(ctx.get("relax"))


def _ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


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
    vz = 0.4 if _relax(ctx) else 0.8
    slack = 0.2 * l["atr"] if _relax(ctx) else 0
    if reg == TREND_UP and l["close"] > l["hh20"] - slack and l["vol_z"] > vz and htf["close"] > htf["ema50"]:
        conf = 0.55 + min(0.2, (l["adx"] - 23) / 100) + min(0.1, l["vol_z"] / 20)
        return _mk("momentum_breakout", "long", l["close"], l["atr"], 1.6, 2.2, conf,
                   [f"اختراق قمة 20 شمعة بحجم مرتفع (z={l['vol_z']:.1f})", "الفريم الأكبر صاعد"])
    if reg == TREND_DOWN and l["close"] < l["ll20"] + slack and l["vol_z"] > vz and htf["close"] < htf["ema50"]:
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
        lo, hi = (34, 65) if _relax(ctx) else (38, 60)
        if touched and l["close"] > l["open"] and lo < l["rsi"] < hi and l["close"] > l["ema50"]:
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
        lo, hi = (35, 66) if _relax(ctx) else (40, 62)
        if touched and l["close"] < l["open"] and lo < l["rsi"] < hi and l["close"] < l["ema50"]:
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
    k = 0.2 * l["atr"] if _relax(ctx) else 0
    r_lo, r_hi = (36, 64) if _relax(ctx) else (32, 68)
    if l["close"] < l["bb_low"] + k and l["rsi"] < r_lo:
        tgt_rr = max(1.2, (l["bb_mid"] - l["close"]) / (1.2 * l["atr"]))
        return _mk("mean_reversion", "long", l["close"], l["atr"], 1.2, min(tgt_rr, 3), 0.58,
                   [f"تحت البولنجر السفلي وRSI={l['rsi']:.0f} في سوق عرضي"])
    if l["close"] > l["bb_up"] - k and l["rsi"] > r_hi:
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
    rx = _relax(ctx)
    if sw["bull_sweep"] and (rx or smc["in_bull_ob"] or smc["in_bull_fvg"] or smc["phase"] == "accumulation"):
        r = ["سحب سيولة أسفل قاع سابق ثم إغلاق داخله"]
        if smc["phase"] == "accumulation":
            r.append("منطقة تجميع (OBV صاعد مع سعر مستقر)")
        return _mk("smc_reversal", "long", l["close"], l["atr"], 1.0, 2.8, 0.6, r,
                   stop_override=l["low"] - 0.25 * l["atr"])
    if sw["bear_sweep"] and (rx or smc["in_bear_ob"] or smc["in_bear_fvg"] or smc["phase"] == "distribution"):
        r = ["سحب سيولة أعلى قمة سابقة ثم إغلاق داخلها"]
        if smc["phase"] == "distribution":
            r.append("منطقة تصريف (OBV هابط مع سعر مستقر)")
        return _mk("smc_reversal", "short", l["close"], l["atr"], 1.0, 2.8, 0.6, r,
                   stop_override=l["high"] + 0.25 * l["atr"])
    return None


# ================================================================ الاستراتيجيات الجديدة (v6)

# ---------------------------------------------------------------- 1) صيد التصفيات
def liquidation_hunt(ctx):
    """بعد موجة تصفيات كبيرة ينهار السعر بشكل مبالغ فيه ثم يرتد. يدخل بعد انتهاء الموجة لا أثناءها."""
    liq = ctx.get("liq")
    if not liq:
        return None  # لا توجد بيانات تصفيات تاريخية — تتعلمها من التداول الحي فقط
    df = ctx["df"]
    l = df.iloc[-1]
    a = l["atr"]
    thr = liq["thr"] * (0.6 if _relax(ctx) else 1)
    move = 1.0 if _relax(ctx) else 1.5
    rng = max(l["high"] - l["low"], 1e-12)
    lo4, hi4 = df["low"].iloc[-4:].min(), df["high"].iloc[-4:].max()
    ref = df["close"].iloc[-5]
    if (liq["long_usd"] >= thr and liq["long_usd"] >= 3 * liq["short_usd"] and ref - lo4 >= move * a
            and l["close"] > l["open"] and (l["close"] - l["low"]) >= 0.5 * rng):
        return _mk("liquidation_hunt", "long", l["close"], a, 1.0, 2.5, 0.58,
                   [f"تصفية مشترين بقيمة ${liq['long_usd'] / 1e6:.1f}M خلال 15 دقيقة", "انهيار مبالغ فيه ثم شمعة ارتداد"],
                   stop_override=lo4 - 0.25 * a)
    if (liq["short_usd"] >= thr and liq["short_usd"] >= 3 * liq["long_usd"] and hi4 - ref >= move * a
            and l["close"] < l["open"] and (l["high"] - l["close"]) >= 0.5 * rng):
        return _mk("liquidation_hunt", "short", l["close"], a, 1.0, 2.5, 0.58,
                   [f"تصفية بائعين بقيمة ${liq['short_usd'] / 1e6:.1f}M خلال 15 دقيقة", "قفزة مبالغ فيها ثم شمعة انعكاس"],
                   stop_override=hi4 + 0.25 * a)
    return None


# ---------------------------------------------------------------- 2) التمويل المتطرف
def funding_extreme(ctx):
    """المشترون يدفعون تمويلاً مرتفعاً جداً بينما السعر يتوقف = ازدحام معرض للانعكاس (والعكس)."""
    df = ctx["df"]
    l = df.iloc[-1]
    f = l.get("funding")
    if not _ok(f) or len(df) < 8:
        return None
    hi_t, lo_t = (0.0003, -0.0002) if _relax(ctx) else (0.0005, -0.0003)
    oi = l.get("oi_chg")
    a = l["atr"]
    if f >= hi_t and l["close"] < l["ema20"] and df["high"].iloc[-3:].max() < df["high"].iloc[-6:-3].max() and l["rsi"] < 55:
        r = [f"تمويل مرتفع جداً {f * 100:.3f}% (المشترون مزدحمون)", "السعر توقف وبدأ يصنع قمماً أدنى"]
        conf = 0.57
        if _ok(oi) and oi > 0.03:
            conf += 0.05; r.append(f"العقود المفتوحة ترتفع {oi:+.0%} — رافعة متراكمة")
        return _mk("funding_extreme", "short", l["close"], a, 1.2, 2.4, conf, r,
                   stop_override=df["high"].iloc[-6:].max() + 0.3 * a)
    if f <= lo_t and l["close"] > l["ema20"] and df["low"].iloc[-3:].min() > df["low"].iloc[-6:-3].min() and l["rsi"] > 45:
        r = [f"تمويل سالب {f * 100:.3f}% (البائعون مزدحمون)", "السعر توقف عن الهبوط ويصنع قيعاناً أعلى"]
        conf = 0.57
        if _ok(oi) and oi > 0.03:
            conf += 0.05; r.append(f"العقود المفتوحة ترتفع {oi:+.0%} — ضغط شورت متراكم")
        return _mk("funding_extreme", "long", l["close"], a, 1.2, 2.4, conf, r,
                   stop_override=df["low"].iloc[-6:].min() - 0.3 * a)
    return None


# ---------------------------------------------------------------- 3) ظل البيتكوين
def btc_shadow(ctx):
    """البيتكوين اخترق للتو، والعملة شديدة التأثر به لم تلحقه بعد."""
    if ctx.get("base") == "BTC":
        return None
    df = ctx["df"]
    l = df.iloc[-1]
    need = ("btc_brk", "beta", "corr", "btc_ret3", "alt_ret3")
    if any(c not in df.columns or not _ok(l[c]) for c in need):
        return None
    b_min, c_min, lag = (0.8, 0.5, 0.7) if _relax(ctx) else (1.0, 0.6, 0.5)
    a = l["atr"]
    if l["btc_brk"] == 1 and l["beta"] >= b_min and l["corr"] >= c_min and l["btc_ret3"] > 0.004 \
            and l["alt_ret3"] < lag * l["beta"] * l["btc_ret3"] and l["close"] > l["ema50"]:
        return _mk("btc_shadow", "long", l["close"], a, 1.3, 2.0, 0.57 + min(0.06, (l["corr"] - 0.6) / 4),
                   [f"البيتكوين اخترق للأعلى ({l['btc_ret3']:+.1%})", f"العملة تتبعه عادة (بيتا {l['beta']:.1f}، ارتباط {l['corr']:.2f}) ولم تتحرك بعد"])
    if l["btc_brk"] == -1 and l["beta"] >= b_min and l["corr"] >= c_min and l["btc_ret3"] < -0.004 \
            and l["alt_ret3"] > lag * l["beta"] * l["btc_ret3"] and l["close"] < l["ema50"]:
        return _mk("btc_shadow", "short", l["close"], a, 1.3, 2.0, 0.57 + min(0.06, (l["corr"] - 0.6) / 4),
                   [f"البيتكوين كسر للأسفل ({l['btc_ret3']:+.1%})", f"العملة تتبعه عادة (بيتا {l['beta']:.1f}) ولم تهبط بعد"])
    return None


# ---------------------------------------------------------------- 4) دوران القوة
def rs_rotation(ctx):
    """شراء الأقوى نسبةً للبيتكوين في السوق الصاعد، وبيع الأضعف في الهابط."""
    df, reg = ctx["df"], ctx["regime"]["regime"]
    l, p = df.iloc[-1], df.iloc[-2]
    rk = l.get("rs_rank")
    if not _ok(rk):
        return None
    top, bot = (0.3, 0.7) if _relax(ctx) else (0.2, 0.8)
    a = l["atr"]
    near20 = lambda side: _relax(ctx) or (df["low"].iloc[-4:].min() <= l["ema20"] * 1.01 if side == 1
                                          else df["high"].iloc[-4:].max() >= l["ema20"] * 0.99)
    if rk <= top and reg == TREND_UP and l["close"] > l["ema50"] and 45 < l["rsi"] < 70 \
            and l["close"] > p["high"] and near20(1):
        return _mk("rs_rotation", "long", l["close"], a, 1.4, 2.5, 0.58 + (top - rk) * 0.3,
                   [f"من أقوى العملات مقارنة بالبيتكوين (أعلى {max(rk, 0.01):.0%})", "ارتداد في اتجاه صاعد ثم اختراق الشمعة السابقة"],
                   stop_override=df["low"].iloc[-3:].min() - 0.3 * a)
    if rk >= bot and reg == TREND_DOWN and l["close"] < l["ema50"] and 30 < l["rsi"] < 55 \
            and l["close"] < p["low"] and near20(-1):
        return _mk("rs_rotation", "short", l["close"], a, 1.4, 2.5, 0.58 + (rk - bot) * 0.3,
                   [f"من أضعف العملات مقارنة بالبيتكوين (أدنى {1 - rk + 0.01:.0%})", "ارتداد في اتجاه هابط ثم كسر الشمعة السابقة"],
                   stop_override=df["high"].iloc[-3:].max() + 0.3 * a)
    return None


# ---------------------------------------------------------------- 5) الانضغاط ثم الانفجار
def squeeze_breakout(ctx):
    df = ctx["df"]
    l, p = df.iloc[-1], df.iloc[-2]
    min_len = 4 if _relax(ctx) else 6
    if not (bool(p["squeeze"]) and p["sq_len"] >= min_len and not bool(l["squeeze"])):
        return None
    vz = 0.0 if _relax(ctx) else 0.5
    a = l["atr"]
    if l["close"] > l["hh20"] and l["close"] > l["ema20"] and l["vol_z"] >= vz:
        return _mk("squeeze_breakout", "long", l["close"], a, 1.5, 2.5, 0.58 + min(0.06, p["sq_len"] / 200),
                   [f"انضغاط استمر {int(p['sq_len'])} شمعة", "انفجار للأعلى مع اختراق قمة 20 شمعة"],
                   stop_override=min(l["ema20"], l["close"] - 1.0 * a) - 0.3 * a)
    if l["close"] < l["ll20"] and l["close"] < l["ema20"] and l["vol_z"] >= vz:
        return _mk("squeeze_breakout", "short", l["close"], a, 1.5, 2.5, 0.58 + min(0.06, p["sq_len"] / 200),
                   [f"انضغاط استمر {int(p['sq_len'])} شمعة", "انفجار للأسفل مع كسر قاع 20 شمعة"],
                   stop_override=max(l["ema20"], l["close"] + 1.0 * a) + 0.3 * a)
    return None


# ---------------------------------------------------------------- 6) توافق الفريمات
def mtf_confluence(ctx):
    """الاتجاه الكبير + هيكل الفريم الأوسط + نقطة دخول دقيقة: صفقات أقل وأجود."""
    ttf = ctx.get("ttf")
    if ttf is None or len(ttf) < 30:
        return None
    df, h = ctx["df"], ctx["htf"]
    t, hl, l, p = ttf.iloc[-1], h.iloc[-1], df.iloc[-1], df.iloc[-2]
    a = l["atr"]
    rx = _relax(ctx)
    up_t = t["close"] > t["ema50"] and t["ema20"] > t["ema50"]
    dn_t = t["close"] < t["ema50"] and t["ema20"] < t["ema50"]
    hl_up = rx or h["low"].iloc[-10:].min() > h["low"].iloc[-20:-10].min()
    lh_dn = rx or h["high"].iloc[-10:].max() < h["high"].iloc[-20:-10].max()
    if up_t and hl["close"] > hl["ema50"] and hl_up and df["low"].iloc[-4:].min() <= l["ema20"] * 1.003 \
            and l["close"] > p["high"] and l["close"] > l["open"]:
        return _mk("mtf_confluence", "long", l["close"], a, 1.4, 3.0, 0.62,
                   ["الفريم الكبير صاعد", "الفريم الأوسط يصنع قيعاناً أعلى", "ارتداد للمتوسط ثم اختراق في الفريم الصغير"],
                   stop_override=df["low"].iloc[-4:].min() - 0.3 * a)
    if dn_t and hl["close"] < hl["ema50"] and lh_dn and df["high"].iloc[-4:].max() >= l["ema20"] * 0.997 \
            and l["close"] < p["low"] and l["close"] < l["open"]:
        return _mk("mtf_confluence", "short", l["close"], a, 1.4, 3.0, 0.62,
                   ["الفريم الكبير هابط", "الفريم الأوسط يصنع قمماً أدنى", "ارتداد للمتوسط ثم كسر في الفريم الصغير"],
                   stop_override=df["high"].iloc[-4:].max() + 0.3 * a)
    return None


# ---------------------------------------------------------------- 7) الانحراف الخفي
def divergence(ctx):
    """قمة سعرية جديدة لا يؤكدها الزخم ولا الحجم (أو العكس في القاع)."""
    df, smc = ctx["df"], ctx["smc"]
    sub = df.iloc[-60:]
    highs, lows = swing_points(sub, 3)
    l, p = df.iloc[-1], df.iloc[-2]
    a = l["atr"]
    n = len(sub)
    rx = _relax(ctx)
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        H, R, O = sub["high"].values, sub["rsi"].values, sub["obv"].values
        if n - i2 <= 12 and H[i2] > H[i1] and R[i2] < R[i1] - 3 and O[i2] <= O[i1] and R[i1] > 60:
            if (l["close"] < p["low"]) or (rx and l["close"] < l["open"]):
                r = ["قمة سعرية أعلى بزخم أضعف (انحراف سلبي)", "الحجم لا يؤكد الصعود"]
                conf = 0.57
                if smc["sweep"]["bear_sweep"]:
                    conf += 0.06; r.append("مع سحب سيولة فوق القمة")
                return _mk("divergence", "short", l["close"], a, 1.2, 2.5, conf, r, stop_override=H[i2] + 0.3 * a)
    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        L, R, O = sub["low"].values, sub["rsi"].values, sub["obv"].values
        if n - i2 <= 12 and L[i2] < L[i1] and R[i2] > R[i1] + 3 and O[i2] >= O[i1] and R[i1] < 40:
            if (l["close"] > p["high"]) or (rx and l["close"] > l["open"]):
                r = ["قاع سعري أدنى بزخم أقوى (انحراف إيجابي)", "البيع يضعف"]
                conf = 0.57
                if smc["sweep"]["bull_sweep"]:
                    conf += 0.06; r.append("مع سحب سيولة تحت القاع")
                return _mk("divergence", "long", l["close"], a, 1.2, 2.5, conf, r, stop_override=L[i2] - 0.3 * a)
    return None


# ---------------------------------------------------------------- 8) افتتاح الجلسات
SESSIONS = {"لندن": (7, 10), "نيويورك": (13, 16)}  # بتوقيت UTC


def session_breakout(ctx):
    """نطاق جلسة آسيا الهادئ يُكسر عند افتتاح لندن أو نيويورك."""
    df = ctx["df"]
    ts = df["ts"].values
    hour = int((ts[-1] // 3_600_000) % 24)
    sess = next((k for k, (a, b) in SESSIONS.items() if a <= hour < b), None)
    if not sess:
        return None
    day = ts // 86_400_000
    hrs = (ts // 3_600_000) % 24
    m = (day == day[-1]) & (hrs < 7)
    if m.sum() < 4:
        return None
    hi, lo = df["high"].values[m].max(), df["low"].values[m].min()
    l, p = df.iloc[-1], df.iloc[-2]
    if (hi - lo) / l["close"] > (0.06 if _relax(ctx) else 0.04):
        return None  # آسيا لم تكن هادئة
    vz = 0.0 if _relax(ctx) else 0.5
    mid = (hi + lo) / 2
    if l["close"] > hi and p["close"] <= hi and l["vol_z"] >= vz:
        return _mk("session_breakout", "long", l["close"], l["atr"], 1.2, 2.0, 0.56,
                   [f"اختراق نطاق آسيا عند افتتاح {sess}", f"نطاق آسيا هادئ ({(hi - lo) / l['close']:.1%})"],
                   stop_override=min(mid, l["close"] - 0.8 * l["atr"]))
    if l["close"] < lo and p["close"] >= lo and l["vol_z"] >= vz:
        return _mk("session_breakout", "short", l["close"], l["atr"], 1.2, 2.0, 0.56,
                   [f"كسر نطاق آسيا عند افتتاح {sess}", f"نطاق آسيا هادئ ({(hi - lo) / l['close']:.1%})"],
                   stop_override=max(mid, l["close"] + 0.8 * l["atr"]))
    return None



# ================================================================ الاستراتيجيات الجديدة (v11)

# ---------------------------------------------------------------- 9) اختراق السلحفاة
def donchian_turtle(ctx):
    """اختراق أعلى/أدنى 55 شمعة مع الاتجاه الأكبر — من أشهر أنظمة تتبع الاتجاه تاريخياً."""
    df, h = ctx["df"], ctx["htf"].iloc[-1]
    l, p = df.iloc[-1], df.iloc[-2]
    a = l["atr"]
    slack = 0.2 * a if _relax(ctx) else 0
    if l["close"] > l["hh55"] - slack and p["close"] <= p["hh55"] and h["close"] > h["ema50"] and l["adx"] > 18:
        return _mk("donchian_turtle", "long", l["close"], a, 2.0, 3.0, 0.57,
                   ["اختراق أعلى قمة في 55 شمعة", "الفريم الأكبر صاعد"])
    if l["close"] < l["ll55"] + slack and p["close"] >= p["ll55"] and h["close"] < h["ema50"] and l["adx"] > 18:
        return _mk("donchian_turtle", "short", l["close"], a, 2.0, 3.0, 0.57,
                   ["كسر أدنى قاع في 55 شمعة", "الفريم الأكبر هابط"])
    return None


# ---------------------------------------------------------------- 10) ارتداد RSI-2
def rsi2_pullback(ctx):
    """تصحيح حاد قصير داخل اتجاه قوي (RSI بفترة 2) — ارتداد سريع وهدف قريب."""
    df = ctx["df"]
    l = df.iloc[-1]
    lo, hi = (15, 85) if _relax(ctx) else (10, 90)
    a = l["atr"]
    if l["close"] > l["ema200"] and l["ema50"] > l["ema200"] and l["rsi2"] < lo:
        return _mk("rsi2_pullback", "long", l["close"], a, 1.5, 1.5, 0.57,
                   [f"تصحيح حاد (RSI-2 = {l['rsi2']:.0f})", "داخل اتجاه صاعد طويل (فوق EMA200)"])
    if l["close"] < l["ema200"] and l["ema50"] < l["ema200"] and l["rsi2"] > hi:
        return _mk("rsi2_pullback", "short", l["close"], a, 1.5, 1.5, 0.57,
                   [f"ارتداد حاد (RSI-2 = {l['rsi2']:.0f})", "داخل اتجاه هابط طويل (تحت EMA200)"])
    return None


# ---------------------------------------------------------------- 11) استعادة VWAP
def vwap_reclaim(ctx):
    """السعر يستعيد متوسط اليوم المرجّح بالحجم (VWAP) مع اتجاه الفريم الأكبر."""
    df, h = ctx["df"], ctx["htf"].iloc[-1]
    l, p = df.iloc[-1], df.iloc[-2]
    vz = 0.0 if _relax(ctx) else 0.3
    a = l["atr"]
    if p["close"] < p["vwap"] and l["close"] > l["vwap"] and l["close"] > l["open"] and l["vol_z"] >= vz \
            and h["close"] > h["ema50"] and l["close"] > l["ema50"]:
        return _mk("vwap_reclaim", "long", l["close"], a, 1.2, 2.0, 0.56,
                   ["استعادة VWAP اليومي بحجم جيد", "الفريم الأكبر صاعد"],
                   stop_override=df["low"].iloc[-4:].min() - 0.2 * a)
    if p["close"] > p["vwap"] and l["close"] < l["vwap"] and l["close"] < l["open"] and l["vol_z"] >= vz \
            and h["close"] < h["ema50"] and l["close"] < l["ema50"]:
        return _mk("vwap_reclaim", "short", l["close"], a, 1.2, 2.0, 0.56,
                   ["خسارة VWAP اليومي بحجم جيد", "الفريم الأكبر هابط"],
                   stop_override=df["high"].iloc[-4:].max() + 0.2 * a)
    return None


# ---------------------------------------------------------------- 12) الترند الخارق
def supertrend_adx(ctx):
    """انقلاب مؤشر Supertrend مع قوة اتجاه كافية."""
    df = ctx["df"]
    l, p = df.iloc[-1], df.iloc[-2]
    adx_min = 16 if _relax(ctx) else 20
    a = l["atr"]
    if p["st_dir"] == -1 and l["st_dir"] == 1 and l["adx"] > adx_min and l["close"] > l["ema50"]:
        return _mk("supertrend_adx", "long", l["close"], a, 1.5, 2.5, 0.57,
                   ["Supertrend انقلب صاعداً", f"قوة اتجاه ADX {l['adx']:.0f}"],
                   stop_override=min(l["st_line"], l["close"] - 0.8 * a))
    if p["st_dir"] == 1 and l["st_dir"] == -1 and l["adx"] > adx_min and l["close"] < l["ema50"]:
        return _mk("supertrend_adx", "short", l["close"], a, 1.5, 2.5, 0.57,
                   ["Supertrend انقلب هابطاً", f"قوة اتجاه ADX {l['adx']:.0f}"],
                   stop_override=max(l["st_line"], l["close"] + 0.8 * a))
    return None


# ---------------------------------------------------------------- 13) اختبار مستوى الأمس
def level_retest(ctx):
    """كسر قمة/قاع الأمس، ثم عودة لاختباره من الجهة الأخرى وصموده."""
    df = ctx["df"]
    l = df.iloc[-1]
    pdh, pdl, a = l["pdh"], l["pdl"], l["atr"]
    if pdh <= 0 or pdl <= 0:
        return None
    rec = df.iloc[-13:-1]
    tol = 0.35 * a if _relax(ctx) else 0.2 * a
    if (rec["close"] > pdh).any() and l["low"] <= pdh + tol and l["close"] > pdh and l["close"] > l["open"]:
        return _mk("level_retest", "long", l["close"], a, 1.2, 2.2, 0.57,
                   ["اختراق قمة الأمس", "إعادة اختبارها من الأعلى وصمودها"], stop_override=pdh - 0.5 * a)
    if (rec["close"] < pdl).any() and l["high"] >= pdl - tol and l["close"] < pdl and l["close"] < l["open"]:
        return _mk("level_retest", "short", l["close"], a, 1.2, 2.2, 0.57,
                   ["كسر قاع الأمس", "إعادة اختباره من الأسفل ورفضه"], stop_override=pdl + 0.5 * a)
    return None


# ---------------------------------------------------------------- 14) الاختراق ثم إعادة الاختبار
def breakout_retest(ctx):
    """اختراق حقيقي يعود السعر لاختباره ولا يكسره — دخول أفضل من مطاردة الاختراق."""
    df = ctx["df"]
    l = df.iloc[-1]
    a = l["atr"]
    win = df.iloc[-16:-3]
    tol = 0.35 * a if _relax(ctx) else 0.2 * a
    up = win[win["close"] > win["hh20"]]
    if len(up):
        lvl = float(up["hh20"].iloc[-1])
        held = df["low"].iloc[-3:].min() > lvl - 0.3 * a
        if held and l["low"] <= lvl + tol and l["close"] > lvl and l["close"] > l["open"]:
            return _mk("breakout_retest", "long", l["close"], a, 1.2, 2.5, 0.58,
                       ["اختراق مستوى مقاومة", "عاد السعر لاختباره وصمد فوقه"], stop_override=lvl - 0.6 * a)
    dn = win[win["close"] < win["ll20"]]
    if len(dn):
        lvl = float(dn["ll20"].iloc[-1])
        held = df["high"].iloc[-3:].max() < lvl + 0.3 * a
        if held and l["high"] >= lvl - tol and l["close"] < lvl and l["close"] < l["open"]:
            return _mk("breakout_retest", "short", l["close"], a, 1.2, 2.5, 0.58,
                       ["كسر مستوى دعم", "عاد السعر لاختباره ورُفض تحته"], stop_override=lvl + 0.6 * a)
    return None


ORIGINAL = ["momentum_breakout", "trend_pullback", "mean_reversion", "smc_reversal"]
STRATEGIES = {
    "momentum_breakout": momentum_breakout,
    "trend_pullback": trend_pullback,
    "mean_reversion": mean_reversion,
    "smc_reversal": smc_reversal,
    "liquidation_hunt": liquidation_hunt,
    "funding_extreme": funding_extreme,
    "btc_shadow": btc_shadow,
    "rs_rotation": rs_rotation,
    "squeeze_breakout": squeeze_breakout,
    "mtf_confluence": mtf_confluence,
    "divergence": divergence,
    "session_breakout": session_breakout,
    "donchian_turtle": donchian_turtle,
    "rsi2_pullback": rsi2_pullback,
    "vwap_reclaim": vwap_reclaim,
    "supertrend_adx": supertrend_adx,
    "level_retest": level_retest,
    "breakout_retest": breakout_retest,
}

STRATEGY_AR = {
    "momentum_breakout": "الزخم والاختراق",
    "trend_pullback": "الارتداد مع الاتجاه",
    "mean_reversion": "الارتداد للمتوسط",
    "smc_reversal": "انعكاس الأموال الذكية",
    "liquidation_hunt": "صيد التصفيات",
    "funding_extreme": "التمويل المتطرف",
    "btc_shadow": "ظل البيتكوين",
    "rs_rotation": "دوران القوة",
    "squeeze_breakout": "الانضغاط ثم الانفجار",
    "mtf_confluence": "توافق الفريمات",
    "divergence": "الانحراف الخفي",
    "session_breakout": "افتتاح الجلسات",
    "donchian_turtle": "اختراق السلحفاة",
    "rsi2_pullback": "ارتداد RSI-2",
    "vwap_reclaim": "استعادة VWAP",
    "supertrend_adx": "الترند الخارق",
    "level_retest": "اختبار مستوى الأمس",
    "breakout_retest": "الاختراق ثم إعادة الاختبار",
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
    strat = [1 if sig.strategy == s else 0 for s in ORIGINAL]  # طول ثابت للتوافق مع الذاكرة القديمة

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


def metrics(ctx, sig: Signal) -> dict:
    """مقاييس جودة الإشارة التي يطوّر داروين حدودها (شروط الدخول)."""
    l = ctx["df"].iloc[-1]
    rsi = float(l["rsi"]) if _ok(l["rsi"]) else 50.0
    return {"vz": float(l["vol_z"]) if _ok(l["vol_z"]) else 0.0, "adx": float(l["adx"]) if _ok(l["adx"]) else 0.0,
            "rsi_dir": rsi if sig.side == "long" else 100 - rsi, "rr": float(sig.rr)}


def run_all(ctx, only=None) -> list:
    out = []
    for name, fn in STRATEGIES.items():
        if only is not None and name not in only:
            continue
        try:
            s = fn(ctx)
        except Exception:
            s = None
        if s and s.rr >= 1.2:
            out.append(s)
    return out
